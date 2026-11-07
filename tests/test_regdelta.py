"""RegDelta acceptance tests — the demonstration success criteria from the proposal.

Run: python -m unittest -v
"""
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from regdelta import gate, workflow
from regdelta.workflow import Ctx

ROOT = Path(__file__).resolve().parent.parent
WS = ROOT / "fixtures" / "harbourview-bank"
EXPECTED = json.loads((ROOT / "fixtures" / "expected.json").read_text(encoding="utf-8"))


def tree_hash(folder: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(folder.rglob("*")):
        if p.is_file():
            h.update(p.relative_to(folder).as_posix().encode())
            h.update(p.read_bytes())
    return h.hexdigest()


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.before = tree_hash(WS)
        cls.out = Path(tempfile.mkdtemp(prefix="regdelta-test-"))
        cls.ctx = Ctx(workspace=str(WS), out=str(cls.out), actor="test")
        cls.register = workflow.analyze(cls.ctx)
        cls.assessment = workflow.map_assets(cls.ctx)
        cls.findings = cls.assessment["findings"]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.out, ignore_errors=True)

    def fresh_out(self):
        d = Path(tempfile.mkdtemp(prefix="regdelta-gate-"))
        for f in ("change-register.json", "assessment.json"):
            shutil.copy(self.out / f, d / f)
        self.addCleanup(shutil.rmtree, d, True)
        return d


class TestChangeRegister(Base):
    def test_every_seeded_change_is_found(self):
        got = {c["id"]: {"kind": c["kind"], "subject": c["subject"]} for c in self.register["changes"]}
        self.assertEqual(got, EXPECTED["changes"])

    def test_every_change_references_its_spec_line(self):
        for c in self.register["changes"]:
            r = c["spec_ref"]
            lines = (WS / r["file"]).read_text(encoding="utf-8").splitlines()
            self.assertEqual(lines[r["line"] - 1].strip(), r["text"], c["id"])
            self.assertTrue(r["text"], c["id"])


class TestImpactMatrix(Base):
    def by(self, change, kind):
        return sorted(f["asset"] for f in self.findings if f["change_id"] == change and f["impact_type"] == kind)

    def test_all_seeded_dependencies_found_exactly(self):
        for change, kinds in EXPECTED["findings"].items():
            for kind in ("gap", "direct", "upstream", "downstream", "candidate"):
                with self.subTest(change=change, kind=kind):
                    self.assertEqual(self.by(change, kind), sorted(kinds.get(kind, [])))

    def test_no_false_positives_on_unrelated_assets(self):
        hit = {f["asset"] for f in self.findings}
        for p in EXPECTED["never_affected"]:
            self.assertNotIn(p, hit)

    def test_every_finding_has_verifiable_evidence(self):
        for f in self.findings:
            self.assertTrue(f["evidence"], f["id"])
            for e in f["evidence"]:
                lines = (WS / e["file"]).read_text(encoding="utf-8").splitlines()
                self.assertGreaterEqual(e["line"], 1)
                self.assertEqual(lines[e["line"] - 1].strip(), e["text"], f"{f['id']} {e['file']}:{e['line']}")

    def test_gap_findings_really_lack_the_element(self):
        for f in self.findings:
            if f["impact_type"] == "gap":
                subject = next(c["subject"] for c in self.register["changes"] if c["id"] == f["change_id"])
                self.assertNotIn(subject, (WS / f["asset"]).read_text(encoding="utf-8"), f["id"])

    def test_candidates_are_never_presented_as_confirmed(self):
        for f in self.findings:
            if f["impact_type"] == "candidate":
                self.assertEqual(f["impact_label"], "Semantic candidate")
                self.assertEqual(f["review_status"], "needs_review")
                self.assertTrue(f["confidence"].startswith(("Low", "Medium")))
                self.assertEqual(f["risk"], "Low")
            else:
                self.assertTrue(f["confidence"].startswith("High"))

    def test_lineage_paths_are_real(self):
        graph_edges = {(e["from"], e["to"]) for e in self.assessment["graph"]["edges"]}
        for f in self.findings:
            if f["impact_type"] in ("upstream", "downstream"):
                self.assertGreaterEqual(len(f["via"]), 3, f["id"])
                self.assertEqual(f["via"][-1], f["asset"])

    def test_dependency_map_reaches_downstream_assets(self):
        downstream = {f["asset"] for f in self.findings if f["impact_type"] == "downstream"}
        self.assertIn("dashboards/credit_risk_dashboard.json", downstream)
        self.assertIn("etl/build_submission.py", downstream)
        node_ids = {n["id"] for n in self.assessment["graph"]["nodes"]}
        for p in downstream:
            self.assertIn(p, node_ids)

    def test_artifacts_written(self):
        for name in ("change-register.md", "impact-matrix.csv", "dependency-map.md", "dependency-graph.json", "review-sheet.md"):
            self.assertTrue((self.out / name).exists(), name)
        header = (self.out / "impact-matrix.csv").read_text(encoding="utf-8").splitlines()[0]
        for col in ("Change ID", "Requirement", "Affected asset", "Evidence location", "Impact type", "Risk level", "Confidence", "Review status"):
            self.assertIn(col, header)


class TestApprovalGate(Base):
    def ctx_for(self, d):
        return Ctx(workspace=str(WS), out=str(d), actor="test")

    def test_blocked_without_approval(self):
        d = self.fresh_out()
        r = gate.check(d, WS, log=False)
        self.assertFalse(r["allowed"])
        self.assertEqual(r["code"], "NO_APPROVAL")
        self.assertFalse(workflow.plan(self.ctx_for(d))["allowed"])
        self.assertFalse((d / "remediation-plan.md").exists())

    def test_blocked_with_invalid_record(self):
        d = self.fresh_out()
        (d / "approval-record.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(gate.check(d, WS, log=False)["code"], "INVALID_APPROVAL")
        (d / "approval-record.json").write_text(json.dumps({"schema": "x"}), encoding="utf-8")
        self.assertEqual(gate.check(d, WS, log=False)["code"], "INVALID_APPROVAL")

    def test_blocked_when_approval_is_for_another_assessment(self):
        d = self.fresh_out()
        workflow.record_review(self.ctx_for(d), "Zain Zia", "all", None, None, "ok")
        rec = json.loads((d / "approval-record.json").read_text(encoding="utf-8"))
        rec["assessment_fingerprint"] = "sha256:" + "0" * 64
        (d / "approval-record.json").write_text(json.dumps(rec), encoding="utf-8")
        self.assertEqual(gate.check(d, WS, log=False)["code"], "FINGERPRINT_MISMATCH")

    def test_blocked_when_findings_are_edited_after_approval(self):
        d = self.fresh_out()
        workflow.record_review(self.ctx_for(d), "Zain Zia", "all", None, None, "ok")
        a = json.loads((d / "assessment.json").read_text(encoding="utf-8"))
        a["findings"][0]["risk"] = "Low" if a["findings"][0]["risk"] != "Low" else "High"
        (d / "assessment.json").write_text(json.dumps(a), encoding="utf-8")
        self.assertEqual(gate.check(d, WS, log=False)["code"], "FINGERPRINT_MISMATCH")

    def test_blocked_when_an_asset_changes_after_approval(self):
        ws = Path(tempfile.mkdtemp(prefix="regdelta-ws-"))
        self.addCleanup(shutil.rmtree, ws, True)
        shutil.copytree(WS, ws / "bank")
        d = Path(tempfile.mkdtemp(prefix="regdelta-gate-"))
        self.addCleanup(shutil.rmtree, d, True)
        c = Ctx(workspace=str(ws / "bank"), out=str(d), actor="test")
        workflow.map_assets(c)
        workflow.record_review(c, "Zain Zia", "all", None, None, "ok")
        self.assertTrue(gate.check(d, ws / "bank", log=False)["allowed"])
        f = ws / "bank" / "sql" / "03_rpt_l01.sql"
        f.write_text(f.read_text(encoding="utf-8") + "\n-- edited\n", encoding="utf-8")
        self.assertEqual(gate.check(d, ws / "bank", log=False)["code"], "FINGERPRINT_MISMATCH")

    def test_agents_cannot_approve(self):
        d = self.fresh_out()
        with self.assertRaises(SystemExit):
            workflow.record_review(self.ctx_for(d), "remediation-planner", "all", None, None, "")
        workflow.record_review(self.ctx_for(d), "Zain Zia", "all", None, None, "ok")
        rec = json.loads((d / "approval-record.json").read_text(encoding="utf-8"))
        rec["reviewer"] = "asset-mapper"
        (d / "approval-record.json").write_text(json.dumps(rec), encoding="utf-8")
        self.assertEqual(gate.check(d, WS, log=False)["code"], "NOT_A_HUMAN_REVIEWER")

    def test_incomplete_decisions_block(self):
        d = self.fresh_out()
        workflow.record_review(self.ctx_for(d), "Zain Zia", "all", None, None, "ok")
        rec = json.loads((d / "approval-record.json").read_text(encoding="utf-8"))
        rec["decisions"].pop("F-001")
        (d / "approval-record.json").write_text(json.dumps(rec), encoding="utf-8")
        self.assertEqual(gate.check(d, WS, log=False)["code"], "INCOMPLETE_DECISIONS")

    def test_valid_approval_opens_gate_and_plan_follows_decisions(self):
        d = self.fresh_out()
        cand = [f["id"] for f in self.findings if f["impact_type"] == "candidate"]
        workflow.record_review(self.ctx_for(d), "Zain Zia", "all-evidenced", cand[0], ",".join(cand[1:]), "msg")
        r = gate.check(d, WS, log=False)
        self.assertTrue(r["allowed"], r)
        p = workflow.plan(self.ctx_for(d))
        self.assertTrue(p["allowed"])
        b = p["buckets"]
        self.assertEqual({i["id"] for i in b["excluded"]}, {cand[0]})
        self.assertEqual({i["id"] for i in b["monitor"]}, set(cand[1:]))
        evidenced = {f["id"] for f in self.findings if f["impact_type"] != "candidate"}
        self.assertEqual({i["id"] for i in b["must"] + b["should"]}, evidenced)
        self.assertTrue(all(i["risk"] in ("Critical", "High") for i in b["must"]))
        self.assertTrue(all(i["owner"] and i["owner"] != "Unassigned" for i in b["must"] + b["should"]))
        self.assertTrue((d / "remediation-plan.md").exists())
        seq = p["rerun_sequence"]
        self.assertLess(seq.index("sql/01_stg_loans.sql"), seq.index("sql/03_rpt_l01.sql"))
        self.assertLess(seq.index("sql/03_rpt_l01.sql"), seq.index("sql/05_mart_risk_summary.sql"))


class TestNoProductionMutation(Base):
    def test_workspace_untouched(self):
        d = self.fresh_out()
        workflow.record_review(Ctx(workspace=str(WS), out=str(d)), "Zain Zia", "all", None, None, "ok")
        workflow.plan(Ctx(workspace=str(WS), out=str(d)))
        from regdelta.ui import build_ui
        build_ui(Ctx(workspace=str(WS), out=str(d)))
        self.assertEqual(tree_hash(WS), self.before)


if __name__ == "__main__":
    unittest.main()

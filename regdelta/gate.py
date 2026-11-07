"""The approval gate.

The Remediation Planner may only produce the final plan when a valid human
approval record exists for *this exact* assessment. The gate recomputes the
assessment fingerprint from what is on disk right now (specs, every scanned
asset, every finding), so an approval given for an older or edited
assessment does not open it.

Prototype control, not production security: a real deployment needs signed
approvals, identity from the chat platform and tamper-resistant storage.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

APPROVAL_SCHEMA = "regdelta.approval/v1"
DECISIONS = {"confirmed", "rejected", "needs_investigation"}
AGENT_NAMES = {"spec-analyst", "specification-analyst", "asset-mapper", "remediation-planner", "regdelta", "agent", "claude"}


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def fingerprint(spec_hashes: dict, asset_hashes: dict, findings: list[dict]) -> str:
    core = [
        {k: f[k] for k in ("id", "change_id", "asset", "impact_type", "risk")} | {
            "evidence": [(e["file"], e["line"]) for e in f["evidence"]]
        }
        for f in findings
    ]
    return "sha256:" + sha256_text(canonical({"specs": spec_hashes, "assets": asset_hashes, "findings": core}))


def current_fingerprint(assessment: dict, workspace: Path) -> str:
    """Recompute from disk: the spec and asset files as they are *now*."""
    spec_hashes = {}
    for key in ("old_spec", "new_spec"):
        p = workspace / assessment["meta"][key]
        spec_hashes[assessment["meta"][key]] = hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else "missing"
    asset_hashes = {}
    for rel in assessment["meta"]["asset_hashes"]:
        p = workspace / rel
        asset_hashes[rel] = hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else "missing"
    return fingerprint(spec_hashes, asset_hashes, assessment["findings"])


def _log(out: Path, result: dict, actor: str):
    out.mkdir(parents=True, exist_ok=True)
    with (out / "gate-log.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "actor": actor, **result}) + "\n")


def check(out: Path, workspace: Path, actor: str = "cli", log: bool = True) -> dict:
    out, workspace = Path(out), Path(workspace)

    def result(allowed, code, message, **extra):
        r = {"allowed": allowed, "code": code, "message": message, **extra}
        if log:
            _log(out, r, actor)
        return r

    a_path = out / "assessment.json"
    if not a_path.exists():
        return result(False, "NO_ASSESSMENT", "No impact assessment found. Run `regdelta map` first.")
    assessment = json.loads(a_path.read_text(encoding="utf-8"))
    current = current_fingerprint(assessment, workspace)

    r_path = out / "approval-record.json"
    if not r_path.exists():
        return result(False, "NO_APPROVAL", "No approval record. The Human Reporting Owner must review the findings first.",
                      fingerprint=current)
    try:
        rec = json.loads(r_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return result(False, "INVALID_APPROVAL", f"Approval record is not valid JSON ({e.msg}).", fingerprint=current)
    missing = [k for k in ("schema", "assessment_fingerprint", "reviewer", "decided_at", "decisions") if k not in rec]
    if missing or rec.get("schema") != APPROVAL_SCHEMA or not isinstance(rec.get("decisions"), dict):
        return result(False, "INVALID_APPROVAL",
                      f"Approval record is malformed ({'missing ' + ', '.join(missing) if missing else 'wrong schema'}).",
                      fingerprint=current)
    reviewer = str(rec.get("reviewer", "")).strip()
    if not reviewer or reviewer.lower() in AGENT_NAMES:
        return result(False, "NOT_A_HUMAN_REVIEWER", f"Reviewer '{reviewer}' is not a human reporting owner.", fingerprint=current)
    if rec["assessment_fingerprint"] != current:
        return result(False, "FINGERPRINT_MISMATCH",
                      "The approval was given for a different assessment (findings, specs or assets changed since). Re-run the review.",
                      fingerprint=current, approved_fingerprint=rec["assessment_fingerprint"])
    ids = {f["id"] for f in assessment["findings"]}
    decided = set(rec["decisions"])
    bad = sorted(v for v in rec["decisions"].values() if v not in DECISIONS)
    if bad or decided != ids:
        return result(False, "INCOMPLETE_DECISIONS",
                      f"Every finding needs one decision. Missing: {sorted(ids - decided)[:5]} Unknown: {sorted(decided - ids)[:5]} Invalid: {bad[:3]}",
                      fingerprint=current)
    return result(True, "OK", f"Approved by {reviewer} at {rec['decided_at']}.", fingerprint=current, reviewer=reviewer)

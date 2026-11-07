"""The RegDelta workflow: analyze -> map -> review -> plan.

Each step reads and writes plain files in the output folder, so the three
Switch agents (each a separate Claude Code session) share state through the
same working directory, and every artifact can be posted into the room.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from . import gate
from .impact import IMPACT_LABEL, IMPACT_ORDER, RISK_ORDER, build_graph, map_impacts
from .scanner import KIND_LABEL, load_dictionary, scan_workspace
from .specdiff import load_spec, diff_specs

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = {
    "workspace": "fixtures/harbourview-bank",
    "old_spec": "specs/prf-3.1.json",
    "new_spec": "specs/prf-3.2.json",
    "out": "out",
}


class Ctx:
    def __init__(self, workspace=None, old=None, new=None, out=None, actor="cli"):
        cfg = dict(DEFAULT_CONFIG)
        cfg_file = ROOT / "regdelta.json"
        if cfg_file.exists():
            cfg.update(json.loads(cfg_file.read_text(encoding="utf-8")))
        self.workspace = self._abs(workspace, cfg["workspace"])
        self.old_rel = old or cfg["old_spec"]
        self.new_rel = new or cfg["new_spec"]
        self.out = self._abs(out, cfg["out"])
        self.actor = actor

    @staticmethod
    def _abs(given, default) -> Path:
        if given:
            p = Path(given)
            return p if p.is_absolute() else (Path.cwd() / p).resolve()
        p = Path(default)
        return p if p.is_absolute() else ROOT / p


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log_event(ctx: Ctx, event: str, detail: str):
    ctx.out.mkdir(parents=True, exist_ok=True)
    with (ctx.out / "events.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": now(), "actor": ctx.actor, "event": event, "detail": detail}) + "\n")


def _write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


# ----------------------------------------------------------------- analyze
def analyze(ctx: Ctx) -> dict:
    old = load_spec(ctx.workspace / ctx.old_rel)
    new = load_spec(ctx.workspace / ctx.new_rel)
    changes = diff_specs(old, new, ctx.old_rel, ctx.new_rel)
    meta = {
        "framework": new.get("framework_name", new.get("framework")),
        "authority": new.get("authority"),
        "old_version": old["version"], "new_version": new["version"],
        "effective_date": new.get("effective_date"),
        "old_spec": ctx.old_rel, "new_spec": ctx.new_rel,
        "workspace": ctx.workspace.name, "generated_at": now(),
    }
    register = {"meta": meta, "changes": [{k: v for k, v in c.items() if k not in ("field", "old_field", "rule", "old_rule")} | {
        "field": c.get("field"), "old_field": c.get("old_field")} for c in changes]}
    _write(ctx.out / "change-register.json", json.dumps(register, indent=2))
    _write(ctx.out / "change-register.md", render_change_register(register))
    log_event(ctx, "analyze", f"{len(changes)} changes between {ctx.old_rel} and {ctx.new_rel}")
    return register


def render_change_register(reg: dict) -> str:
    m = reg["meta"]
    out = [f"# Change Register — {m['framework']} {m['old_version']} → {m['new_version']}", "",
           f"Authority: {m['authority']}  ", f"Effective date: **{m['effective_date']}**  ",
           f"Compared: `{m['old_spec']}` → `{m['new_spec']}`", "",
           "> Synthetic demonstration data. Not a real regulatory notice.", "",
           "| ID | Change | Subject | Old | New | Spec reference |", "|---|---|---|---|---|---|"]
    for c in reg["changes"]:
        r = c["spec_ref"]
        out.append(f"| {c['id']} | {c['label']} | `{c['subject']}` | {c['old'] or '—'} | {c['new'] or '—'} | `{r['file']}:{r['line']}` ({r.get('section') or ''}) |")
    out += ["", "## In plain language", ""]
    for c in reg["changes"]:
        out.append(f"- **{c['id']} {c['label']} — {c['subject']}.** {c['plain']}")
    return "\n".join(out) + "\n"


# ----------------------------------------------------------------- map
def map_assets(ctx: Ctx) -> dict:
    reg_path = ctx.out / "change-register.json"
    if not reg_path.exists():
        analyze(ctx)
    reg = read_json(reg_path)
    old = load_spec(ctx.workspace / ctx.old_rel)
    new = load_spec(ctx.workspace / ctx.new_rel)
    changes = diff_specs(old, new, ctx.old_rel, ctx.new_rel)   # full objects incl. fields
    assets = scan_workspace(ctx.workspace)
    dictionary = load_dictionary(ctx.workspace)
    findings = map_impacts(changes, assets, dictionary)
    graph = build_graph(changes, findings, assets)

    meta = dict(reg["meta"])
    meta["asset_hashes"] = {p: a.sha256 for p, a in sorted(assets.items())}
    meta["assets_scanned"] = len(assets)
    meta["mapped_at"] = now()
    spec_hashes = {r: hashlib.sha256((ctx.workspace / r).read_bytes()).hexdigest() for r in (ctx.old_rel, ctx.new_rel)}
    fp = gate.fingerprint(spec_hashes, meta["asset_hashes"], findings)
    meta["fingerprint"] = fp
    inventory = [{"path": p, "kind": a.kind, "kind_label": KIND_LABEL.get(a.kind, a.kind), "owner": a.owner,
                  "affected": any(f["asset"] == p for f in findings)} for p, a in sorted(assets.items())]
    assessment = {"meta": meta, "changes": reg["changes"], "findings": findings, "graph": graph, "inventory": inventory}
    _write(ctx.out / "assessment.json", json.dumps(assessment, indent=2))
    _write(ctx.out / "impact-matrix.csv", render_matrix_csv(findings))
    _write(ctx.out / "dependency-graph.json", json.dumps(graph, indent=2))
    _write(ctx.out / "dependency-map.md", render_dependency_map(assessment))
    _write(ctx.out / "review-sheet.md", render_review_sheet(assessment))
    # a fresh assessment invalidates any earlier plan
    for stale in ("remediation-plan.md", "plan.json"):
        if (ctx.out / stale).exists() and not _approval_matches(ctx, fp):
            (ctx.out / stale).unlink()
    c = summary_counts(findings)
    log_event(ctx, "map", f"{len(findings)} findings across {c['assets']} assets ({c['by_type'].get('candidate', 0)} candidates); fingerprint {fp[:19]}")
    return assessment


def _approval_matches(ctx, fp) -> bool:
    p = ctx.out / "approval-record.json"
    try:
        return read_json(p).get("assessment_fingerprint") == fp
    except Exception:
        return False


def summary_counts(findings: list[dict]) -> dict:
    by_type, by_risk = defaultdict(int), defaultdict(int)
    for f in findings:
        by_type[f["impact_type"]] += 1
        by_risk[f["risk"]] += 1
    return {"findings": len(findings), "assets": len({f["asset"] for f in findings}),
            "by_type": dict(by_type), "by_risk": dict(by_risk)}


MATRIX_COLUMNS = ["Finding ID", "Change ID", "Requirement", "Affected asset", "Asset kind", "Owner", "Evidence location",
                  "Evidence", "Impact type", "Risk level", "Confidence", "Review status", "Lineage path", "Proposed action"]


def render_matrix_csv(findings: list[dict], decisions: dict | None = None) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(MATRIX_COLUMNS)
    for f in findings:
        e = f["evidence"][0]
        status = (decisions or {}).get(f["id"], f["review_status"])
        w.writerow([f["id"], f["change_id"], f["requirement"], f["asset"], f["asset_kind_label"], f["owner"],
                    f'{e["file"]}:{e["line"]}', e["text"] + (f' [{e["note"]}]' if e.get("note") else ""),
                    f["impact_label"], f["risk"], f["confidence"], status, " -> ".join(f["via"]), f["action"]])
    return buf.getvalue()


def render_dependency_map(a: dict) -> str:
    out = [f"# Dependency Map — {a['meta']['framework']} {a['meta']['old_version']} → {a['meta']['new_version']}", "",
           "Solid arrows are deterministic evidence (exact reference or lineage). Dotted arrows are AI/semantic candidates that need a human.", "",
           "```mermaid", "flowchart LR"]
    ids = {}

    def nid(x):
        if x not in ids:
            ids[x] = f"n{len(ids)}"
        return ids[x]

    for n in a["graph"]["nodes"]:
        label = n["label"].replace('"', "'")
        if n["type"] == "change":
            out.append(f'  {nid(n["id"])}{{{{"{n["id"]}<br/>{label}"}}}}')
        elif n["type"] == "table":
            out.append(f'  {nid(n["id"])}[("{label}")]')
        else:
            out.append(f'  {nid(n["id"])}["{label}<br/><small>{n["sub"]}</small>"]')
    for e in a["graph"]["edges"]:
        arrow = "-.->" if e["kind"] == "candidate" else "-->"
        lbl = e["kind"] if e["kind"] in ("writes", "reads", "imports") else e.get("finding", e["kind"])
        out.append(f'  {nid(e["from"])} {arrow}|{lbl}| {nid(e["to"])}')
    out += ["```", "", "## Blast radius per change", ""]
    for c in a["changes"]:
        fs = [f for f in a["findings"] if f["change_id"] == c["id"]]
        out.append(f"### {c['id']} {c['label']} — `{c['subject']}` ({len(fs)} findings)")
        for f in fs:
            path = " → ".join(f["via"]) if f["via"] else f["asset"]
            e = f["evidence"][0]
            out.append(f"- `{f['id']}` **{f['impact_label']}** · {f['risk']} · `{f['asset']}` — evidence `{e['file']}:{e['line']}`" +
                       (f" — path: {path}" if f["via"] else ""))
        out.append("")
    return "\n".join(out)


def render_review_sheet(a: dict) -> str:
    c = summary_counts(a["findings"])
    evid = [f["id"] for f in a["findings"] if f["impact_type"] != "candidate"]
    cand = [f["id"] for f in a["findings"] if f["impact_type"] == "candidate"]
    out = ["# Review sheet for the Human Reporting Owner", "",
           f"Assessment fingerprint: `{a['meta']['fingerprint']}`", "",
           f"{c['findings']} findings on {c['assets']} assets. {len(evid)} have deterministic evidence; {len(cand)} are semantic candidates.", "",
           "Reply in the Switch room, for example:", "",
           "```text", "@remediation-planner confirm all evidenced, reject F-0xx, investigate F-0yy", "```", "",
           "| ID | Change | Asset | Type | Risk | Evidence |", "|---|---|---|---|---|---|"]
    for f in a["findings"]:
        e = f["evidence"][0]
        out.append(f"| {f['id']} | {f['change_id']} | `{f['asset']}` | {f['impact_label']} | {f['risk']} | `{e['file']}:{e['line']}` |")
    return "\n".join(out) + "\n"


# ----------------------------------------------------------------- review
def parse_ids(spec: str | None, findings: list[dict]) -> set[str]:
    if not spec:
        return set()
    ids = set()
    known = {f["id"] for f in findings}
    for tok in spec.replace(";", ",").replace(" ", ",").split(","):
        tok = tok.strip().upper()
        if not tok:
            continue
        if tok in ("ALL",):
            ids |= known
        elif tok in ("ALL-EVIDENCED", "EVIDENCED", "ALL-DIRECT", "DIRECT"):
            ids |= {f["id"] for f in findings if f["impact_type"] != "candidate"}
        elif tok in ("CANDIDATES", "ALL-CANDIDATES"):
            ids |= {f["id"] for f in findings if f["impact_type"] == "candidate"}
        elif tok.startswith("CHG-"):
            ids |= {f["id"] for f in findings if f["change_id"] == tok}
        else:
            if not tok.startswith("F-"):
                tok = "F-" + tok.zfill(3)
            if tok not in known:
                raise ValueError(f"Unknown finding id {tok}")
            ids.add(tok)
    return ids


def record_review(ctx: Ctx, reviewer: str, confirm: str | None, reject: str | None, investigate: str | None,
                  message: str | None, default: str = "needs_investigation") -> dict:
    a_path = ctx.out / "assessment.json"
    if not a_path.exists():
        raise SystemExit("No assessment yet. Run `regdelta map` first.")
    a = read_json(a_path)
    findings = a["findings"]
    if gate.is_agent_name(reviewer):
        raise SystemExit("The reviewer must be the human reporting owner (use their name), not an agent.")
    c, r, i = parse_ids(confirm, findings), parse_ids(reject, findings), parse_ids(investigate, findings)
    decisions = {}
    for f in findings:
        fid = f["id"]
        # the most specific/conservative instruction wins: reject > investigate > confirm
        decisions[fid] = "rejected" if fid in r else "needs_investigation" if fid in i else "confirmed" if fid in c else default
    fp = gate.current_fingerprint(a, ctx.workspace)
    rec = {
        "schema": gate.APPROVAL_SCHEMA,
        "assessment_fingerprint": fp,
        "reviewer": reviewer.strip(),
        "reviewer_role": "Human Reporting Owner",
        "decided_at": now(),
        "recorded_by": ctx.actor,
        "source": {"channel": "switch-room", "message": message or ""},
        "decisions": decisions,
        "summary": {k: sum(1 for v in decisions.values() if v == k) for k in sorted(gate.DECISIONS)},
    }
    _write(ctx.out / "approval-record.json", json.dumps(rec, indent=2))
    _write(ctx.out / "impact-matrix.csv", render_matrix_csv(findings, decisions))
    s = rec["summary"]
    log_event(ctx, "review", f"{reviewer} reviewed {len(decisions)} findings: {s['confirmed']} confirmed, {s['rejected']} rejected, {s['needs_investigation']} to investigate")
    return rec


# ----------------------------------------------------------------- plan
def plan(ctx: Ctx) -> dict:
    res = gate.check(ctx.out, ctx.workspace, actor=ctx.actor)
    if not res["allowed"]:
        log_event(ctx, "plan_blocked", f"{res['code']}: {res['message']}")
        return {"allowed": False, "gate": res}
    a = read_json(ctx.out / "assessment.json")
    rec = read_json(ctx.out / "approval-record.json")
    dec = rec["decisions"]
    buckets = {"must": [], "should": [], "monitor": [], "excluded": []}
    for f in a["findings"]:
        d = dec[f["id"]]
        item = {k: f[k] for k in ("id", "change_id", "requirement", "asset", "asset_kind_label", "owner", "impact_label",
                                   "impact_type", "risk", "confidence", "action", "rationale", "via")}
        item["evidence"] = f'{f["evidence"][0]["file"]}:{f["evidence"][0]["line"]}'
        item["decision"] = d
        if d == "rejected":
            buckets["excluded"].append(item)
        elif d == "needs_investigation":
            buckets["monitor"].append(item)
        elif f["risk"] in ("Critical", "High"):
            buckets["must"].append(item)
        else:
            buckets["should"].append(item)
    for b in buckets.values():
        b.sort(key=lambda x: (RISK_ORDER.index(x["risk"]), x["change_id"], x["id"]))

    # rerun order: confirmed transformations/pipelines in lineage order
    confirmed_assets = {i["asset"] for i in buckets["must"] + buckets["should"]}
    sequence = _rerun_sequence(a, confirmed_assets)
    owners = defaultdict(lambda: {"must": 0, "should": 0, "monitor": 0})
    for k in ("must", "should", "monitor"):
        for i in buckets[k]:
            owners[i["owner"]][k] += 1
    p = {"allowed": True, "gate": res, "approved_by": rec["reviewer"], "approved_at": rec["decided_at"],
         "fingerprint": rec["assessment_fingerprint"], "effective_date": a["meta"]["effective_date"],
         "buckets": buckets, "rerun_sequence": sequence, "owners": dict(owners), "generated_at": now()}
    _write(ctx.out / "plan.json", json.dumps(p, indent=2))
    _write(ctx.out / "remediation-plan.md", render_plan(a, p))
    log_event(ctx, "plan", f"Plan generated: {len(buckets['must'])} must-fix, {len(buckets['should'])} should-fix, {len(buckets['monitor'])} monitor")
    return p


def _rerun_sequence(a: dict, confirmed: set[str]) -> list[str]:
    edges = defaultdict(set)
    for e in a["graph"]["edges"]:
        if e["kind"] in ("writes", "reads", "imports"):
            edges[e["from"]].add(e["to"])
    # asset -> asset through tables
    order_nodes = [n["id"] for n in a["graph"]["nodes"] if n["type"] == "asset" and n["id"] in confirmed
                   and n["kind"] in ("transformation", "pipeline")]
    deps = {x: set() for x in order_nodes}
    for x in order_nodes:
        for y in order_nodes:
            if x != y and _reaches(edges, x, y):
                deps[y].add(x)
    out, done = [], set()
    while len(out) < len(order_nodes):
        ready = sorted(x for x in order_nodes if x not in done and deps[x] <= done)
        if not ready:
            out += sorted(set(order_nodes) - done)
            break
        out += ready
        done |= set(ready)
    return out


def _reaches(edges, a, b, seen=None) -> bool:
    seen = seen or set()
    for n in edges.get(a, ()):
        if n == b:
            return True
        if n not in seen:
            seen.add(n)
            if _reaches(edges, n, b, seen):
                return True
    return False


def render_plan(a: dict, p: dict) -> str:
    m = a["meta"]
    b = p["buckets"]
    out = [f"# Remediation Plan — {m['framework']} {m['old_version']} → {m['new_version']}", "",
           f"Approved by **{p['approved_by']}** (Human Reporting Owner) at {p['approved_at']}  ",
           f"Assessment fingerprint: `{p['fingerprint']}`  ",
           f"Deadline: before the first reporting cycle under {m['new_version']} (effective **{m['effective_date']}**)", "",
           "> RegDelta does not change any system. This is a plan for the responsible teams.", ""]
    titles = {"must": "Must fix before the next reporting cycle", "should": "Should fix", "monitor": "Monitor / investigate"}
    for k in ("must", "should", "monitor"):
        out += [f"## {titles[k]} ({len(b[k])})", ""]
        if not b[k]:
            out += ["_Nothing in this group._", ""]
            continue
        out += ["| # | Finding | Risk | Asset | Action | Evidence | Owner |", "|---|---|---|---|---|---|---|"]
        for n, i in enumerate(b[k], 1):
            out.append(f"| {n} | {i['id']} ({i['change_id']}) | {i['risk']} | `{i['asset']}` | {i['action']} | `{i['evidence']}` | {i['owner']} |")
        out.append("")
    if p["rerun_sequence"]:
        out += ["## Re-run order after the fixes", "", "Rebuild in lineage order so every table is refreshed after its inputs:", ""]
        out += [f"{n}. `{s}`" for n, s in enumerate(p["rerun_sequence"], 1)] + [""]
    out += ["## Work by owner", "", "| Owner | Must | Should | Monitor |", "|---|---|---|---|"]
    for o, c in sorted(p["owners"].items()):
        out.append(f"| {o} | {c['must']} | {c['should']} | {c['monitor']} |")
    if b["excluded"]:
        out += ["", "## Excluded by the reviewer", ""]
        out += [f"- {i['id']} `{i['asset']}` — {i['impact_label']} (rejected)" for i in b["excluded"]]
    return "\n".join(out) + "\n"


# ----------------------------------------------------------------- status
def status_text(ctx: Ctx) -> str:
    lines = []
    a_path = ctx.out / "assessment.json"
    if not (ctx.out / "change-register.json").exists():
        return "Nothing analysed yet. Run `python -m regdelta analyze`."
    reg = read_json(ctx.out / "change-register.json")
    m = reg["meta"]
    lines.append(f"**{m['framework']} {m['old_version']} → {m['new_version']}** (effective {m['effective_date']}) — {len(reg['changes'])} changes")
    if a_path.exists():
        a = read_json(a_path)
        c = summary_counts(a["findings"])
        t = c["by_type"]
        lines.append(f"{c['findings']} findings on {c['assets']} of {a['meta']['assets_scanned']} assets: "
                     f"{t.get('gap', 0)} missing-element, {t.get('direct', 0)} direct, {t.get('upstream', 0)} upstream, "
                     f"{t.get('downstream', 0)} downstream, {t.get('candidate', 0)} semantic candidates")
        r = c["by_risk"]
        lines.append("Risk: " + ", ".join(f"{k} {r.get(k, 0)}" for k in RISK_ORDER))
        g = gate.check(ctx.out, ctx.workspace, actor=ctx.actor, log=False)
        lines.append(("Gate: OPEN — " if g["allowed"] else f"Gate: BLOCKED ({g['code']}) — ") + g["message"])
    return "\n".join(lines)

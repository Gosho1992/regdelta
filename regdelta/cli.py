"""Command line for RegDelta. Every Switch agent drives the workflow with these commands."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from . import __version__, gate, workflow
from .workflow import Ctx


def _utf8():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _ctx(args) -> Ctx:
    return Ctx(workspace=args.workspace, old=args.old, new=args.new, out=args.out,
               actor=args.actor or os.environ.get("REGDELTA_ACTOR", "cli"))


def cmd_analyze(args):
    ctx = _ctx(args)
    reg = workflow.analyze(ctx)
    print(f"Change Register: {len(reg['changes'])} changes ({reg['meta']['old_version']} -> {reg['meta']['new_version']}, effective {reg['meta']['effective_date']})")
    for c in reg["changes"]:
        r = c["spec_ref"]
        print(f"  {c['id']}  {c['label']:<24} {c['subject']:<16} [{r['file']}:{r['line']}]")
    print(f"Wrote {ctx.out / 'change-register.md'}")


def cmd_map(args):
    ctx = _ctx(args)
    a = workflow.map_assets(ctx)
    c = workflow.summary_counts(a["findings"])
    print(f"Impact Matrix: {c['findings']} findings on {c['assets']} of {a['meta']['assets_scanned']} assets")
    for t in ("gap", "direct", "upstream", "downstream", "candidate"):
        print(f"  {workflow.IMPACT_LABEL[t]:<26} {c['by_type'].get(t, 0)}")
    print("  Risk: " + ", ".join(f"{k} {c['by_risk'].get(k, 0)}" for k in workflow.RISK_ORDER))
    print(f"Fingerprint: {a['meta']['fingerprint']}")
    print(f"Wrote impact-matrix.csv, dependency-map.md, dependency-graph.json, review-sheet.md in {ctx.out}")
    if args.verbose:
        for f in a["findings"]:
            e = f["evidence"][0]
            print(f"  {f['id']} {f['change_id']} {f['impact_type']:<10} {f['risk']:<8} {f['asset']:<42} {e['file']}:{e['line']}")


def cmd_review(args):
    ctx = _ctx(args)
    try:
        rec = workflow.record_review(ctx, args.reviewer, args.confirm, args.reject, args.investigate, args.message,
                                     default=args.default)
    except ValueError as e:
        print(f"Review not recorded: {e}")
        return 2
    s = rec["summary"]
    print(f"Approval record written for {rec['reviewer']}: {s['confirmed']} confirmed, {s['rejected']} rejected, {s['needs_investigation']} to investigate")
    print(f"Bound to assessment {rec['assessment_fingerprint']}")
    return 0


def cmd_gate(args):
    ctx = _ctx(args)
    r = gate.check(ctx.out, ctx.workspace, actor=ctx.actor)
    print(("ALLOWED  " if r["allowed"] else "BLOCKED  ") + f"[{r['code']}] {r['message']}")
    return 0 if r["allowed"] else 1


def cmd_plan(args):
    ctx = _ctx(args)
    p = workflow.plan(ctx)
    if not p["allowed"]:
        g = p["gate"]
        print(f"PLAN BLOCKED by approval gate [{g['code']}]: {g['message']}")
        print("The Remediation Planner cannot produce the final plan until the Human Reporting Owner approves this assessment.")
        return 2
    b = p["buckets"]
    print(f"Gate OPEN — {p['gate']['message']}")
    print(f"Remediation Plan: {len(b['must'])} must-fix, {len(b['should'])} should-fix, {len(b['monitor'])} monitor, {len(b['excluded'])} excluded")
    print(f"Wrote {ctx.out / 'remediation-plan.md'}")
    return 0


def cmd_status(args):
    print(workflow.status_text(_ctx(args)))


def cmd_ui(args):
    from .ui import build_ui
    ctx = _ctx(args)
    path = build_ui(ctx, Path(args.html) if args.html else None)
    print(f"Impact Room UI: {path}")
    if args.open:
        import webbrowser
        webbrowser.open(path.resolve().as_uri())


def cmd_demo(args):
    """Full blocked -> approved -> tamper-detected run in a scratch output folder."""
    out = Path(args.out) if args.out else Path(tempfile.mkdtemp(prefix="regdelta-demo-"))
    if out.exists() and args.out:
        shutil.rmtree(out)
    base = dict(workspace=args.workspace, old=args.old, new=args.new, out=str(out))
    step = lambda n, t: print(f"\n=== {n}. {t} ===")
    step(1, "Specification Analyst compares PRF 3.1 with 3.2")
    reg = workflow.analyze(Ctx(**base, actor="spec-analyst"))
    for c in reg["changes"]:
        print(f"  {c['id']} {c['label']}: {c['subject']}")
    step(2, "Asset Mapper traces the changes through the reporting stack")
    a = workflow.map_assets(Ctx(**base, actor="asset-mapper"))
    c = workflow.summary_counts(a["findings"])
    print(f"  {c['findings']} findings on {c['assets']} assets; {c['by_type'].get('candidate', 0)} semantic candidates kept separate")
    step(3, "Remediation Planner tries to write the plan before any human review")
    p = workflow.plan(Ctx(**base, actor="remediation-planner"))
    print(f"  -> {'ALLOWED' if p['allowed'] else 'BLOCKED'} [{p['gate']['code']}] {p['gate']['message']}")
    assert not p["allowed"]
    step(4, "Human Reporting Owner reviews in the Switch room and approves")
    cand = [f["id"] for f in a["findings"] if f["impact_type"] == "candidate"]
    rej = [f["id"] for f in a["findings"] if f["impact_type"] == "candidate" and f["asset"].endswith("board_kpi_pack.yaml")]
    msg = f"confirm all evidenced, investigate {', '.join(x for x in cand if x not in rej)}, reject {', '.join(rej)}"
    workflow.record_review(Ctx(**base, actor="remediation-planner"), args.reviewer, "all-evidenced", ",".join(rej),
                           ",".join(x for x in cand if x not in rej), f"@remediation-planner {msg}")
    print(f"  {args.reviewer}: \"{msg}\"")
    step(5, "Remediation Planner tries again")
    p = workflow.plan(Ctx(**base, actor="remediation-planner"))
    print(f"  -> {'ALLOWED' if p['allowed'] else 'BLOCKED'} [{p['gate']['code']}] {p['gate']['message']}")
    b = p["buckets"]
    print(f"  Plan: {len(b['must'])} must-fix, {len(b['should'])} should-fix, {len(b['monitor'])} monitor")
    step(6, "Tamper check: edit one finding after approval, in a copy")
    tmp = Path(tempfile.mkdtemp(prefix="regdelta-tamper-"))
    for f in ("assessment.json", "approval-record.json"):
        shutil.copy(out / f, tmp / f)
    t = json.loads((tmp / "assessment.json").read_text(encoding="utf-8"))
    victim = next(f for f in t["findings"] if f["risk"] == "Critical")
    victim["risk"] = "Low"
    print(f"  Downgrading {victim['id']} ({victim['asset']}) from Critical to Low after approval...")
    (tmp / "assessment.json").write_text(json.dumps(t), encoding="utf-8")
    r = gate.check(tmp, Ctx(**base).workspace, actor="tamper-test", log=False)
    print(f"  -> {'ALLOWED' if r['allowed'] else 'BLOCKED'} [{r['code']}] {r['message']}")
    shutil.rmtree(tmp, ignore_errors=True)
    from .ui import build_ui
    path = build_ui(Ctx(**base))
    print(f"\nDone. Artifacts in {out}\nOpen the Impact Room: {path}")


def main(argv=None):
    _utf8()
    ap = argparse.ArgumentParser(prog="regdelta", description="RegDelta — regulatory reporting impact assessment")
    ap.add_argument("--version", action="version", version=f"regdelta {__version__}")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--workspace", help="folder with the organisation's reporting assets")
    common.add_argument("--old", help="previous spec, relative to the workspace")
    common.add_argument("--new", help="revised spec, relative to the workspace")
    common.add_argument("--out", help="output folder (default: out/)")
    common.add_argument("--actor", help="who is running this step (agent or person), for the audit trail")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("analyze", parents=[common], help="Stage 1: build the Change Register").set_defaults(fn=cmd_analyze)
    m = sub.add_parser("map", parents=[common], help="Stages 2-3: Impact Matrix + Dependency Map")
    m.add_argument("-v", "--verbose", action="store_true")
    m.set_defaults(fn=cmd_map)
    r = sub.add_parser("review", parents=[common], help="Stage 4: record the human owner's decisions")
    r.add_argument("--reviewer", required=True, help="name of the Human Reporting Owner")
    r.add_argument("--confirm", help="ids, CHG-xx, 'all-evidenced', 'candidates' or 'all'")
    r.add_argument("--reject", help="ids to reject")
    r.add_argument("--investigate", help="ids that need further investigation")
    r.add_argument("--message", help="the owner's message in the Switch room, verbatim")
    r.add_argument("--default", default="needs_investigation", choices=sorted(gate.DECISIONS),
                   help="decision for findings not mentioned (default: needs_investigation)")
    r.set_defaults(fn=cmd_review)
    sub.add_parser("gate", parents=[common], help="Check the approval gate (exit 0 = open)").set_defaults(fn=cmd_gate)
    sub.add_parser("plan", parents=[common], help="Stage 5: remediation plan (blocked without approval)").set_defaults(fn=cmd_plan)
    sub.add_parser("status", parents=[common], help="One-paragraph status for the room").set_defaults(fn=cmd_status)
    u = sub.add_parser("ui", parents=[common], help="Build the Impact Room HTML page")
    u.add_argument("--html", help="where to write the page (default: <out>/index.html)")
    u.add_argument("--open", action="store_true", help="open it in the browser")
    u.set_defaults(fn=cmd_ui)
    d = sub.add_parser("demo", parents=[common], help="Run the whole workflow end to end")
    d.add_argument("--reviewer", default="Demo Reviewer")
    d.set_defaults(fn=cmd_demo)
    args = ap.parse_args(argv)
    rc = args.fn(args)
    return rc or 0


if __name__ == "__main__":
    sys.exit(main())

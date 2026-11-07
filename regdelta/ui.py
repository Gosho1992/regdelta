"""Builds the Impact Room: one self-contained HTML page (no server, no internet)."""
from __future__ import annotations

import json
from pathlib import Path

from . import gate

TEMPLATE = Path(__file__).with_name("ui_template.html")


def _jsonl(p: Path) -> list[dict]:
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def _load(p: Path):
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def build_ui(ctx, path: Path | None = None) -> Path:
    out = ctx.out
    assessment = _load(out / "assessment.json")
    register = _load(out / "change-register.json")
    if assessment is None and register is None:
        raise SystemExit("Nothing to show yet. Run `python -m regdelta analyze` and `map` first.")
    data = {
        "register": register,
        "assessment": assessment,
        "approval": _load(out / "approval-record.json"),
        "plan": _load(out / "plan.json"),
        "gate": gate.check(out, ctx.workspace, log=False) if assessment else None,
        "events": _jsonl(out / "events.jsonl"),
        "gate_log": _jsonl(out / "gate-log.jsonl"),
        "files": {},
    }
    if assessment:
        for rel in sorted({f["asset"] for f in assessment["findings"]}):
            p = ctx.workspace / rel
            if p.exists():
                data["files"][rel] = p.read_text(encoding="utf-8").splitlines()
        for key in ("old_spec", "new_spec"):
            rel = assessment["meta"][key]
            data["files"][rel] = (ctx.workspace / rel).read_text(encoding="utf-8").splitlines()
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    html = TEMPLATE.read_text(encoding="utf-8").replace("/*__REGDELTA_DATA__*/null", payload)
    target = path or (out / "index.html")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(html, encoding="utf-8")
    return target

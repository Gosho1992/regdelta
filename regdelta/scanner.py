"""Deterministic analysis engine: reads the organisation's reporting assets.

Nothing here uses an LLM. It extracts, with file and line numbers:
  * what kind of asset each file is and who owns it
  * which tables each asset writes and reads (table lineage)
  * which Python modules each asset imports (code lineage)
  * which reporting templates each asset implements
"""
from __future__ import annotations

import csv
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

SKIP_DIRS = {"specs", "__pycache__", ".git"}
SKIP_FILES = {"README.md"}
ASSET_SUFFIXES = {".sql", ".py", ".yaml", ".yml", ".json", ".csv"}

KIND_LABEL = {
    "transformation": "SQL transformation",
    "pipeline": "Python ETL",
    "regulatory_report": "Regulatory report",
    "management_report": "Management report",
    "validation_config": "Validation rules",
    "dashboard": "Dashboard",
    "export": "Regulatory export",
    "dictionary": "Data dictionary",
    "other": "Other",
}

_OWNER = re.compile(r"""owner"?\s*[:=]\s*"?([A-Za-z][^"\n,]*?)"?\s*,?\s*$""", re.I)
_KIND = re.compile(r"""^\s*"?kind"?\s*:\s*"?([a-z_]+)"?""", re.I)
_WRITES = re.compile(r"\b(?:CREATE\s+(?:OR\s+REPLACE\s+)?TABLE|INSERT\s+INTO)\s+([A-Za-z_][\w.]*)", re.I)
_READS_SQL = re.compile(r"\b(?:FROM|JOIN)\s+([A-Za-z_][\w.]*)", re.I)
_SOURCE_TABLE = re.compile(r"""source_table"?\s*:\s*"?([A-Za-z_][\w.]*)""")
_IMPORT = re.compile(r"^\s*(?:from\s+([A-Za-z_]\w*)\s+import|import\s+([A-Za-z_]\w*))")


def word_pattern(token: str, case_sensitive: bool) -> re.Pattern:
    flags = 0 if case_sensitive else re.I
    return re.compile(r"(?<![A-Za-z0-9_])" + re.escape(token) + r"(?![A-Za-z0-9_])", flags)


@dataclass
class Asset:
    path: str               # posix path relative to the workspace
    kind: str
    owner: str
    lines: list[str]
    sha256: str
    writes: dict[str, int] = field(default_factory=dict)      # table -> line
    reads: dict[str, int] = field(default_factory=dict)       # table -> line
    imports: dict[str, int] = field(default_factory=dict)     # module -> line
    templates: list[dict] = field(default_factory=list)       # {code, line, source_table}
    name: str = ""

    @property
    def module(self) -> str | None:
        return Path(self.path).stem if self.path.endswith(".py") else None

    def find(self, pattern: re.Pattern) -> list[tuple[int, str]]:
        return [(i, l) for i, l in enumerate(self.lines, start=1) if pattern.search(l)]

    def text_contains(self, pattern: re.Pattern) -> bool:
        return any(pattern.search(l) for l in self.lines)


def _detect_kind(path: Path, lines: list[str]) -> str:
    if path.name == "data-dictionary.csv":
        return "dictionary"
    for l in lines[:15]:
        m = _KIND.match(l)
        if m:
            return m.group(1).lower()
    return {".sql": "transformation", ".py": "pipeline"}.get(path.suffix, "other")


def _detect_owner(lines: list[str], kind: str) -> str:
    if kind == "dictionary":
        return "Regulatory Reporting"
    for l in lines[:15]:
        m = _OWNER.search(l.strip().lstrip("-#/ ").strip())
        if m and "owner" in l.lower():
            return m.group(1).strip().strip('"')
    return "Unassigned"


def _detect_name(lines: list[str], path: Path) -> str:
    for l in lines[:6]:
        m = re.match(r"""^\s*"?name"?\s*:\s*"?([^",]+)"?""", l)
        if m:
            return m.group(1).strip()
    return path.name


def scan_workspace(workspace: Path) -> dict[str, Asset]:
    workspace = Path(workspace)
    assets: dict[str, Asset] = {}
    for p in sorted(workspace.rglob("*")):
        if not p.is_file() or p.suffix not in ASSET_SUFFIXES:
            continue
        rel = p.relative_to(workspace)
        if rel.parts[0] in SKIP_DIRS or p.name in SKIP_FILES or any(part in SKIP_DIRS for part in rel.parts):
            continue
        raw = p.read_bytes()
        text = raw.decode("utf-8")
        lines = text.splitlines()
        kind = _detect_kind(p, lines)
        a = Asset(
            path=rel.as_posix(), kind=kind, owner=_detect_owner(lines, kind),
            lines=lines, sha256=hashlib.sha256(raw).hexdigest(),
            name=_detect_name(lines, p),
        )
        for i, l in enumerate(lines, start=1):
            code = l.split("--", 1)[0] if p.suffix == ".sql" else l
            for m in _WRITES.finditer(code):
                a.writes.setdefault(m.group(1), i)
            if p.suffix == ".py":
                m = _IMPORT.match(l)
                if m:
                    a.imports.setdefault(m.group(1) or m.group(2), i)
        assets[a.path] = a

    # Table lineage. Tables are whatever some asset writes or declares as its
    # source; an asset reads a table when it names it and does not write it.
    known = set()
    for a in assets.values():
        known.update(a.writes)
        for l in a.lines:
            m = _SOURCE_TABLE.search(l)
            if m:
                known.add(m.group(1))
    for a in assets.values():
        if a.kind == "dictionary":
            continue
        for i, l in enumerate(a.lines, start=1):
            code = l.split("--", 1)[0] if a.path.endswith(".sql") else l
            if a.path.endswith(".sql"):
                for m in _READS_SQL.finditer(code):
                    t = m.group(1)
                    if t not in a.writes:
                        a.reads.setdefault(t, i)
            for t in known:
                if t not in a.writes and t not in a.reads and word_pattern(t, True).search(code):
                    a.reads[t] = i

    # Template implementers: `template: C02`, `templates: [L01, C02]`, or a
    # top-level `C02:` section (export mappings).
    for a in assets.values():
        if a.kind in ("dictionary", "transformation", "pipeline"):
            continue
        for i, l in enumerate(a.lines, start=1):
            codes = []
            m = re.match(r"^\s*template\s*:\s*([A-Z][A-Z0-9]+)\s*$", l)
            if m:
                codes = [m.group(1)]
            m = re.match(r"^\s*templates\s*:\s*\[([^\]]*)\]", l)
            if m:
                codes = [c.strip() for c in m.group(1).split(",") if c.strip()]
            m = re.match(r"^([A-Z][A-Z0-9]{1,5})\s*:\s*$", l)
            if m:
                codes = [m.group(1)]
            for c in codes:
                src = None
                for l2 in a.lines[i - 1:]:
                    sm = _SOURCE_TABLE.search(l2)
                    if sm:
                        src = sm.group(1)
                        break
                a.templates.append({"code": c, "line": i, "source_table": src})
    return assets


def writers_of(assets: dict[str, Asset], table: str) -> list[Asset]:
    return [a for a in assets.values() if table in a.writes]


def readers_of(assets: dict[str, Asset], table: str) -> list[Asset]:
    return [a for a in assets.values() if table in a.reads]


def importers_of(assets: dict[str, Asset], module: str) -> list[Asset]:
    return [a for a in assets.values() if module in a.imports]


def load_dictionary(workspace: Path) -> dict[str, dict]:
    p = Path(workspace) / "data-dictionary.csv"
    if not p.exists():
        return {}
    with p.open(encoding="utf-8", newline="") as fh:
        return {row["reg_code"]: row for row in csv.DictReader(fh)}

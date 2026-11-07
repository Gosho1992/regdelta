"""Stage 1 - Specification analysis.

Compares the previous and revised reporting specifications and produces the
Change Register. Every change points back to the exact line of the spec file
it came from, so a reviewer can check it.
"""
from __future__ import annotations

import json
from pathlib import Path

KIND_ORDER = [
    "field_added",
    "field_removed",
    "field_renamed",
    "format_changed",
    "type_changed",
    "mandatory_changed",
    "definition_changed",
    "rule_added",
    "rule_removed",
    "rule_modified",
]

KIND_LABEL = {
    "field_added": "New field",
    "field_removed": "Field removed",
    "field_renamed": "Field renamed",
    "format_changed": "Format changed",
    "type_changed": "Data type changed",
    "mandatory_changed": "Mandatory flag changed",
    "definition_changed": "Definition changed",
    "rule_added": "New validation rule",
    "rule_removed": "Validation rule removed",
    "rule_modified": "Validation rule changed",
}


def load_spec(path: Path) -> dict:
    text = Path(path).read_text(encoding="utf-8")
    spec = json.loads(text)
    spec["_lines"] = text.splitlines()
    spec["_path"] = Path(path)
    return spec


def _line_of(spec: dict, needle: str) -> int:
    for i, line in enumerate(spec["_lines"], start=1):
        if needle in line:
            return i
    return 1


def _ref(spec: dict, rel: str, needle: str, section: str | None) -> dict:
    line = _line_of(spec, needle)
    return {
        "file": rel,
        "line": line,
        "text": spec["_lines"][line - 1].strip(),
        "section": section,
    }


def diff_specs(old: dict, new: dict, old_rel: str, new_rel: str) -> list[dict]:
    """Return the ordered list of changes between two specifications."""
    changes: list[dict] = []
    old_fields = {f["dpm_id"]: f for f in old["fields"]}
    new_fields = {f["dpm_id"]: f for f in new["fields"]}

    def add(kind, subject, **kw):
        changes.append({"kind": kind, "subject": subject, **kw})

    # Fields: matched on the stable data-point id, so a renamed code is a
    # rename and not a delete plus an add.
    for dpm, nf in new_fields.items():
        if dpm not in old_fields:
            add(
                "field_added", nf["code"], field=nf, templates=nf["templates"],
                mandatory=nf["mandatory"],
                old=None, new=f'{nf["code"]} ({nf["type"]}, {"mandatory" if nf["mandatory"] else "optional"})',
                summary=f'New {"mandatory " if nf["mandatory"] else ""}field {nf["code"]} in template {", ".join(nf["templates"])}.',
                plain=f'The regulator now requires {nf["code"]} ({nf["definition"].rstrip(".")}). Every place that builds template {", ".join(nf["templates"])} must start producing it.',
                spec_ref=_ref(new, new_rel, f'"dpm_id": "{dpm}"', nf.get("section")),
            )
    for dpm, of in old_fields.items():
        if dpm not in new_fields:
            add(
                "field_removed", of["code"], field=of, templates=of["templates"],
                mandatory=of["mandatory"], old=of["code"], new=None,
                summary=f'Field {of["code"]} removed from template {", ".join(of["templates"])}.',
                plain=f'{of["code"]} is no longer part of the specification. Anything that still sends or depends on it must be cleaned up.',
                spec_ref=_ref(old, old_rel, f'"dpm_id": "{dpm}"', of.get("section")),
            )
    for dpm, nf in new_fields.items():
        of = old_fields.get(dpm)
        if not of:
            continue
        ref = _ref(new, new_rel, f'"dpm_id": "{dpm}"', nf.get("section"))
        if of["code"] != nf["code"]:
            add(
                "field_renamed", of["code"], field=nf, old_field=of, templates=nf["templates"],
                mandatory=nf["mandatory"], old=of["code"], new=nf["code"],
                summary=f'Field {of["code"]} renamed to {nf["code"]} (same data point {dpm}).',
                plain=f'{of["code"]} is now called {nf["code"]}. The meaning is the same, but every mapping that uses the old name will produce the wrong column.',
                spec_ref=ref,
            )
        if (of.get("format") or None) != (nf.get("format") or None):
            add(
                "format_changed", nf["code"], field=nf, old_field=of, templates=nf["templates"],
                mandatory=nf["mandatory"], old=of.get("format"), new=nf.get("format"),
                summary=f'{nf["code"]} format changes from {of.get("format")} to {nf.get("format")}.',
                plain=f'{nf["code"]} must now be written as {nf.get("format")} instead of {of.get("format")}. Code that formats this value will produce rejected files.',
                spec_ref=ref,
            )
        if of["type"] != nf["type"]:
            add(
                "type_changed", nf["code"], field=nf, old_field=of, templates=nf["templates"],
                mandatory=nf["mandatory"], old=of["type"], new=nf["type"],
                summary=f'{nf["code"]} type changes from {of["type"]} to {nf["type"]}.',
                plain=f'{nf["code"]} changes data type. Columns and casts that hold it need checking.',
                spec_ref=ref,
            )
        if of["mandatory"] != nf["mandatory"]:
            add(
                "mandatory_changed", nf["code"], field=nf, old_field=of, templates=nf["templates"],
                mandatory=nf["mandatory"], old=str(of["mandatory"]), new=str(nf["mandatory"]),
                summary=f'{nf["code"]} becomes {"mandatory" if nf["mandatory"] else "optional"}.',
                plain=f'{nf["code"]} is now {"mandatory" if nf["mandatory"] else "optional"}.',
                spec_ref=ref,
            )
        if of["definition"].strip() != nf["definition"].strip():
            add(
                "definition_changed", nf["code"], field=nf, old_field=of, templates=nf["templates"],
                mandatory=nf["mandatory"], old=of["definition"], new=nf["definition"],
                summary=f'Definition of {nf["code"]} changed.',
                plain=f'{nf["code"]} keeps its name but now means something different: "{nf["definition"]}" (was: "{of["definition"]}"). Every calculation built on it produces different numbers.',
                spec_ref=ref,
            )

    old_rules = {r["id"]: r for r in old.get("validation_rules", [])}
    new_rules = {r["id"]: r for r in new.get("validation_rules", [])}
    for rid, nr in new_rules.items():
        if rid not in old_rules:
            add(
                "rule_added", rid, rule=nr, templates=nr["templates"], mandatory=True,
                old=None, new=nr["expression"], severity=nr["severity"],
                summary=f'New validation rule {rid} ({nr["severity"]}) on {", ".join(nr["templates"])}: {nr["expression"]}',
                plain=f'The regulator will run a new check, {rid}, and {"reject" if nr["severity"] == "error" else "flag"} submissions that fail it. Our pre-submission checks must include it.',
                spec_ref=_ref(new, new_rel, f'"id": "{rid}"', nr.get("section")),
            )
    for rid, orr in old_rules.items():
        if rid not in new_rules:
            add(
                "rule_removed", rid, rule=orr, templates=orr["templates"], mandatory=False,
                old=orr["expression"], new=None, severity=orr["severity"],
                summary=f"Validation rule {rid} removed.",
                plain=f"{rid} no longer applies.",
                spec_ref=_ref(old, old_rel, f'"id": "{rid}"', orr.get("section")),
            )
    for rid, nr in new_rules.items():
        orr = old_rules.get(rid)
        if not orr:
            continue
        diffs = []
        if orr["templates"] != nr["templates"]:
            diffs.append(f'templates {"+".join(orr["templates"])} -> {"+".join(nr["templates"])}')
        if orr["severity"] != nr["severity"]:
            diffs.append(f'severity {orr["severity"]} -> {nr["severity"]}')
        if orr["expression"] != nr["expression"]:
            diffs.append("expression rewritten")
        if diffs:
            linked = [t for t in nr["templates"] if t not in orr["templates"]]
            plain = f'{rid} changed ({"; ".join(diffs)}).'
            if linked:
                plain += f' It now links template {", ".join(orr["templates"])} to {", ".join(linked)}: the two reports must agree with each other, not just be valid on their own.'
            if orr["severity"] != nr["severity"] and nr["severity"] == "error":
                plain += " A failure now blocks the submission instead of producing a warning."
            add(
                "rule_modified", rid, rule=nr, old_rule=orr, templates=nr["templates"],
                linked_templates=linked, mandatory=True, severity=nr["severity"],
                old=orr["expression"], new=nr["expression"],
                summary=f'Validation rule {rid} changed: {"; ".join(diffs)}.',
                plain=plain,
                spec_ref=_ref(new, new_rel, f'"id": "{rid}"', nr.get("section")),
            )

    changes.sort(key=lambda c: (KIND_ORDER.index(c["kind"]), c["subject"]))
    for i, c in enumerate(changes, start=1):
        c["id"] = f"CHG-{i:02d}"
        c["label"] = KIND_LABEL[c["kind"]]
    return changes

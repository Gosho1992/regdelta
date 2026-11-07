"""Stages 2 and 3 - Technical impact mapping and dependency mapping.

Turns each change in the Change Register into evidence-linked findings:

  gap        the asset implements an affected template but is missing
             something the new spec requires (checked: the element is absent)
  direct     the asset names the changed field, column, rule or format
  upstream   the asset feeds a table that must start carrying a new field
  downstream the asset consumes the output of a directly affected asset
             (table lineage or a Python import)
  candidate  a possible semantic relationship with no exact reference.
             Never presented as confirmed; always needs a human.

Every finding carries file + line evidence that tests re-verify.
"""
from __future__ import annotations

import re
from collections import deque

from .scanner import Asset, KIND_LABEL, importers_of, readers_of, word_pattern, writers_of

IMPACT_ORDER = ["gap", "direct", "upstream", "downstream", "candidate"]
IMPACT_LABEL = {
    "gap": "Direct - missing element",
    "direct": "Direct reference",
    "upstream": "Upstream feeder",
    "downstream": "Downstream (lineage)",
    "candidate": "Semantic candidate",
}
RISK_ORDER = ["Critical", "High", "Medium", "Low"]

STOPWORDS = set(
    "a an and are as at be before by for from in into is it its of on or that the this to was were with "
    "within used use not yet only all any each per".split()
)
GENERIC_PHRASES = {"credit risk", "risk provisions"}

# Which way a change travels through lineage.
PROPAGATION = {
    "field_added": "upstream",
    "definition_changed": "downstream",
    "format_changed": "downstream",
    "type_changed": "downstream",
}
SEMANTIC_KINDS = {"field_added", "definition_changed"}


def _strftime(fmt: str) -> str:
    return fmt.replace("YYYY", "%Y").replace("MM", "%m").replace("DD", "%d")


def format_tokens(fmt: str | None) -> list[str]:
    if not fmt or not any(x in fmt for x in ("YYYY", "MM", "DD")):
        return []
    return sorted({fmt, _strftime(fmt), fmt.replace("DD", "dd").replace("YYYY", "yyyy")})


def _content_words(text: str) -> list[str]:
    words = re.sub(r"[^a-z0-9]+", " ", text.lower()).split()
    return [w for w in words if w not in STOPWORDS and len(w) > 1]


def phrases(text: str) -> set[str]:
    w = _content_words(text)
    return {f"{a} {b}" for a, b in zip(w, w[1:])} - GENERIC_PHRASES


def risk_for(change: dict, impact: str, kind: str) -> str:
    if impact == "candidate":
        return "Low"
    if kind == "dictionary":
        return "Low"
    if kind in ("dashboard", "management_report"):
        return "Medium"
    if kind in ("export", "regulatory_report"):
        return "Critical" if impact in ("gap", "direct") else "High"
    if kind == "validation_config":
        return "Critical" if change["kind"].startswith("rule_") else "High"
    return "High"


def confidence_for(impact: str, score: int = 0) -> str:
    if impact in ("gap", "direct"):
        return "High - exact reference"
    if impact in ("upstream", "downstream"):
        return "High - deterministic lineage"
    return "Medium - semantic match" if score >= 2 else "Low - semantic match"


ACTIONS = {
    ("field_added", "gap"): "Add {new_code} to this {kind_label} for template {templates}.",
    ("field_added", "upstream"): "Carry {new_code} through this feeder so the reporting table can expose it.",
    ("field_added", "candidate"): "Check whether this asset is the right source for {new_code}.",
    ("field_removed", "direct"): "Remove {old_code} from this {kind_label}.",
    ("field_renamed", "direct"): "Rename {old_code} to {new_code} in this {kind_label}.",
    ("format_changed", "direct"): "Change the {field} format from {old} to {new}.",
    ("format_changed", "downstream"): "Re-test output after the {field} format change in its dependency.",
    ("definition_changed", "direct"): "Recalculate {field} under the new definition: {new}",
    ("definition_changed", "downstream"): "Re-validate figures: an upstream {field} calculation changes meaning.",
    ("definition_changed", "candidate"): "Confirm whether this metric follows the {field} definition; if so, align it.",
    ("rule_added", "gap"): "Implement validation rule {rule} ({new}).",
    ("rule_modified", "direct"): "Update to satisfy {rule}: {new}",
}


def _action(change: dict, impact: str, asset: Asset) -> str:
    key = (change["kind"], impact)
    tmpl = ACTIONS.get(key) or ACTIONS.get((change["kind"], "direct")) or "Review this asset against {change_id}."
    f = change.get("field") or {}
    of = change.get("old_field") or f
    return tmpl.format(
        new_code=f.get("code", change["subject"]), old_code=of.get("code", change["subject"]),
        field=change["subject"], kind_label=KIND_LABEL.get(asset.kind, asset.kind).lower(),
        templates=", ".join(change.get("templates", [])), old=change.get("old"), new=change.get("new"),
        rule=change["subject"], change_id=change["id"],
    )


def _ev(asset: Asset, line: int, note: str | None = None) -> dict:
    e = {"file": asset.path, "line": line, "text": asset.lines[line - 1].strip()}
    if note:
        e["note"] = note
    return e


def map_impacts(changes: list[dict], assets: dict[str, Asset], dictionary: dict[str, dict]) -> list[dict]:
    findings: list[dict] = []

    for ch in changes:
        per_change: dict[str, dict] = {}   # asset path -> finding

        def put(asset: Asset, impact: str, evidence: list[dict], rationale: str, via=None, score=0):
            if asset.path in per_change:
                return
            per_change[asset.path] = {
                "change_id": ch["id"],
                "requirement": f'{ch["label"]}: {ch["subject"]}',
                "asset": asset.path,
                "asset_name": asset.name,
                "asset_kind": asset.kind,
                "asset_kind_label": KIND_LABEL.get(asset.kind, asset.kind),
                "owner": asset.owner,
                "impact_type": impact,
                "impact_label": IMPACT_LABEL[impact],
                "evidence": evidence,
                "via": via or [],
                "risk": risk_for(ch, impact, asset.kind),
                "confidence": confidence_for(impact, score),
                "review_status": "needs_review",
                "rationale": rationale,
                "action": _action(ch, impact, asset),
            }

        field = ch.get("field") or {}
        old_field = ch.get("old_field") or field
        kind = ch["kind"]

        # ---------------- gaps and direct references ----------------
        if kind == "field_added":
            code = field["code"]
            code_pat = word_pattern(code, True)
            for a in assets.values():
                if a.kind in ("export", "regulatory_report"):
                    for t in a.templates:
                        if t["code"] in ch["templates"] and not a.text_contains(code_pat):
                            put(a, "gap", [_ev(a, t["line"], f"implements {t['code']}; {code} not found anywhere in file")],
                                f"Implements template {t['code']} but has no {code}.")
                if a.kind == "dictionary" and not a.text_contains(code_pat):
                    put(a, "gap", [_ev(a, 1, f"no row for {code}")], f"The data dictionary has no entry for {code}.")
        elif kind in ("field_removed", "field_renamed", "definition_changed", "type_changed", "mandatory_changed"):
            code = old_field["code"]
            pats = [word_pattern(code, True)]
            col = (dictionary.get(code) or {}).get("internal_column")
            if col and kind != "field_renamed":    # a rename changes the regulatory code, not our column
                pats.append(word_pattern(col, False))
            for a in assets.values():
                hits = [(i, l) for i, l in enumerate(a.lines, 1) if any(p.search(l) for p in pats)]
                if hits:
                    names = code + (f" / {col}" if col and kind != "field_renamed" else "")
                    put(a, "direct", [_ev(a, i) for i, _ in hits[:4]], f"References {names} directly.")
        elif kind == "format_changed":
            code = field["code"]
            col = (dictionary.get(code) or {}).get("internal_column")
            pats = [word_pattern(code, True)] + ([word_pattern(col, False)] if col else [])
            toks = format_tokens(ch.get("old"))
            for a in assets.values():
                if not any(a.text_contains(p) for p in pats):
                    continue
                hits = [(i, l) for i, l in enumerate(a.lines, 1) if any(t in l for t in toks)]
                if hits:
                    put(a, "direct", [_ev(a, i, f"old format {ch['old']}") for i, _ in hits[:4]],
                        f"Hard-codes the old {code} format {ch['old']}.")
        elif kind in ("rule_added",):
            rid_pat = word_pattern(ch["subject"], True)
            for a in assets.values():
                if a.kind == "validation_config" and not a.text_contains(rid_pat):
                    line = next((i for i, l in enumerate(a.lines, 1) if "kind" in l), 1)
                    put(a, "gap", [_ev(a, line, f"{ch['subject']} not implemented")],
                        f"Pre-submission validation does not implement {ch['subject']}.")
        elif kind in ("rule_modified", "rule_removed"):
            rid_pat = word_pattern(ch["subject"], True)
            for a in assets.values():
                hits = a.find(rid_pat)
                if hits:
                    put(a, "direct", [_ev(a, i) for i, _ in hits[:3]], f"Implements rule {ch['subject']}.")
            if kind == "rule_modified":
                for a in assets.values():
                    if a.kind in ("export", "regulatory_report"):
                        for t in a.templates:
                            if t["code"] in ch["templates"]:
                                note = "newly linked by the rule" if t["code"] in ch.get("linked_templates", []) else "covered by the rule"
                                put(a, "direct", [_ev(a, t["line"], f"implements {t['code']} ({note})")],
                                    f"Produces template {t['code']}, which {ch['subject']} now checks.")

        directs = [p for p, f in per_change.items() if f["impact_type"] in ("gap", "direct")]

        # ---------------- lineage ----------------
        mode = PROPAGATION.get(kind)
        if mode == "upstream":
            queue = deque()
            for p in directs:
                for t in assets[p].templates:
                    if t["code"] in ch["templates"] and t["source_table"]:
                        queue.append((t["source_table"], [p, t["source_table"]]))
            seen = set()
            while queue:
                table, path = queue.popleft()
                if table in seen:
                    continue
                seen.add(table)
                for w in writers_of(assets, table):
                    put(w, "upstream", [_ev(w, w.writes[table], f"writes {table}")],
                        f"Builds {table}, which must carry {field['code']}.", via=path + [w.path])
                    for src in w.reads:
                        queue.append((src, path + [w.path, src]))
        elif mode == "downstream":
            queue = deque((p, [p]) for p in directs)
            while queue:
                p, path = queue.popleft()
                a = assets[p]
                nxt = []
                for table in a.writes:
                    for r in readers_of(assets, table):
                        nxt.append((r, [table], _ev(r, r.reads[table], f"reads {table}")))
                if a.module:
                    for r in importers_of(assets, a.module):
                        nxt.append((r, [f"import {a.module}"], _ev(r, r.imports[a.module], f"imports {a.module}")))
                for r, hop, ev in nxt:
                    if r.path in per_change:
                        continue
                    put(r, "downstream", [ev], f"Consumes output of {p} ({hop[0]}).", via=path + hop + [r.path])
                    queue.append((r.path, path + hop + [r.path]))

        # ---------------- semantic candidates ----------------
        if kind in SEMANTIC_KINDS:
            wanted = phrases(field.get("definition", "")) | phrases(old_field.get("definition", ""))
            for a in assets.values():
                if a.path in per_change or a.kind == "dictionary":
                    continue
                hits, matched = [], set()
                for i, l in enumerate(a.lines, 1):
                    m = phrases(l) & wanted
                    if m:
                        hits.append(i)
                        matched |= m
                if hits:
                    put(a, "candidate", [_ev(a, i, "semantic match: " + ", ".join(sorted(phrases(a.lines[i - 1]) & wanted))) for i in hits[:3]],
                        f'Mentions {", ".join(sorted(matched))} but never references {ch["subject"]} or its column. Possibly related; not confirmed.',
                        score=len(matched))
            # candidates propagate as candidates
            cands = [p for p, f in per_change.items() if f["impact_type"] == "candidate"]
            queue = deque((p, [p]) for p in cands)
            while queue:
                p, path = queue.popleft()
                for table in assets[p].writes:
                    for r in readers_of(assets, table):
                        if r.path in per_change:
                            continue
                        put(r, "candidate", [_ev(r, r.reads[table], f"reads {table}, built by candidate {p}")],
                            f"Consumes {table} from candidate {p}. Affected only if that candidate is confirmed.",
                            via=path + [table, r.path])
                        queue.append((r.path, path + [table, r.path]))

        ordered = sorted(per_change.values(), key=lambda f: (IMPACT_ORDER.index(f["impact_type"]), len(f["via"]), f["asset"]))
        findings.extend(ordered)

    for i, f in enumerate(findings, start=1):
        f["id"] = f"F-{i:03d}"
    return findings


def build_graph(changes: list[dict], findings: list[dict], assets: dict[str, Asset]) -> dict:
    """Nodes: changes, affected assets, the tables between them. Edges carry evidence."""
    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    affected = {f["asset"] for f in findings}

    for ch in changes:
        nodes[ch["id"]] = {"id": ch["id"], "type": "change", "label": f'{ch["subject"]}', "sub": ch["label"], "change_kind": ch["kind"]}
    for p in sorted(affected):
        a = assets[p]
        worst = min((RISK_ORDER.index(f["risk"]) for f in findings if f["asset"] == p), default=3)
        only_cand = all(f["impact_type"] == "candidate" for f in findings if f["asset"] == p)
        nodes[p] = {"id": p, "type": "asset", "kind": a.kind, "label": p.split("/")[-1], "sub": KIND_LABEL.get(a.kind, a.kind),
                    "owner": a.owner, "risk": RISK_ORDER[worst], "candidate_only": only_cand}
    # tables that sit between affected assets
    for p in sorted(affected):
        a = assets[p]
        for t, line in a.writes.items():
            nodes.setdefault(f"table:{t}", {"id": f"table:{t}", "type": "table", "label": t, "sub": "table"})
            edges.append({"from": p, "to": f"table:{t}", "kind": "writes", "evidence": f"{p}:{line}"})
        for t, line in a.reads.items():
            writers = [w.path for w in writers_of(assets, t)]
            if any(w in affected for w in writers) or not writers:
                nodes.setdefault(f"table:{t}", {"id": f"table:{t}", "type": "table", "label": t,
                                                "sub": "table" if writers else "source system"})
                edges.append({"from": f"table:{t}", "to": p, "kind": "reads", "evidence": f"{p}:{line}"})
        for m, line in a.imports.items():
            for b in assets.values():
                if b.module == m and b.path in affected:
                    edges.append({"from": b.path, "to": p, "kind": "imports", "evidence": f"{p}:{line}"})
    for f in findings:
        e = f["evidence"][0]
        edges.append({"from": f["change_id"], "to": f["asset"], "kind": f["impact_type"], "finding": f["id"],
                      "evidence": f'{e["file"]}:{e["line"]}', "risk": f["risk"]})
    # drop table nodes that ended up with a single edge to nowhere useful
    return {"nodes": list(nodes.values()), "edges": edges}

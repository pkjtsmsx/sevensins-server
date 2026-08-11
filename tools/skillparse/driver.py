"""Drives the parse: clause -> effects (trigger + segments), and a whole skill row ->
its structured record. Ties text/targets/matchers together; holds no rules of its own."""
import json
import re

from .matchers import TRIGGERS, is_noop_clause, parse_segment, split_segments
from .text import clean, split_clauses, split_named_blocks, strip_status_defs


def parse_clause(clause, catalog):
    """-> list of effect dicts for one clause. Trigger is clause-level; target and
    chance are resolved per SEGMENT so co-located ops don't steal each other's."""
    trigger = "on_use"
    text = clause
    for regex, name in TRIGGERS:
        if regex.search(text):
            trigger = name
            text = regex.sub("", text)
            break
    if re.match(r"^if\b", text, re.I):
        trigger = "conditional"

    effects = []
    for seg in split_segments(text, catalog):
        effects.extend(parse_segment(seg, trigger, catalog))
    seen, uniq = set(), []
    for e in effects:
        k = json.dumps(e, sort_keys=True)
        if k not in seen:
            seen.add(k)
            uniq.append(e)
    return uniq


def parse_skill(row, catalog):
    prose, defined = strip_status_defs(clean(row.get("_note1_en")))
    blocks = split_named_blocks(prose)
    out_blocks, unparsed = [], []
    for name, body in blocks:
        effects = []
        for clause in split_clauses(body):
            got = parse_clause(clause, catalog)
            if got:
                effects.extend(got)
            elif clause.strip() and not is_noop_clause(clause):
                unparsed.append(clause.strip())
        out_blocks.append({"name": name, "effects": effects})
    return {"id": row.get("_id"), "type": row.get("_type"),
            "target": row.get("_target"), "hits": row.get("_count"),
            "defined_statuses": defined, "blocks": out_blocks, "unparsed": unparsed}

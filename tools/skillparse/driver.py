"""Drives the parse: clause -> effects (trigger + segments), and a whole skill row ->
its structured record. Ties text/targets/matchers together; holds no rules of its own."""
import json
import re

from .conditions import parse_condition
from .matchers import RIDER_PREFIX_RE, TRIGGERS, is_noop_clause, parse_segment, split_segments
from .text import clean, split_clauses, split_named_blocks, strip_status_defs


def _split_conditional(text):
    """'If <cond>, <effects>' -> (cond_dict, effect_text), or None if the clause is not
    conditional OR its gate doesn't parse. The gate may itself contain commas ('affected
    by Freeze, Charm and Elite'), so try every comma split and keep the LONGEST prefix
    that parses as a condition -- parse_condition is strict, so a longer accepted prefix
    is always the truer reading, never a swallowed effect."""
    m = re.match(r"^if\b", text, re.I)
    if not m:
        return None
    parts = text.split(",")
    best = None
    for i in range(1, len(parts)):
        cond = parse_condition(",".join(parts[:i]))
        if cond:
            best = (cond, ",".join(parts[i:]).strip())
    if best is None:
        return None
    cond, rest = best
    rest = re.sub(r"^then\b\s*", "", rest, flags=re.I)
    return cond, rest


def parse_clause(clause, catalog):
    """-> list of effect dicts for one clause. Trigger is clause-level; target and
    chance are resolved per SEGMENT so co-located ops don't steal each other's.
    A conditional clause ('If <gate>, <effects>') parses ONLY when the gate does: its
    effects then carry {"when": cond} and the engine evaluates the gate at runtime. An
    unparseable gate fails the whole clause -- never fire an effect on a guessed gate."""
    trigger = "on_use"
    text = re.sub(r"^(?:\d+\.\s*|[※*(]\s*)+", "", clause)
    for regex, name in TRIGGERS:
        if regex.search(text):
            trigger = name
            text = regex.sub("", text)
            break
    text = RIDER_PREFIX_RE.sub("", text)

    when = None
    got = _split_conditional(text)
    if got:
        when, text = got
    elif re.match(r"^if\b", text, re.I):
        return []                       # conditional clause with an unparseable gate

    effects = []
    for seg in split_segments(text, catalog):
        for e in parse_segment(seg, trigger, catalog):
            if when is not None:
                e["when"] = when
            effects.append(e)
    seen, uniq = set(), []
    for e in effects:
        k = json.dumps(e, sort_keys=True)
        if k not in seen:
            seen.add(k)
            uniq.append(e)
    return uniq


def _fix_impossible_targets(row, blocks):
    """A damaging skill aimed at ENEMIES can never damage the caster -- the design row
    says so, and it outranks anything read out of prose.

    "Deals 200% ATK as damage and heals the STR Type ally ... by 100% of THE CASTER's
    ATK" resolved the damage to `self`, and the cast attacked itself in game. The prose
    fix (segmenting on heal verbs) handles that sentence; this is the guard for the
    class of it. `DesignSkillRow.GetTargetGroup()` is `_target / 100`: 0 enemies,
    1 allies -- so on an enemy-group skill, self-damage is by construction wrong.
    """
    if (int(row.get("_target") or 0) // 100) != 0:
        return
    for block in blocks:
        for eff in block["effects"]:
            if eff.get("self_inflicted"):
                continue          # a stated drawback ("then takes 25% Max HP damage")
            if (eff.get("op") == "damage"
                    and eff.get("target") in ("self", "all_allies")):
                # Both directions of the same error: the damage inherited a target from
                # a HEAL phrase sharing the sentence ("Deals 160% ATK as damage and
                # recovers all allies..."), so the skill hit its own team.
                eff["target"] = "enemy_target"


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
    _fix_impossible_targets(row, out_blocks)
    return {"id": row.get("_id"), "type": row.get("_type"),
            "target": row.get("_target"), "hits": row.get("_count"),
            "defined_statuses": defined, "blocks": out_blocks, "unparsed": unparsed}

#!/usr/bin/env python3
"""Compile the design pack's skill rows into a checked-in, diffable spec.

The point of compiling offline: a wrong skill becomes a visible one-line data change in
git, not a mystery at turn 7 of a fight. Nothing here runs at battle time.

Sources, in order of confidence (see docs/BATTLE_CLIENT_CONTRACT.md):

  targeting breadth   `_target` -> the client's own `23000 + _target` label table  EXACT
  swings              cinematic BscTagKind.Damage tag count, else `hit`            EXACT
  cd / charge / type  design columns                                               EXACT
  effects             the opcode slots `_action[]` / `_actID[]`                     EXACT
  damage coefficient  prose (`Deals 108% ATK as damage`) -- nowhere else            INFERRED

Only the damage coefficient is prose-derived. Damage is genuinely absent from the opcode
script: pure-damage skills often carry no opcodes at all.

    tools/compile_skills.py [--skill ID ...] [--out FILE] [--stats]
"""
import argparse
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "server")
SWINGS_FILE = os.path.join(SERVER, "battle_data/cinematic_swings.json")
OUT_DEFAULT = os.path.join(SERVER, "battle_data/skills.json")

sys.path.insert(0, SERVER)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(SERVER)
# design_data is the design-PACK reader and has no battle imports. Reaching it via
# `import battle` (as this did) needlessly pulled the OLD engine into the new pipeline;
# nothing here should touch it.
import design_data as dd  # noqa: E402
import status_prose as sp  # noqa: E402

SKILL_TYPE = {1: "com_attack", 2: "skill", 3: "sp_skill", 4: "passive",
              5: "support", 6: "status", 7: "sub_skill", 10: "god_item",
              101: "collection1", 102: "collection2", 103: "collection3"}

TARGET_GROUP = {0: "enemy", 1: "ally", 2: "dead_enemy", 3: "dead_ally"}

# Status id blocks -> (category, stackable). From op 114's category operands, each
# independently confirmed by prose. See contract doc 6.1.
STATUS_BLOCK = {
    1000: ("heal_over_time", False),
    2000: ("buff", False),
    3000: ("buff", True),
    4000: ("shield", False),
    5000: ("damage_over_time", False),
    6000: ("debuff", False),
    7000: ("debuff", True),
    8000: ("passive_grant", False),
    9000: ("stat_up", False),
}

# Opcodes that take an operand. Everything else is a trigger or an operandless effect --
# op 115 (skill-CD change) proves "no operand" does NOT imply "condition".
OP_APPLY, OP_APPLY_CHANCE, OP_REMOVE, OP_FOLLOW_UP = 112, 113, 114, 117
OP_MODIFY_CD = 115

# Operandless EFFECT opcodes, decoded by prose enrichment against the whole corpus: for
# each opcode, how much more often its rows mention a concept than the corpus baseline.
# The test is decisive because these three also appear ALONE on hundreds of rows, where
# there is nothing else the prose could be describing.
#
#   116  move gauge   92% of 2,478 rows vs 6.4% baseline (4.2x); alone on 414 rows,
#                     375 of which say "Move Gauge" / 行動值
#   111  revive       98% of 245 rows vs 4% baseline (23x); alone on 14 rows reading 復活
#   5    heal         84% of 919 rows vs 20% baseline (4.2x)
#
# Their MAGNITUDE is not encoded -- the operand is always 0 -- so it comes from prose on
# the same three-tier basis as everything else, and is null when unstated.
OP_EFFECT_NO_OPERAND = {116: "modify_gauge", 111: "revive", 5: "heal"}

# --- op 1: the attack rider -------------------------------------------------------
#
# 1,195 sites, the largest single gap after 116/111/5. It is NOT one effect. Evidence:
#
#   * it rides on ATTACKS -- 87% of its rows are com_attack/skill/sp_skill and 93% of
#     them deal damage (corpus baseline 58%). op 5 by contrast is 46% passives with a
#     damage rate at baseline, which is what separates the two heal-ish opcodes.
#   * on the 137 rows where op 1 is the ONLY opcode the prose partitions cleanly, with
#     NO overlap: 102 heal ("Deals N% ATK as damage and recovers the caster's HP"),
#     30 bonus damage ("if the target is stunned, additionally deal 100% ATK once"),
#     5 neither.
#   * across all 1,136 rows with prose the same split holds at 422 / 422.
#
# So the opcode encodes "this attack carries an extra effect"; WHICH effect is only in
# prose, exactly as magnitudes are. 74% classify unambiguously; the remainder stay in
# `unknown` rather than being guessed, because emitting a rider with no kind would be an
# effect the engine silently skips.
OP_ATTACK_RIDER = 1
_RIDER_HEAL = re.compile(r"recover|restore|heal|absorb|回復|恢復|補血", re.I)
_RIDER_DMG = re.compile(
    r"additionally deal|extra .{0,10}(atk|damage)|額外造成|as damages", re.I)
# The rider's own percentage, not the skill's main coefficient: a parenthetical
# "(by 30% ATK)" for heals, or the number right after "additionally deal(s)".
_RIDER_PCT_HEAL = re.compile(
    r"(?:recovers?|restores?|heals?)[^.]{0,60}?(\d+(?:\.\d+)?)\s*%", re.I)
_RIDER_PCT_DMG = re.compile(
    r"additionally deals?[^.]{0,40}?(\d+(?:\.\d+)?)\s*%", re.I)


def attack_rider(r):
    """-> the op-1 rider effect, or None when its kind cannot be read."""
    note = r.get("_note1_en") or r.get("_note1") or ""
    if not note.strip():
        return None
    heal, dmg = bool(_RIDER_HEAL.search(note)), bool(_RIDER_DMG.search(note))
    if heal == dmg:                      # neither, or both -- genuinely ambiguous
        return None
    kind = "heal" if heal else "bonus_damage"
    m = (_RIDER_PCT_HEAL if heal else _RIDER_PCT_DMG).search(note)
    return {"kind": kind,
            "percent": float(m.group(1)) if m else None,
            "source": "prose" if m else None}

# --- op 116's magnitude and recipient --------------------------------------------
#
# The gauge clause states both, but only NEXT TO the words "Move Gauge" -- taking the
# first percentage in the note picks up the damage coefficient instead, which is how a
# 180-point gauge change reached the wire and hung the client.
#
# The recipient matters as much as the number: it is usually the CASTER or an ally, not
# the skill's target. "the caster's Move Gauge will increase 25%", "Grant the ally with
# the highest ATK an Move Gauge increase of 40%". Applying it to the skill's targets
# would speed up the enemies the skill just hit.
# Each operandless effect states its magnitude next to its OWN phrase, never first in
# the note -- the first percentage is the damage coefficient. This was fixed for the move
# gauge after a 180-point "gauge change" (Lucifer's 180% ATK) hung the client; heal and
# revive had exactly the same bug and simply failed quieter, losing the magnitude and
# doing nothing at all.
#
#   heal    "restores HP of all allies by 250% ATK", "restore 25% of the max HP"
#   revive  "Revive 2 random dead allies and restore 25% of their HP"
EFFECT_WORD = {
    "modify_gauge": re.compile(r"move gauge|行動值", re.I),
    # WORD-BOUNDED. Without \b, `heals?` matches inside "Healthy Strike IV: ... ATK+35%",
    # so a stat buff's clause was being read as a heal clause -- and since that clause
    # says ATK, it dragged the heal's magnitude, recipient AND basis off a line that
    # has nothing to do with healing. 365 rows were landing in the ATK bucket that way.
    "heal": re.compile(r"(?:\b(?:restores?|recovers?|heals?|healing)\b|回復|恢復|補血)",
                       re.I),
    "revive": re.compile(r"reviv|resurrect|復活", re.I),
}

_GAUGE_WORD = EFFECT_WORD["modify_gauge"]
_GAUGE_PCT = re.compile(r"(\d+(?:\.\d+)?)\s*%")
_GAUGE_SELF = re.compile(r"caster|self|its own|自身|我方自身", re.I)
_GAUGE_ALLY = re.compile(r"\ball(y|ies)\b|我方", re.I)
_GAUGE_ENEMY = re.compile(r"enem|敵方|对方", re.I)
# "the caster's Max HP", "its own ATK", "the target's SPD" -- the owner of a STAT the
# magnitude is computed from, which is not the same thing as the recipient.
_POSSESSIVE_SOURCE = re.compile(
    r"\b(?:the\s+)?(?:caster|self|its\s+own|target|enemy|ally)'?s?\s+"
    r"(?:max(?:imum)?\s*)?(?:HP|ATK|DEF|SPD|CRI|CRT)\b", re.I)

_GAUGE_DOWN = re.compile(r"reduc|decreas|lower|lose|下降|減少|降低", re.I)
_GAUGE_UP = re.compile(r"increas|rise|gain|restor|提升|增加|上升", re.I)


# What the heal's percentage is a percentage OF. The engine treated every heal as a
# fraction of the recipient's MAX HP, so Michael's "restores HP of all allies by 250%
# ATK" healed each ally for 250% of their own max HP -- a guaranteed full-party heal on
# a zero-cooldown skill, which is what made it look like a design mistake rather than
# ours. The two bases differ by an order of magnitude, so guessing is not an option.
_HEAL_ATK = re.compile(r"\bATK\b|攻擊力", re.I)
_HEAL_CASTER_HP = re.compile(
    r"\b(?:the\s+)?(?:caster|self|its\s+own)'?s?\s+(?:max(?:imum)?\s*)?HP\b", re.I)
_HEAL_HP = re.compile(r"\b(max(?:imum)?\s*HP|HP)\b|生命|血量", re.I)


def heal_basis(r):
    """-> "atk" or "max_hp" for a heal's percentage, from its own clause.

    Decided by what the percentage ATTACHES to, because the pack writes it both ways
    round: "by 250% ATK" puts the noun after the number, "recovers the caster's Max HP
    by 15%" puts it before. Looking forward first and only then behind is what keeps
    those apart. `max_hp` is the fallback -- it is the overwhelmingly common form, and
    it is the conservative one: reading an ATK heal as max-HP over-heals, but reading a
    max-HP heal as ATK would silently nerf every heal in the game.
    """
    note = r.get("_note1_en") or ""
    m = EFFECT_WORD["heal"].search(note)
    if not m:
        return "max_hp"
    # The clause runs from the heal verb to the end of its sentence, so a LATER
    # sentence's "% ATK" damage line cannot be read as this heal's basis.
    clause = re.split(r"(?<=[.!?])\s", note[m.start():])[0][:180]
    pm = re.search(r"(\d+(?:\.\d+)?)\s*%", clause)
    if not pm:
        return "max_hp"
    # WHOSE max HP, checked before the generic HP form: "restores HP to all allies by
    # 20% of the caster's Max HP" scales off the CASTER once, not off each recipient --
    # a party heal from a tanky healer is a flat number, not a fraction of whoever
    # receives it. Asking "is this HP-based?" first swallowed the distinction.
    def hp_kind():
        return "caster_max_hp" if _HEAL_CASTER_HP.search(clause) else "max_hp"

    after = clause[pm.end():pm.end() + 40]
    if _HEAL_ATK.search(after):
        return "atk"
    if _HEAL_HP.search(after):
        return hp_kind()
    before = clause[:pm.start()]
    if _HEAL_ATK.search(before) and not _HEAL_HP.search(before):
        return "atk"
    return hp_kind()


def clause_percent(r, op):
    """-> the percentage stated next to THIS effect's own phrase, or None.

    Searches forward from the phrase first and falls back to a short lookbehind, then
    rejects anything equal to the skill's damage coefficient -- that is exactly the
    number the naive "first percentage in the note" rule kept picking up.
    """
    word = EFFECT_WORD.get(op)
    note = r.get("_note1_en") or ""
    if word is None or not note:
        return None
    m = word.search(note)
    if not m:
        return None
    after = note[m.end():m.end() + 80]
    before = note[max(0, m.start() - 45):m.start()]
    pm = _GAUGE_PCT.search(after) or _GAUGE_PCT.search(before)
    if not pm:
        return None
    pct = float(pm.group(1))
    return None if _is_damage_coefficient(r, pct) else pct



def clause_target(r, op):
    """-> who an operandless effect acts on: "caster", "allies", "targets", or None.

    The recipient is NOT the skill's target, and assuming it is has now caused two
    live bugs. Michael's Gate of Judgement "restores HP of all allies by 200% ATK" is
    an ENEMY-targeting attack, so healing its targets healed the raid boss -- with five
    casts using it, the fight could not end.

    Read from the effect's own clause, the same way its magnitude is.
    """
    word = EFFECT_WORD.get(op)
    note = r.get("_note1_en") or ""
    if word is None or not note:
        return None
    m = word.search(note)
    if not m:
        return None
    clause = note[max(0, m.start() - 45):m.end() + 80]
    # A POSSESSIVE names the source of the number, not the recipient. Rainbow Wheel
    # "restores HP to all allies by 20% of the caster's Max HP" was being read as a
    # caster-only heal purely because the word "caster" appears in it -- so a full party
    # heal landed on one unit. Strip the possessives before asking who it acts on.
    stripped = _POSSESSIVE_SOURCE.sub(" ", clause)
    for probe in (stripped, clause):
        if _GAUGE_ALLY.search(probe):
            return "allies"
        if _GAUGE_SELF.search(probe):
            return "caster"
        if _GAUGE_ENEMY.search(probe):
            return "targets"
    return None


def gauge_effect(r):
    """-> {percent, target, source} for op 116, read from the gauge CLAUSE only.

    Two traps, both hit on the first attempt:

      * looking BEFORE the phrase first picks up the damage coefficient. Poison
        Injection reads "Deals 180% ATK as damage. Grant the ally ... an Move Gauge
        increase of 40%" -- the 40 is what matters and it comes after. So the search
        runs forward first and only falls back to a short lookbehind.
      * a direction word from an UNRELATED clause flips the sign. Sign of Ill Fortune
        reads "removes the target's All DMG Reduction. After the action, increases the
        caster's Move Gauge by 20%" -- "removes" is 60 characters away and made it -20.
        So "increase" wins over "reduce" when both appear.
    """
    note = r.get("_note1_en") or ""
    m = _GAUGE_WORD.search(note)
    if not m:
        return {"percent": None, "target": None, "source": None}
    after = note[m.end():m.end() + 80]
    before = note[max(0, m.start() - 45):m.start()]

    pm = _GAUGE_PCT.search(after) or _GAUGE_PCT.search(before)
    pct = float(pm.group(1)) if pm else None
    if pct is not None and _is_damage_coefficient(r, pct):
        pct = None                      # still the coefficient -- not this effect's

    clause = before + note[m.start():m.end() + 80]
    if pct is not None and _GAUGE_DOWN.search(clause) and not _GAUGE_UP.search(clause):
        pct = -pct

    if _GAUGE_SELF.search(clause):
        tgt = "caster"
    elif _GAUGE_ENEMY.search(clause):
        tgt = "targets"
    elif _GAUGE_ALLY.search(clause):
        tgt = "allies"
    else:
        tgt = None
    return {"percent": pct, "target": tgt,
            "source": "prose" if pct is not None else None}


# Percent for the operandless effects, e.g. "increases Move Gauge by 30%",
# "recovers the caster's Max HP by 15%", "recovers their HP by 35%".
_PCT_ANY = re.compile(r"(\d+(?:\.\d+)?)\s*%")

_COEF = re.compile(r"(\d+(?:\.\d+)?)\s*%\s*(ATK|DEF|Max HP|HP)", re.I)
# Chinese writes it either way round: `攻擊力95%` or `95%攻擊力`.
_COEF_ZH = re.compile(
    r"(攻擊力|最大體力|防禦力)\s*(\d+(?:\.\d+)?)\s*%"
    r"|(\d+(?:\.\d+)?)\s*%\s*(?:的)?\s*(攻擊力|最大體力|防禦力)")
_ZH_BASIS = {"攻擊力": "ATK", "防禦力": "DEF", "最大體力": "MAX_HP"}


def _text(tid):
    row = (dd.rows("text") or {}).get(tid) or (dd.rows("text") or {}).get(str(tid))
    if not row:
        return None
    return row.get("_text_en") or row.get("_text") or None


def breadth(target):
    """-> a structured targeting spec, from the client's own label table.

    `_target` packs group*100 + range, and the UI label is text `23000 + _target`. We
    parse that label because it is the client's own words for the rule, which beats
    re-deriving the range semantics ourselves.
    """
    t = int(target or 0)
    if t == 0:
        return {"group": None, "select": "none", "count": 0}
    spec = {"group": TARGET_GROUP.get(t // 100, str(t // 100))}
    label = _text(23000 + t)
    if not label:
        return dict(spec, select="unknown", count=None, raw=t)
    low = label.lower()
    spec["label"] = label
    if low.startswith("player"):
        # "Player", "Player+1 ally", "Player+2 allies"
        n = re.search(r"\+(\d+)", low)
        spec.update(select="self_plus", count=1 + (int(n.group(1)) if n else 0))
    elif "all " in low or low.startswith("all"):
        spec.update(select="all", count=None)
    elif "random" in low:
        n = re.match(r"(\d+)", low)
        spec.update(select="random", count=int(n.group(1)) if n else 1)
    elif "highest" in low or "lowest" in low:
        stat = re.search(r"(hp|atk|def|spd)", low)
        spec.update(select=("highest" if "highest" in low else "lowest"),
                    stat=(stat.group(1).upper() if stat else None), count=1)
    elif re.match(r"^(str|agi|tec)\b", low):
        spec.update(select="by_attribute", attribute=low.split()[0].upper(), count=1)
    elif "except player" in low:
        spec.update(select="all_except_self", count=None)
    else:
        n = re.match(r"(\d+)", low)
        spec.update(select="count", count=int(n.group(1)) if n else 1)
    return spec


def status_meta(rows, sid):
    """-> {id, name, category, stackable, stack_cap} for a status row id."""
    r = rows.get(sid) or {}
    name = (r.get("_name_en") or r.get("_name") or "").strip()
    cat, stackable = STATUS_BLOCK.get(sid // 1000 * 1000 if sid < 100000 else -1,
                                      ("other", False))
    cap = None
    m = re.search(r"\((\d+)\)\s*$", name)
    if m:
        cap = int(m.group(1))
        stackable = True
    return {"id": sid, "name": re.sub(r"\s*\(\d+\)\s*$", "", name) or None,
            "category": cat, "stackable": stackable, "stack_cap": cap}


_CORPUS = None
_CAST_GLOSSARY = None


def cast_glossary(rows):
    """-> {skill id: {status key: body}} merged across each CAST's whole skill set.

    A cast's four skills are written as a unit, and the glossary line for a status is
    stated once -- on whichever skill introduces it -- not repeated on every skill that
    applies it. Raphael's `Sweet Rhapsody` defines "Power Attack Seal: ... Lasts for 1
    turn"; his passive `Zero Cal` applies the same seal with no glossary line at all.

    Without this the passive's seal has no duration, and an unstated duration used to
    mean PERMANENT -- so the boss sealed the party's power attacks for the entire fight.
    """
    global _CAST_GLOSSARY
    if _CAST_GLOSSARY is not None:
        return _CAST_GLOSSARY
    out = {}
    for char in (dd.rows("char") or {}).values():
        skills = [s for s in (char.get("_skills") or []) if s]
        if not skills:
            continue
        merged = {}
        for sid in skills:
            row = rows.get(sid) or {}
            for name, body in sp.glossary_lines(row.get("_note1_en")).items():
                merged.setdefault(sp.norm_name(name), body)
        # Every level of every skill in the set shares the cast's glossary.
        for sid in skills:
            base = (rows.get(sid) or {}).get("_group") or sid
            for other, orow in rows.items():
                if (orow.get("_group") or other) == base:
                    out.setdefault(other, {}).update(merged)
    _CAST_GLOSSARY = out
    return out


def corpus_defaults(rows):
    """-> {status name key: {duration, magnitude, agreement, samples}}.

    Built once over every glossary line in the pack. Used ONLY as a fallback: 12,080 of
    the 21,407 apply sites are on a skill whose prose never mentions the status (the
    glossary lists what the description names, and passives, sub-skills and untranslated
    rows name nothing), so without a fallback more than half the effect graph would carry
    no duration at all.

    `agreement` is the share of corpus bodies backing the modal value, and it is the
    whole point of doing it this way: Daze agrees 93% across 473 bodies and a default is
    genuinely safe, while Iron Wrist agrees on duration only 74% and on magnitude 31%,
    where a default would be a fabricated number. The caller applies the threshold.
    """
    global _CORPUS
    if _CORPUS is not None:
        return _CORPUS
    import collections
    per = collections.defaultdict(list)
    for row in rows.values():
        for name, body in sp.glossary_lines(row.get("_note1_en")).items():
            per[sp.norm_name(name)].append(sp.parse(body))
    out = {}
    for key, parsed in per.items():
        entry = {"samples": len(parsed)}
        for field in ("duration", "magnitude"):
            vals = [p[field] for p in parsed if p[field] is not None]
            if vals:
                c = collections.Counter(vals).most_common(1)[0]
                entry[field] = c[0]
                entry[field + "_agreement"] = round(c[1] / len(vals), 3)
            else:
                entry[field] = None
                entry[field + "_agreement"] = 0.0
        out[key] = entry
    _CORPUS = out
    return out


# Below this share of corpus agreement a modal value is a guess, not a reading, and the
# field is left unknown instead. 0.8 keeps the control statuses (Daze 93%, Charm 98%,
# Stun 95%, Taunt 93%, All DMG Reduction 98%) and drops Iron Wrist's 74%.
DEFAULT_MIN_AGREEMENT = 0.8


def status_numbers(rows, skill_row, status_id):
    """-> the duration/magnitude for THIS skill applying THIS status, with provenance.

    Three tiers, and `source` says which was used so a wrong number is attributable:
      "skill"          -- the applying skill's own `* Name: ...` line (authoritative)
      "cast"           -- another skill of the SAME CAST defines it (see cast_glossary)
      "corpus_default" -- the modal value across the pack, agreement >= threshold
      None             -- nothing stated; the engine must apply a policy, knowingly
    """
    name = (rows.get(status_id) or {}).get("_name_en") \
        or (rows.get(status_id) or {}).get("_name")
    key = sp.norm_name(name)
    lines = {sp.norm_name(k): v for k, v in
             sp.glossary_lines(skill_row.get("_note1_en")).items()}
    if key in lines:
        got = sp.parse(lines[key])
        got["source"] = "skill"
        return got

    own = cast_glossary(rows).get(skill_row.get("_id") or 0, {})
    if key in own:
        got = sp.parse(own[key])
        got["source"] = "cast"
        return got

    d = corpus_defaults(rows).get(key)
    out = {"duration": None, "permanent": False, "magnitude": None,
           "magnitude_sign": None, "stat": None, "stacks": None, "raw": None,
           "source": None}
    if d:
        used = False
        for field in ("duration", "magnitude"):
            if d[field] is not None and d[field + "_agreement"] >= DEFAULT_MIN_AGREEMENT:
                out[field] = d[field]
                used = True
        if used:
            out["source"] = "corpus_default"
            out["default_samples"] = d["samples"]
    return out


def effects(rows, r):
    """-> (effects, unknown) read straight off the opcode slots.

    A repeated (op, operand) pair is emitted VERBATIM, one entry per slot, carrying its
    slot index. It is ambiguous by nature -- sometimes stacks, sometimes the same effect
    under two different triggers -- and folding repeats into a count is wrong about half
    the time. See contract doc 6.4.1.
    """
    acts, ids = r.get("_action") or [], r.get("_actID") or []
    out, unknown = [], []
    for i, op in enumerate(acts):
        if not op:
            continue
        aid = ids[i] if i < len(ids) else 0
        if op in (OP_APPLY, OP_APPLY_CHANCE) and aid in rows:
            meta = status_meta(rows, aid)
            out.append({"op": "apply_status", "slot": i,
                        "chance": op == OP_APPLY_CHANCE,
                        "conditional": is_conditional(r, meta.get("name")),
                        "requires": condition_requires(r, meta.get("name")),
                        "recipient": status_target(r, meta.get("name")),
                        "removes": status_removes(r, meta.get("name")),
                        "status": meta,
                        # Per-(skill, status): no column carries these, and they change
                        # with skill level while act_id does not. See contract doc 5.1.
                        "numbers": status_numbers(rows, r, aid)})
        elif op == OP_REMOVE:
            if aid in rows:
                out.append({"op": "remove_status", "slot": i,
                            "status": status_meta(rows, aid)})
            elif aid:
                cat, stackable = STATUS_BLOCK.get(aid, ("unknown", None))
                out.append({"op": "remove_status", "slot": i,
                            "category": cat, "stackable": stackable, "raw": aid})
        elif op == OP_FOLLOW_UP and aid:
            out.append({"op": "follow_up", "slot": i, "skill": aid,
                        "name": (rows.get(aid) or {}).get("_name_en")
                                or (rows.get(aid) or {}).get("_name")})
        elif op == OP_MODIFY_CD:
            out.append({"op": "modify_cd", "slot": i})
        elif op == OP_ATTACK_RIDER and attack_rider(r):
            out.append({"op": "attack_rider", "slot": i, **attack_rider(r)})
        elif op in OP_EFFECT_NO_OPERAND:
            # The opcode says WHAT; only prose says how much. A skill with an unstated
            # magnitude still executes the right kind of effect, which is strictly better
            # than filing the whole thing under `unknown` and executing nothing.
            note = r.get("_note1_en") or r.get("_note1") or ""
            m = _PCT_ANY.search(note)
            pct = float(m.group(1)) if m else None
            # ...but "the first percentage in the note" is the DAMAGE COEFFICIENT on an
            # attack skill. Lucifer's Eclipse Slash reads "Deals 180% ATK as damage
            # twice", and that 180 was being emitted as a 180-point move-gauge change.
            # If the number matches the coefficient it is not this effect's magnitude,
            # and unknown is the honest answer.
            if pct is not None and _is_damage_coefficient(r, pct):
                pct = None
            name = OP_EFFECT_NO_OPERAND[op]
            if pct is None:
                pct = clause_percent(r, name)      # try this effect's own clause
            entry = {"op": name, "slot": i,
                     "percent": pct,
                     "source": ("prose" if pct is not None else None)}
            if op == 116:
                entry.update(gauge_effect(r))
            elif name in ("heal", "revive"):
                # Same recipient problem as the gauge: a heal on an attack skill goes to
                # allies, not to the enemy being hit.
                entry["target"] = clause_target(r, name)
                if name == "heal":
                    entry["basis"] = heal_basis(r)
            out.append(entry)
        else:
            # Not decoded. Kept OUT of `effects` on purpose: the engine executes
            # `effects`, so an undecoded opcode sitting in that list would be silently
            # skipped and a half-understood skill would look identical to a complete
            # one. In its own list, "which skills do we only partly execute?" is a
            # query the harness can answer. See contract doc 6.3/6.5.
            unknown.append({"opcode": op, "slot": i,
                            **({"operand": aid} if aid else {})})
    return out, unknown


# A status named inside an "if/when ..." sentence is applied CONDITIONALLY, and the
# condition is nowhere in the opcode script -- `_action` carries no branch marker at all.
# Eclipse Slash reads "if the caster is affected by The Divine, ... inflict freeze", and
# its opcodes are a flat [115, 116, 112, 112].
#
# Applying those unconditionally is how a raid boss ended up permanently frozen AND
# stunned: two conditional control effects landing on every single cast.
#
# Attributed per EFFECT, not per skill: the sentence that names the status is the one
# that governs it. Per-skill would flag 73% of sites; per-sentence flags 25%.
_CONDITIONAL = re.compile(r"\b(if|when|whenever|upon|while|should)\b", re.I)
# Verbs that mean "this status is being APPLIED here", as opposed to merely referenced.
_GRANTS = re.compile(r"\b(grants?|inflicts?|applies|apply|gives?|gains?|cast)\b", re.I)
# "removes its the Divine effect", "removes it's the Fallen effect" (sic, both spellings)
_REMOVES = re.compile(
    r"removes?\s+([A-Za-z][A-Za-z0-9 '\-]{2,40}?)\s*effect", re.I)
# Leading words that name WHOSE status is stripped, not which one.
_REMOVE_OWNER = re.compile(
    r"^(?:it'?s|its|the|all|a|an|caster'?s?|target'?s?|enemy'?s?|"
    r"ally'?s?|allies'?|enemies'?|unit'?s?)\s+", re.I)
_STATUS_NAMES = None


def _loose(name):
    """Join key for a status name, matching engine/status.py's `_loose`. The leading
    article has to go: the registry row is `The Divine`, and the clause that strips it
    says "removes its the Divine effect", which the owner-stripper reduces to `Divine`.
    """
    n = sp.norm_name(name)
    return n[4:] if n.startswith("the ") else n


def _status_names():
    """Normalised names of every type-6 status row -- the whitelist a named strip must
    match. Without it the regex happily captured category cleanses ("removes control
    effect") and multi-name phrases ("removes Taunt and Freeze effect"), and a named
    strip outranks the `unremovable` guard, so a loose match there deletes statuses the
    game says nothing may remove.
    """
    global _STATUS_NAMES
    if _STATUS_NAMES is None:
        _STATUS_NAMES = set()
        for row in (dd.rows("skill") or {}).values():
            if row.get("_type") != 6:
                continue
            nm = _loose(row.get("_name_en") or row.get("_name") or "")
            if nm:
                _STATUS_NAMES.add(nm)
    return _STATUS_NAMES


def status_removes(r, status_name):
    """-> the status this application STRIPS, when the clause plainly names one.

    Lucifer's toggle: "grants the caster The Fallen and removes its the Divine effect".
    Without this both markers accumulate and the toggle never toggles.

    Only a single, known status name counts. A category cleanse ("removes control
    effect") is op 114's job and comes through as `remove_status`, and a status that
    reads as removing ITSELF is the sentence-matching heuristic misfiring, not a rule.
    """
    clause = _clause_for(r.get("_note1_en") or "", status_name)
    if not clause:
        return None
    for m in _REMOVES.finditer(clause):
        got = m.group(1).strip()
        while True:                       # "removes all allies' Fracture effect"
            stripped = _REMOVE_OWNER.sub("", got, count=1)
            if stripped == got:
                break
            got = stripped
        if re.search(r"\band\b|,", got):        # two names -- ambiguous, skip
            continue
        norm = _loose(got)
        if not norm or norm not in _status_names():
            continue
        if norm == _loose(status_name or ""):
            continue                      # a status does not strip itself
        return got


def _clause_for(note, name):
    """-> the sentence naming this status, or None.

    Tries the qualifier-stripped name too: the prose writes "Cast Serum Injection on up
    to 1 allies with the highest ATK" while the status rows are `Serum Injection(ATK)`
    and `(CRT)`. Without this the clause is never found, so the status looks
    unconditional and un-retargeted -- which is how a party buff ended up on the boss.
    """
    if not note or not name:
        return None
    wanted = [name.lower()]
    bare = re.sub(r"\s*\([^)]*\)\s*$", "", name).strip().lower()
    if bare and bare != wanted[0]:
        wanted.append(bare)

    # A status is often named in SEVERAL sentences -- as the CONDITION in one and as the
    # thing GRANTED in another. Lucifer's Lamenting Starlight is the clean example:
    #
    #   "if the caster is affected by The Divine, grants the caster The Fallen ..."
    #   "if the caster is affected by The Fallen, grants the caster The Divine ..."
    #
    # Taking the first match gave The Divine the FIRST sentence, so both halves of the
    # toggle came out requiring The Divine and the marker never flipped. Prefer the
    # sentence where a granting verb precedes the name; fall back to first match.
    fallback = None
    for sentence in re.split(r"(?<=[.!?])\s+", note):
        low = sentence.lower()
        for w in wanted:
            at = low.find(w)
            if at < 0:
                continue
            if fallback is None:
                fallback = sentence
            if _GRANTS.search(low[:at]):
                return sentence
            break
    return fallback


# A condition of the form "if the caster is affected by The Divine" IS evaluatable -- it
# names a status the engine already tracks. 956 of the 4,934 conditional sites are this
# shape, and it is the shape behind the reported bug (Eclipse Slash gates its Freeze on
# The Divine and its Stun on The Fallen).
#
# Extracting it turns a coin flip into a real check. The rest stay unevaluatable and fall
# back to the policy in engine/core.
_AFFECTED = re.compile(
    r"(caster|self|target|enemy|ally|allies)\b[^.]{0,40}?affected by(?: the)? "
    r"[\"\u201c]?([A-Za-z][A-Za-z0-9 '\-]{2,26}?)[\"\u201d]?\s*(?:,|\.|;|$| and | when | while )",
    re.I)

_SELF_WORDS = {"caster", "self"}


def status_target(r, status_name):
    """-> who a status is applied to: "caster", "allies", "targets", or None.

    **The recipient is not the skill's target.** This has now caused three live bugs in
    a row -- a move gauge handed to the enemy it was cast at, a heal that restored the
    raid boss, and Metatron's "Cast Serum Injection on up to 1 allies with the highest
    ATK" buffing the boss instead. An attack skill routinely aims at an enemy and applies
    something to its own side.

    Read from the sentence naming the status, the same way its duration and condition
    are. `None` means the prose does not say, and the engine falls back to the skill's
    targets -- which is right for the ordinary "inflicts X on the target" case.
    """
    clause = _clause_for(r.get("_note1_en") or "", status_name)
    if not clause:
        return None
    # Enemy wins when both appear: "grants all allies X and inflicts Y on all enemies"
    # is two clauses in one sentence, and the status we are asked about is usually the
    # one nearer its own verb -- so prefer the explicit ally/self wording only when no
    # enemy wording is present.
    if _GAUGE_ENEMY.search(clause):
        return None
    if _GAUGE_SELF.search(clause):
        return "caster"
    if _GAUGE_ALLY.search(clause):
        return "allies"
    return None


def condition_requires(r, status_name):
    """-> {"status": name, "on": "caster"|"target"} when the clause names one, else None."""
    clause = _clause_for(r.get("_note1_en") or "", status_name)
    if not clause:
        return None
    m = _AFFECTED.search(clause)
    if not m:
        return None
    who = m.group(1).lower()
    required = m.group(2).strip()
    if not required or required.lower() == (status_name or "").lower():
        return None                      # "if affected by X, X does more" -- not a gate
    return {"status": required,
            "on": "caster" if who in _SELF_WORDS else "target"}


def is_conditional(r, status_name):
    """Is this status applied only under a condition the opcodes do not encode?"""
    clause = _clause_for(r.get("_note1_en") or "", status_name)
    if clause is None:
        return None                      # not mentioned -- cannot tell either way
    return bool(_CONDITIONAL.search(clause))


def _is_damage_coefficient(r, pct):
    """Is `pct` just the skill's own damage coefficient restated?"""
    m = _COEF.search(r.get("_note1_en") or "")
    if m and abs(float(m.group(1)) - pct) < 1e-6:
        return True
    m = _COEF_ZH.search(r.get("_note1") or "")
    if m and abs(float(m.group(2) or m.group(3)) - pct) < 1e-6:
        return True
    return False


def damage(r, targets_enemy):
    """-> the damage entry, or None if this skill genuinely does not attack.

    Damage is the one thing with NO opcode: there is no `deal damage` verb in
    action[]. So its coefficient can only be read from prose -- but *whether* a skill
    attacks is structural (an enemy-targeting attack row with hit >= 1), and those two
    facts must not be conflated.

    They were. Emitting nothing when the prose had no percentage meant 1,159 real
    enemy-targeting skills -- the untranslated mob and boss attacks, e.g. 100201
    `爆触手` -- compiled to no damage effect at all and would have silently done
    nothing in a fight. That is the same silent-failure class as a zero duration, and
    it gets the same treatment: emit the effect, mark the coefficient unknown.

    Sources, in order of authority:
      "en"   -- the English note states `N% ATK`
      "zh"   -- the Chinese note states it; `_note1` is the ORIGINAL language and
                carries a percentage in MORE rows than the English (11,041 vs 10,837),
                so it is a recovery, not a guess
      None   -- no percentage in any language (904 enemy skills, mostly rows whose note
                was left as unfilled boilerplate: `強烈的一擊，%固定機率使對手的%(回合)。`).
                The engine must apply a default and know that it did.
    """
    note_en = r.get("_note1_en") or ""
    m = _COEF.search(note_en)
    basis, coef, source = None, None, None
    if m:
        basis = m.group(2).upper().replace(" ", "_")
        coef = round(float(m.group(1)) / 100.0, 4)
        source = "en"
    else:
        # The Chinese writes the percentage BEFORE the stat -- `95%攻擊力的2段傷害` --
        # which is why the English-shaped pattern finds nothing in these rows.
        m = _COEF_ZH.search(r.get("_note1") or "")
        if m:
            stat = m.group(1) or m.group(4)
            pct = m.group(2) or m.group(3)
            basis = _ZH_BASIS.get(stat, "ATK")
            coef = round(float(pct) / 100.0, 4)
            source = "zh"

    if coef is None and not (targets_enemy and (r.get("_count") or 0) >= 1):
        return None                      # not an attack at all -- correctly no damage

    times = None
    low = note_en.lower()
    for word, n in (("twice", 2), ("three times", 3), ("four times", 4),
                    ("five times", 5)):
        if word in low:
            times = n
            break
    return {"op": "damage", "basis": basis, "coefficient": coef,
            "prose_times": times, "source": source}


def compile_skill(rows, sid, swings_by_act):
    r = rows.get(sid)
    if not r:
        return None
    act = (r.get("_actName") or "").strip()
    typ = SKILL_TYPE.get(r.get("_type"), str(r.get("_type")))
    declared = int(r.get("_count") or 0)
    cine = swings_by_act.get(act)
    # A PASSIVE/STATUS row can carry a vestigial _actName it never renders (see contract
    # doc: 136 of the 193 apparent mismatches were exactly this), so ignore it there.
    if typ in ("passive", "status") or not act:
        swings, src = declared, "hit"
    elif cine:
        swings, src = cine, "cinematic"
    else:
        # Tagless cinematic -> DoAllDamage flattens the list; grouping is free.
        swings, src = declared, "hit(flatten)"
    spec = {
        "id": sid, "name": r.get("_name_en") or r.get("_name"),
        "group": r.get("_group"), "lv": r.get("_lv"), "type": typ,
        "target": breadth(r.get("_target")),
        "swings": swings, "swings_from": src,
        "cd": r.get("_cdTurn"), "charge": r.get("_charge"),
        "cinematic": act or None,
        "effects": [],
    }
    d = damage(r, spec["target"].get("group") == "enemy"
               and spec["type"] in ("com_attack", "skill", "sp_skill", "sub_skill"))
    if d:
        spec["effects"].append(d)
    eff, unknown = effects(rows, r)
    spec["effects"].extend(eff)
    if unknown:
        spec["unknown"] = unknown
    return spec


def cast_owners(rows):
    """-> {skill group id: [cast name, ...]}.

    Grouping by cast NAME rather than char id collapses the variants -- one cast spans
    several char rows (skins, rarities), and there are only 65 distinct names that own
    skills at all, 64 of them safe as filenames.
    """
    owners = {}
    for cid, c in (dd.rows("char") or {}).items():
        name = (c.get("_name_en") or "").strip()
        if not name or not re.fullmatch(r"[A-Za-z0-9 _.'-]+", name):
            continue
        for sk in (c.get("_skills") or []):
            if sk:
                owners.setdefault(int(sk), set()).add(name)
    return {k: sorted(v) for k, v in owners.items()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skill", type=int, action="append", help="compile just these ids")
    ap.add_argument("--out", default=os.path.join(SERVER, "battle_data/skills"),
                    help="output DIRECTORY (split per cast)")
    ap.add_argument("--stats", action="store_true")
    args = ap.parse_args()

    rows = dd.rows("skill") or {}
    swings_by_act = {}
    if os.path.isfile(SWINGS_FILE):
        with open(SWINGS_FILE) as f:
            swings_by_act = json.load(f)
    else:
        print(f"warning: {SWINGS_FILE} missing -- run tools/skill_cinematics.py --json",
              file=sys.stderr)

    if args.skill:
        for sid in args.skill:
            print(json.dumps(compile_skill(rows, sid, swings_by_act),
                             indent=2, ensure_ascii=False))
        return

    owners = cast_owners(rows)
    # A skill is filed under every cast that can use it. Duplicating a shared skill keeps
    # each cast file self-contained -- open BELIAL.json and everything Belial does is
    # there -- which is the whole point of splitting.
    buckets, index = {}, {}
    for sid, r in rows.items():
        spec = compile_skill(rows, sid, swings_by_act)
        if not spec:
            continue
        names = owners.get(int(r.get("_group") or sid)) or []
        if names:
            targets = [f"cast/{n}" for n in names]
        elif spec["type"] == "status":
            targets = ["_status"]
        elif spec["type"] == "sub_skill":
            targets = ["_sub_skill"]
        else:
            targets = ["_other"]
        for t in targets:
            buckets.setdefault(t, {})[str(sid)] = spec
        index[str(sid)] = targets[0]

    out = args.out
    os.makedirs(os.path.join(out, "cast"), exist_ok=True)
    for name, data in buckets.items():
        path = os.path.join(out, name + ".json")
        with open(path, "w") as f:
            json.dump(data, f, indent=1, ensure_ascii=False, sort_keys=True)
    with open(os.path.join(out, "_index.json"), "w") as f:
        json.dump(index, f, indent=1, sort_keys=True)
    total = sum(len(v) for v in buckets.values())
    print(f"wrote {out}/: {len(buckets)} files, {len(index)} skills "
          f"({total} rows incl. shared duplicates)")

    if args.stats:
        import collections
        ops, unk = collections.Counter(), collections.Counter()
        partial = full = 0
        for sid in index:
            spec = buckets[index[sid]][sid]
            for e in spec["effects"]:
                ops[e["op"]] += 1
            if spec.get("unknown"):
                partial += 1
                for u in spec["unknown"]:
                    unk[u["opcode"]] += 1
            elif spec["effects"]:
                full += 1
        print("  effect ops        :", dict(ops.most_common(8)))
        print("  fully decoded     :", full)
        print("  partially decoded :", partial)
        print("  unknown opcodes   :", dict(unk.most_common(8)))


if __name__ == "__main__":
    main()

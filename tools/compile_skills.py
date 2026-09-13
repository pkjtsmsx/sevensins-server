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
import collections
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
# The side words, in both languages. Counted over every `_note1` in the pack before
# being written down, because two of these were missing and one was in the wrong script:
#
#   自身  8,173 in 4,067 skills      我方  8,324 in 5,549      敵方  4,924 in 3,727
#   自己  2,697 in 2,276 skills  <-- was absent from _GAUGE_SELF entirely
#   敵人    862 in   671 skills  <-- was absent from _GAUGE_ENEMY entirely
#   己方     25 in    25 skills  <-- was absent from _GAUGE_ALLY
#   對方     31 in    31 skills  <-- _GAUGE_ENEMY carried the SIMPLIFIED 对方, and this
#                                    pack is traditional, so it never matched anything
#
# The enemy list is the one that must not be short. `_who_in` tests enemy FIRST and
# `status_target` treats an enemy hit as "fall through to the skill's own targets", so a
# missed enemy word lets an ally word later in the same clause win -- which redirects a
# debuff onto the player's own party. That is the dangerous direction, and 敵人 alone
# appears in 671 skills.
_GAUGE_SELF = re.compile(r"caster|self|its own|自身|自己|我方自身", re.I)
_GAUGE_ALLY = re.compile(r"\ball(y|ies)\b|我方|己方", re.I)
_GAUGE_ENEMY = re.compile(r"enem|敵方|敵人|對方|对方", re.I)
# "the caster's Max HP", "its own ATK", "the target's SPD" -- the owner of a STAT the
# magnitude is computed from, which is not the same thing as the recipient.
_POSSESSIVE_SOURCE = re.compile(
    r"\b(?:the\s+)?(?:caster|self|its\s+own|target|enemy|ally)'?s?\s+"
    r"(?:max(?:imum)?\s*)?(?:HP|ATK|DEF|SPD|CRI|CRT)\b", re.I)

# Where `status_target` recorded the English translation naming a different side than
# the original. Reported at the end of a compile rather than swallowed -- section 3 says
# these are real and there were 20 known ones in a corpus sweep, so a count that grows
# silently is exactly what we do not want.
_WHO_DISAGREEMENTS = []
# (skill id, status, zh verdict, en verdict) where the two languages disagree about
# whether an application is CONDITIONAL. Printed rather than accumulated quietly, for the
# reason the recipient list is: the Chinese wins, and how often it has to is a number
# somebody should watch. See `is_conditional`.
_COND_DISAGREEMENTS = []

_MINUS_PCT = re.compile(r"-\s*\d+(?:\.\d+)?\s*%")
# "increases the Move Gauge of all allies (EXCLUDING THE CASTER) by 10%" -- the excluded
# party is the one place in a clause that names somebody who is NOT the recipient, and
# reading it as one turns an ally-wide buff into a self-buff. Stripped before any
# recipient test, never after.
_EXCLUSION = re.compile(
    r"[(（]?\b(?:excluding|except(?:\s+for)?|other\s+than|but\s+not|不包[括含]|除了)\b"
    r"[^)）,.;]{0,40}[)）]?", re.I)
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


# ---- the Chinese clause readers -----------------------------------------------------
#
# Fallbacks for the four operandless effects whose numbers the English parsers above
# could not find. Each reads the ORIGINAL note (`_note1`), split into clauses on the
# Chinese separators, and looks only inside the clause that names the effect -- the
# same discipline as the English readers, in the language the game was written in.
# Where the English answered, these are never consulted.
_ZH_SPLIT = re.compile(r"[，。；、\n]")
_ZH_PCT = re.compile(r"(\d+(?:\.\d+)?)\s*[%％]")
# "以25%機率", "30%的固定機率", "有40%固定機率" -- a chance, never a magnitude.
_ZH_CHANCE = re.compile(r"(\d+(?:\.\d+)?)\s*[%％]\s*的?(?:固定)?機率")
# "200%攻擊力" -- a coefficient stated inside the clause itself, as opposed to the
# skill's own, which is what `_is_damage_coefficient` checks.
_ZH_ATK_COEF = re.compile(r"(\d+(?:\.\d+)?)\s*[%％]\s*攻擊力")
_ZH_EXTRA_TURN = re.compile(r"再度行動|再次行動|額外回合|額外行動|可以再行動")


# The clause ledger's running totals, printed unconditionally at the end of a compile
# rather than behind `--stats`: a drop in claim coverage is a regression, and it should
# show up in a build nobody asked a question of. `seen` counts clauses the readers found,
# `unclaimed` the ones no opcode took (which the `uncovered_*` pass then emits), and
# `disagree` the times an opcode and its own clause stated different numbers -- the
# opcode wins, per the evidence hierarchy, but how often the pack contradicts itself is
# worth a number rather than a shrug.
LEDGER = collections.Counter()


def _note_disagreement(kind, opcode_value, prose_value):
    """Count an opcode/prose value conflict. The opcode already won; this only tallies."""
    if (opcode_value is not None and prose_value is not None
            and opcode_value != prose_value):
        LEDGER[kind + ".disagree"] += 1


def _count_clauses(r, claimed):
    """Tally one skill's clauses against the ledger: how many seen, how many claimed."""
    for kind, reader in (("heal", zh_heals), ("gauge", zh_gauges),
                         ("cd", zh_cds), ("revive", zh_revives)):
        for i, _entry in reader(r):
            LEDGER[kind + ".seen"] += 1
            if (kind, i) in claimed:
                LEDGER[kind + ".claimed"] += 1


_ZH_SENTENCE_END = re.compile(r"[。\n]")


def _zh_governing_condition(rows, note, index):
    """-> the `requires` governing the clause at `index`, or None.

    A condition governs everything after it until the sentence ends. Michael's passive
    is the case that forced this:

        行動結束後，若敵方存活人數在2人以上，對自身附加全傷害激減，並回復自身體力40%
                     ^^ the gate            ^^ the status      ^^ AND the heal

    Own-fragment-plus-previous finds the gate for the status and misses it for the heal,
    which is two fragments away -- so the damage reduction was correctly withheld in a
    one-enemy fight while the 40% self-heal fired anyway, and a player watched Michael
    heal back to full every turn he was hit. Walking back to the start of the SENTENCE
    is the reading that covers both, and stopping at 。 is what keeps a later sentence's
    condition from leaking onto an unrelated clause.
    """
    parts = [c for c in _ZH_SPLIT.split(note or "") if c.strip()]
    if index >= len(parts):
        return None
    # How far back the sentence goes: the fragments are already split on 。 as well as
    # ，, so walk the RAW text to find which fragment follows the last terminator.
    raw, start = note or "", 0
    seen = 0
    for piece in _ZH_SENTENCE_END.split(raw):
        n = len([c for c in _ZH_SPLIT.split(piece) if c.strip()])
        if seen + n > index:
            start = seen
            break
        seen += n
    for j in range(index, start - 1, -1):
        # A fragment only GOVERNS if it is a condition -- it must carry 若/如果/當+state.
        # `_zh_parse_condition` answers "what condition does this text describe" and is
        # happy to find one in ordinary prose: 復活我方被擊倒的3人 ("revive the 3 DEFEATED
        # allies") matches the kill regex on 擊倒 and came back as {killed: True}, so
        # Michael's Blessing Anthem gated its own revive behind "if this attack killed"
        # and revived nobody. Reported from a device within minutes of shipping it.
        if not _ZH_CONDITIONAL.search(parts[j]):
            continue
        got = _zh_parse_condition(rows, parts[j])
        if got:
            return got
    return None


def _zh_clauses_with(note, word_re):
    """-> [(i, own, prev)] for the clauses of `note` that contain `word_re`.

    `i` is the fragment's index in the note, and it is the CLAUSE'S IDENTITY: every
    reader splits on the same `_ZH_SPLIT`, so index 3 means the same fragment to all of
    them. That is what lets the opcode walker record which clauses it consumed and the
    `uncovered_*` pass emit only the rest, instead of the two sides reconstructing the
    answer from the VALUES they produced -- which is how one clause came to be paid
    twice (Michael's Gate of Judgement healed 20,000 where the prose says 10,000) and
    how two clauses stating the same number collapsed into one.

    Numbers are read from `own` ONLY. The first cut prepended `prev` and read from
    the join, and Poison Injection's gauge came out as 180 -- the damage coefficient
    of the clause before. `prev` exists so a subject stated one clause earlier
    ("行動後", "對我方全體") can name the side when `own` names none.
    """
    parts = [c for c in _ZH_SPLIT.split(note or "") if c.strip()]
    out = []
    for i, c in enumerate(parts):
        if word_re.search(c):
            out.append((i, c, parts[i - 1] if i else ""))
    return out


def _zh_side_of(own, prev, default=None):
    side = _zh_side(own)
    if side is None and prev:
        side = _zh_side(prev)
    return side if side is not None else default


def _zh_magnitude(clause):
    """-> the first percentage in the clause that is NOT a chance, or None."""
    blanked = _ZH_CHANCE.sub(" ", clause)
    m = _ZH_PCT.search(blanked)
    return float(m.group(1)) if m else None


def _zh_side(clause, default=None):
    """-> caster / allies / targets from the clause's own side words."""
    if re.search(r"目標|敵方|敵人|敵全體|對方", clause):
        return "targets"
    if re.search(r"我方|該\d*名|成員|全員", clause):
        return "allies"
    if re.search(r"自身|自己", clause):
        return "caster"
    return default


def zh_heals(r):
    """-> EVERY heal the Chinese states, in order. See zh_heal for the single-value form.

    Plural because a skill routinely states more than one: Michael's Gate of Judgement
    heals the party off ATK and then heals himself for a share of his own pool, and
    Lucifer's passive heals only himself. Returning the first and stopping is what left
    ~190 heal clauses across the corpus compiled as nothing at all -- they are emitted
    per OPCODE, and a clause with no heal opcode behind it had no other way in.
    """
    out = []
    for i, c, prev in _zh_clauses_with(r.get("_note1"), re.compile(r"回復|恢復|補血")):
        if "行動值" in c:                        # a GAUGE recovery, not HP
            continue
        if "復活" in c or "復活" in (prev or ""):
            # 復活我方被擊倒的隨機1人，並回復其50%體力 -- the percentage is the HP the
            # REVIVE brings them back on, not a heal of its own. Reading it as a second
            # effect gave Metatron's Sacrifice a 50% party heal the prose never grants,
            # on top of the revive that already pays it. The two halves are separate
            # FRAGMENTS, split on the comma, so the preceding one has to be checked too.
            continue
        pct = _zh_magnitude(c)
        if pct is None or _is_damage_coefficient(r, pct):
            continue
        basis = "atk" if "攻擊力" in c else (
            "caster_max_hp" if re.search(r"自身|自己", c) and "最大" in c else "max_hp")
        entry = {"percent": pct, "target": _zh_side_of(c, prev, "caster"),
                 "basis": basis}
        # 回復我方體力最低的2人20%體力 -- the N lowest-HP allies, not the whole party.
        low = re.search(r"體力最低的?\s*(\d+)\s*[人名]", c)
        if low:
            entry["target"], entry["count"] = "allies_lowest", int(low.group(1))
        gate = _zh_governing_condition(dd.rows("skill") or {}, r.get("_note1"), i)
        if gate:
            entry["requires"] = gate
        elif re.search(r"體力最低的?(?:目標|者|角色|單位|隊友|夥伴)", c) or (
                re.search(r"體力最低", prev or "") and not re.search(r"我方|敵方", c)):
            # The same recipient with no NUMBER on it -- 回復我方體力最低者, 對我方體力
            # 最低的目標. 95 clauses, all compiled as "the whole party" because the
            # pattern above needs a digit. Punica's Guard Breath is one of them, and it
            # is a passive, so the party got a free heal every turn from a clause that
            # names a single ally. Unstated count is one.
            #
            # The recipient can also sit in the PREVIOUS fragment -- 對我方體力最低的
            # 目標，以自身攻擊力的100%恢復體力 splits on the comma, so the heal verb's own
            # clause names nobody and `_zh_side_of` read the 自身 out of 自身攻擊力, which
            # is the BASIS. That made Punica heal herself. 10 clauses, all hers.
            entry["target"], entry["count"] = "allies_lowest", 1
        out.append((i, entry))
    return out


def zh_heal(r):
    """-> {percent, target, basis} for the FIRST heal clause, or None.

    A heal with no stated side heals the caster: an unstated "回復N%體力" on an attack
    is the actor recovering, not the target.
    """
    heals = zh_heals(r)
    return heals[0][1] if heals else None


def zh_gauges(r):
    """-> EVERY gauge change the Chinese states, in order.

    Plural for the reason `zh_heals` is: returning the first and stopping made the Nth
    gauge OPCODE read the FIRST clause, so Ocean Strike's 行動後目標行動值-10%，自身的
    行動值+10% compiled as -10% to the target twice -- the self buff lost, the debuff
    doubled. A skill stating a gauge change with no opcode behind it got nothing at all.
    """
    note = r.get("_note1") or ""
    out = []
    for i, c, _prev in _zh_clauses_with(note, _ZH_EXTRA_TURN):
        cm = _ZH_CHANCE.search(c)
        entry = {"percent": 100.0, "target": "caster", "source": "prose_zh"}
        if cm:
            entry["chance_pct"] = float(cm.group(1))
        out.append((i, entry))
    for i, c, prev in _zh_clauses_with(note, re.compile(r"行動值")):
        m = re.search(r"行動值\s*([+\-－])\s*(\d+(?:\.\d+)?)\s*[%％]", c)
        if m:
            pct = float(m.group(2)) * (-1 if m.group(1) in "-－" else 1)
        else:
            pct = _zh_magnitude(c)
            if pct is None or _is_damage_coefficient(r, pct):
                continue
            if re.search(r"減少|降低|下降|扣除", c) and not re.search(r"增加|提升|提高|回復", c):
                pct = -pct
            dm = _ZH_ATK_COEF.search(c)
            if dm and "傷害" in c and float(dm.group(1)) == abs(pct):
                # The clause states its OWN damage coefficient. Oresama Golden Wheel's
                # 再以200%攻擊力的2段傷害對敵方行動值最高的敵人進行追擊 mentions 行動值
                # only to pick a target, and the 200 belongs to the pursuit -- it came
                # out as a 200% move-gauge grant to the enemy team. `_is_damage_coef`
                # misses it because it compares against the SKILL's coefficient (440%
                # here), not one written inline. 50 entries across 25 skills.
                continue
        entry = {"percent": pct, "target": _zh_side_of(c, prev, "caster"),
                 "source": "prose_zh"}
        gate = _zh_governing_condition(dd.rows("skill") or {}, note, i)
        if gate:
            entry["requires"] = gate
        cm = _ZH_CHANCE.search(c)
        if cm:
            entry["chance_pct"] = float(cm.group(1))
        out.append((i, entry))
    return out


def zh_gauge(r):
    """-> the FIRST gauge change the Chinese states, or None."""
    gauges = zh_gauges(r)
    return gauges[0][1] if gauges else None


def zh_cds(r):
    """-> [(i, {turns, target})] for EVERY "技能冷卻-1" / "技能加速1回合" clause.

    Plural for the reason `zh_heals` and `zh_gauges` are: this used to return the first
    match and stop, so the Nth cd opcode re-read clause #1 and a skill stating two
    different cooldown changes compiled the first one twice.
    """
    out = []
    for i, c, prev in _zh_clauses_with(r.get("_note1"), re.compile(r"冷卻|技能加速")):
        turns = None
        m = re.search(r"冷卻\s*([+\-－])\s*(\d+)", c)
        if m:
            turns = int(m.group(2)) * (-1 if m.group(1) in "-－" else 1)
        else:
            m = re.search(r"技能加速\s*(\d+)", c)
            if m:
                turns = -int(m.group(1))
            else:
                m = re.search(r"冷卻(減少|增加|延長|縮短)\s*(\d+)", c)
                if m:
                    turns = int(m.group(2)) * (-1 if m.group(1) in ("減少", "縮短") else 1)
        if turns is None:
            continue
        out.append((i, {"turns": turns, "target": _zh_side_of(c, prev, "caster")}))
    return out


def zh_rider(r):
    """-> the rider's percent from "額外造成125%攻擊力傷害" / "恢復6%體力"."""
    note = r.get("_note1") or ""
    # "額外造成125%攻擊力傷害" and the shorter "額外造成200%傷害" (ATK is the default basis).
    m = re.search(r"額外(?:造成|再造成)\s*(\d+(?:\.\d+)?)\s*[%％]\s*(?:攻擊力)?(?:的)?傷害", note)
    if m:
        return {"kind": "bonus_damage", "percent": float(m.group(1)), "source": "prose_zh"}
    # "以200%的攻擊力回復我方體力最低的2人" -- a heal sized in the CASTER's ATK, landing
    # on the N lowest-HP allies (1 when unstated). The engine's rider heal used to reach
    # only the caster; `target`/`count` carry the real recipients.
    m = re.search(r"以\s*(\d+(?:\.\d+)?)\s*[%％]\s*的?攻擊力\s*(?:回復|恢復)([^，。]*)", note)
    if m:
        tail = m.group(2)
        cnt = re.search(r"最低的?\s*(\d+)\s*人", tail)
        out = {"kind": "heal", "percent": float(m.group(1)), "source": "prose_zh"}
        # 最低 is what makes it the lowest-HP N; 我方 alone is the WHOLE party. Reading
        # a bare 我方 as allies_lowest sent Michael's 以200%攻擊力恢復我方全體體力 --
        # "restores HP of ALL allies" -- to one ally, and because the heal opcode read
        # the same clause correctly, he paid it twice and healed himself 20,000 where
        # the prose says 10,000.
        if "最低" in tail:
            out["target"] = "allies_lowest"
            out["count"] = int(cnt.group(1)) if cnt else 1
        elif "我方" in tail:
            out["target"] = "allies"
        return out
    m = re.search(r"(?:回復|恢復)\s*(\d+(?:\.\d+)?)\s*[%％]\s*(?:的)?體力", note)
    if m:
        return {"kind": "heal", "percent": float(m.group(1)), "source": "prose_zh"}
    return None


def _en_granting_fragment(note, name):
    """-> the fragment of `note` that both names `name` and grants it, or None.

    Same discipline as `passive_who` and `status_target`: narrow to the fragment before
    reading anything out of it.
    """
    for sentence in re.split(r"(?<=[.!?])\s+", note or ""):
        for frag in _split_fragments(sentence):
            if name.lower() in frag.lower() and _GRANT_VERB.search(frag):
                return frag
    return None


def _zh_chance_fragment(note, name):
    """-> (fragment, chance match) where `note` states odds for granting `name`, or None.

    `_GRANT_VERB` alone is too narrow HERE, and being narrow is expensive: the fragment
    falls through to the English, and on this particular number the English is wrong
    often enough to matter. Two of twelve blind samples disagreed --

        \u4ee535%\u6a5f\u7387\u9b45\u60d1\u6575\u65b9\u96a8\u6a5f2\u4eba   vs  "a 40% chance to inflict Enchant on 2 random enemy targets"
        \u4ee530%\u6a5f\u7387\u4f7f\u6575\u5168\u9ad4\u51cd\u7d50   vs  "a 40% chance to inflict Freeze on all enemies"

    -- and in both the Chinese fragment grants with a form `_GRANT_VERB` does not list:
    the causative \u4f7f/\u4ee4, or the status name used bare as the verb (5%\u6a5f\u7387\u6688\u7729, 25%\u6a5f\u7387\u4e2d\u6bd2).
    Widening the verb list would change every other reader that shares it, so the
    widening lives here: within one fragment, a status named AFTER the stated odds is
    governed by them. \u3001 is deliberately not a fragment separator (see _CLAUSE_SPLIT), so
    \u4ee530%\u6a5f\u7387\u4f7f\u76ee\u6a19\u6688\u7729\u3001\u4e2d\u6bd2 correctly gives both statuses the one stated chance.
    """
    want = sp.norm_name_zh(name)
    for sentence in re.split(r"[\u3002\n]", note or ""):
        for frag in _split_fragments(sentence):
            if want not in sp.norm_name_zh(frag):
                continue
            m = _ZH_CHANCE.search(frag)
            if m and (_GRANT_VERB.search(frag)
                      or want in sp.norm_name_zh(frag[m.end():])):
                return frag, m
            if _GRANT_VERB.search(frag):
                return frag, None     # granted here, and states no odds
    return None


def status_chance(r, zh_name, en_name):
    """-> {"chance_pct": N, "chance_source": ...} for a status application, or {}.

    The opcode says an application HAPPENS; only the prose says how LIKELY, and it says
    so constantly -- \u4ee540%\u7684\u6a5f\u7387\u9644\u52a0\u6311\u91c1, "30% fixed chance to inflict Stun". Without this,
    op 113 rolled a flat 0.75 stand-in and op 112 landed every time, so a stated 10%
    landed seven and a half times too often and a stated 80% landed too seldom.

    **Not gated on opcode 113.** The contract doc read 112 as "guaranteed" and 113 as
    "with a chance", inferred from the operand table alone -- and the prose disagrees:
    648 op-112 sites state a probability in the very fragment that grants the status
    (Wise Prediction III: \u4ee5\u4e0b70%\u7684\u6a5f\u7387\u9644\u52a0\u6311\u91c1). The client settles nothing either way,
    because it never reads the opcode script at all: `DesignSkillRow` exposes no Action
    property, and `AddSkillScripts` (0x1aacb00) walks `_action` only to find opcode 4 so
    it can preload that sub-skill's cinematic. The script was the retail SERVER's, and
    the prose is the only authority we have, so where it states odds we roll them --
    whichever opcode carries the row. Where it states none, 113 keeps its stand-in.

    STRICTLY the granting fragment. Walking back through earlier fragments -- the way
    `zh_condition_for` walks back for a \u82e5 -- was measured and is wrong: of the 118 sites
    it reached, every one sampled took a probability belonging to a DIFFERENT status
    stated earlier in the same sentence.

        105%\u653b\u64ca\u529b\u76843\u6bb5\u50b7\u5bb3\uff0c25%\u6a5f\u7387\u4f7f\u76ee\u6a19\u6688\u7729\uff1b\u884c\u52d5\u524d\u7372\u5f97\u6c23\u5408\u6548\u679c
                            ^^^^ Daze's odds       ^^^^ Spirit, which states none

    14 blind samples at distance 0 were all correct; every sample at distance >= 1 was
    wrong. A status whose own fragment states no probability therefore has none.

    One known approximation, recorded rather than fixed: \u6bcf\u6bb5\u50b7\u5bb3\u90fd\u670950%\u56fa\u5b9a\u6a5f\u7387 is a
    roll PER SWING, and the engine applies statuses once per cast, so a per-swing chance
    comes out weaker than retail on a multi-hit skill.
    """
    if zh_name:
        got = _zh_chance_fragment(r.get("_note1"), zh_name)
        if got:
            frag, m = got
            if m:
                return {"chance_pct": float(m.group(1)), "chance_source": "prose_zh"}
            return {}          # the original states the grant and states no odds
    if en_name:
        frag = _en_granting_fragment(r.get("_note1_en"), en_name)
        if frag:
            m = _STATED_CHANCE.search(frag)
            if m:
                return {"chance_pct": float(m.group(1) or m.group(2)),
                        "chance_source": "prose"}
    return {}


# --- op 6: an amount sized off the CASTER'S OWN HP POOL -----------------------------
#
# Decoded 2026-08-29 by prose clustering, the only tool there is (the client never reads
# the opcode script -- contract doc 6.1.1). Of 125 rows carrying op 6, 95 say \u7576\u524d/\u7576\u4e0b/
# \u76ee\u524d\u9ad4\u529b (current HP) -- a 74x lift over the 0.8% corpus baseline -- and 25 more, all one
# cast's Matcha Sundae, say \u6700\u5927\u9ad4\u529b. The verb is prose, exactly as with op 1: bonus
# damage (\u984d\u5916\u5c0d\u76ee\u6a19\u9020\u6210\u81ea\u8eab8%\u7576\u524d\u9ad4\u529b\u7684\u50b7\u5bb3) or a heal (\u4ee5\u746a\u9580\u7576\u524d\u9ad4\u529b\u768430%\u6062\u5fa9\u6211\u65b93\u540d\u9ad4\u529b\u6700\u4f4e\u7684\u8840\u91cf,
# \u6062\u5fa9\u81ea\u5df1\u7684\u9ad4\u529b(\u76f8\u7576\u65bc8%\u7684\u7576\u524d\u9ad4\u529b)). The remaining 4 rows are a mob Slash with no
# number at all and stay a reported skip. Emitted as an attack_rider with a `basis`,
# because that is what it is: the op-1 shape with a different stat behind the percent.
OP_HP_RIDER = 6
_ZH_HP_POOL = re.compile(r"(\u7576\u524d|\u7576\u4e0b|\u76ee\u524d|\u6700\u5927)\u9ad4\u529b")


def zh_hp_rider(r):
    """-> {kind, percent, basis, target?, count?, requires?} for op 6, or None."""
    note = (r.get("_note1") or "").split("\u203b")[0]
    for sentence in re.split(r"[\u3002\n]", note):
        parts = _split_fragments(sentence)
        for i, frag in enumerate(parts):
            pm = _ZH_HP_POOL.search(frag)
            if not pm:
                continue
            basis = "caster_max_hp" if pm.group(1) == "\u6700\u5927" else "caster_current_hp"
            pct = _zh_magnitude(frag)
            if pct is None:
                continue
            if re.search(r"\u9020\u6210|\u50b7\u5bb3", frag) and not re.search(r"\u6062\u5fa9|\u56de\u5fa9", frag):
                out = {"kind": "bonus_damage", "percent": pct, "basis": basis}
            elif re.search(r"\u6062\u5fa9|\u56de\u5fa9", frag):
                out = {"kind": "heal", "percent": pct, "basis": basis}
                cnt = re.search(r"(?:\u6211\u65b9|\u6211\u65b9\u9ad4\u529b\u6700\u4f4e\u7684?)\s*(\d+)\s*[\u540d\u4eba]", frag)
                if "\u6211\u65b9" in frag or "\u6700\u4f4e" in frag:
                    out["target"] = "allies_lowest"
                    out["count"] = int(cnt.group(1)) if cnt else 1
            else:
                continue
            out["source"] = "prose_zh"
            # \u82e5\u81ea\u8eab\u64c1\u6709\u5171\u4eab\u76db\u5bb4\uff0c\u984d\u5916\u2026 -- the \u82e5 fragment before it gates it, same walk-back
            # as zh_condition_for.
            # `\u82e5` anywhere in the fragment, not only at its start: the corpus writes
            # \u653b\u64ca\u6642\u82e5\u76ee\u6a19\u64c1\u6709\u9006\u98a8 and \u884c\u52d5\u5f8c\u82e5\u76ee\u6a19\u64c1\u67094\u5c64\u9b54\u85e5\u4e4b\u543b with a timing word first. A
            # governing \u82e5 the parser cannot read still becomes a gate -- an unreadable
            # one, which `_condition_met` answers None and the policy rolls. \u82e5\u6575\u65b9\u5b58\u6d3b\u4eba\u6578
            # \u5927\u65bc3\u4eba is that case; firing it every time is not "unknown", it is wrong.
            for j in range(i, max(-1, i - 3), -1):
                # \u7576 is also the first character of \u7576\u524d\u9ad4\u529b and the middle of \u76f8\u7576\u65bc --
                # the roadmap's own listed trap -- so those are blanked before asking
                # whether a \u7576 opens a condition here.
                probe = re.sub(r"\u7576[\u524d\u4e0b]\u9ad4\u529b|\u76f8\u7576\u65bc", " ", parts[j])
                if _ZH_COND_START.search(probe.strip()) or re.search(r"\u82e5|\u7576", probe):
                    got = _zh_parse_condition(dd.rows("skill") or {}, parts[j])
                    out["requires"] = got or {"unparsed": parts[j].strip()[:60]}
                    break
            return out
    return None


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


# --- op 115's delta and recipient ---------------------------------------------------
#
# All 1,012 sites carried no delta and no recipient, so the engine could not run any of
# them (`modify_cd` was a `pass`) and `CD Reduction Block` had nothing to block. Same
# shape as the gauge: the number sits next to the words "Skill CD"/"cooldown", and a
# turn count is a small integer, not a percentage.
_CD_WORD = re.compile(r"skill\s*cd|cooldown|冷卻", re.I)
_NUM_WORD = r"(\d+|" + "|".join(sp.WORD_NUM) + r")"


def _num_token(tok):
    tok = (tok or "").strip().lower()
    return int(tok) if tok.isdigit() else sp.WORD_NUM.get(tok)


_CD_TURNS = re.compile(r"\bby\s+" + _NUM_WORD + r"\b|\b" + _NUM_WORD +
                       r"\s*turns?\b|[+\-]\s*(\d+)", re.I)
_CD_DOWN = re.compile(r"reduc|decreas|refresh|lower|shorten|減少|降低", re.I)
_CD_UP = re.compile(r"increas|delay|extend|\+|增加", re.I)


def cd_effect(r):
    """-> {turns, target} for op 115, read from the cooldown clause only.

    `turns` is SIGNED: negative refreshes (shortens), positive delays. Unstated stays
    None and the engine skips the effect rather than guessing a direction -- a sign
    error here silently hands a cast a free turn.
    """
    note = r.get("_note1_en") or ""
    m = _CD_WORD.search(note)
    if not m:
        return {"turns": None, "target": None}
    clause = note[max(0, m.start() - 60):m.end() + 80]
    turns = None
    tm = _CD_TURNS.search(note[m.end():m.end() + 60]) or \
        _CD_TURNS.search(note[max(0, m.start() - 40):m.start()])
    if tm:
        tok = next((g for g in tm.groups() if g), None)
        turns = _num_token(tok)
    if turns is not None:
        # "increase" wins a tie, same rule as the gauge: an unrelated "removes"/"reduces"
        # in the same sentence must not flip a delay into a refresh.
        if _CD_DOWN.search(clause) and not _CD_UP.search(clause):
            turns = -turns
    return {"turns": turns, "target": clause_target_in(clause)}


def clause_target_in(clause):
    """clause_target's decision, for a clause the caller already located."""
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

    clause = _EXCLUSION.sub(" ", before + note[m.start():m.end() + 80])
    if pct is not None and _GAUGE_DOWN.search(clause) and not _GAUGE_UP.search(clause):
        pct = -pct
    elif pct is not None and _MINUS_PCT.search(clause) and not _GAUGE_UP.search(clause):
        # A bare MINUS is a direction word too. "Dedication Delay I: when knocked out by
        # direct damage, the Move Gauge of all enemies -30%" carries no verb at all, so
        # the verb test above left it +30 -- a gauge CUT on the enemy team compiled as a
        # gauge GIFT to it, and every unit holding such a passive handed the opposition a
        # third of a turn. Found from a device: units acting on a bar that was not full.
        pct = -abs(pct)

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

_COEF = re.compile(r"(\d+(?:\.\d+)?)\s*[%％]\s*(?:of\s+)?(ATK|DEF|Max HP|HP)", re.I)
# Chinese writes it either way round: `攻擊力95%` or `95%攻擊力`.
# `％` (full-width) as well as `%`: "360％防禦力的傷害" is how every rank of Sweets
# Sweet Heart is written, and with `%` alone the whole family compiled to a damage
# effect with no coefficient -- a skill that animates and deals nothing.
_COEF_ZH = re.compile(
    r"(攻擊力|最大體力|防禦力)\s*(\d+(?:\.\d+)?)\s*[%％]"
    r"|(\d+(?:\.\d+)?)\s*[%％]\s*(?:的)?\s*(攻擊力|最大體力|防禦力)")
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


_CAST_GLOSSARY_ZH = None


def cast_glossary_zh(rows):
    """cast_glossary, read from `_note1`: {skill id: {zh status key: body}}."""
    global _CAST_GLOSSARY_ZH
    if _CAST_GLOSSARY_ZH is not None:
        return _CAST_GLOSSARY_ZH
    out = {}
    for char in (dd.rows("char") or {}).values():
        skills = [s for s in (char.get("_skills") or []) if s]
        if not skills:
            continue
        merged = {}
        for sid in skills:
            row = rows.get(sid) or {}
            for name, body in sp.glossary_lines_zh(row.get("_note1")).items():
                merged.setdefault(sp.norm_name_zh(name), body)
        for sid in skills:
            base = (rows.get(sid) or {}).get("_group") or sid
            for other, orow in rows.items():
                if (orow.get("_group") or other) == base:
                    out.setdefault(other, {}).update(merged)
    _CAST_GLOSSARY_ZH = out
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


def zh_inline_shield(r):
    """-> numbers for a shield the note sizes INLINE, or None.

    The clause that names the shield carries its size and, in parentheses, its turns:
    "並對擁有侍奉的目標附加15000點護盾(3回合)", "張開防禦護盾7000點(2回合)", or a
    percentage form "施放相當於攻擊力60%的護盾". The glossary readers never see these
    because there is no `※ 護盾：` line -- the whole definition is the clause.
    """
    note = r.get("_note1") or ""
    for c in _ZH_SPLIT.split(note):
        if "盾" not in c:
            continue
        out = sp.parse_zh(c)                    # duration/percent/flat/basis, same rules
        m = re.search(r"\((\d+)\s*回合\)", c)
        if m and out.get("duration") is None:
            out["duration"] = int(m.group(1))
        if out.get("flat") or out.get("magnitude") is not None:
            out["source"] = "inline_zh"
            return out
    return None


def status_numbers(rows, skill_row, status_id):
    """-> the duration/magnitude for THIS skill applying THIS status, with provenance.

    Three tiers, and `source` says which was used so a wrong number is attributable:
      "skill"          -- the applying skill's own `* Name: ...` line (authoritative)
      "cast"           -- another skill of the SAME CAST defines it (see cast_glossary)
      "corpus_default" -- the modal value across the pack, agreement >= threshold
      None             -- nothing stated; the engine must apply a policy, knowingly
    """
    # THE ORIGINAL LANGUAGE FIRST. The English glossary renames statuses mid-sentence
    # (Scorpion Kiss: row `SPD UP(5)`, line `*Boost Up:`), so the English join missed
    # 3,749 of 6,205 cast applications and every one of those shipped with no
    # magnitude, stacks or duration -- a status that lands, draws an icon and does
    # nothing. The Chinese line is keyed by the row's own `_name` and parses with a
    # fixed vocabulary; see status_prose.parse_zh. English remains the fallback, and
    # the corpus default the last resort, and `source` says which one answered.
    zh_name = (rows.get(status_id) or {}).get("_name")
    zh_key = sp.norm_name_zh(zh_name) if zh_name else None
    if zh_key:
        zh_lines = {sp.norm_name_zh(k): v for k, v in
                    sp.glossary_lines_zh(skill_row.get("_note1")).items()}
        if zh_key in zh_lines:
            got = sp.parse_zh(zh_lines[zh_key])
            got["source"] = "skill_zh"
            return got
        own_zh = cast_glossary_zh(rows).get(skill_row.get("_id") or 0, {})
        if zh_key in own_zh:
            got = sp.parse_zh(own_zh[zh_key])
            got["source"] = "cast_zh"
            return got

    # A SHIELD stated inline rather than on a glossary line: "附加15000點護盾(3回合)",
    # "張開防禦護盾7000點(2回合)". 105 of the 151 shield applications on cast skills
    # had no line of their own and were unsized -- and an unsized shield absorbs nothing.
    if zh_name and re.search(r"盾", zh_name) or re.search(
            r"shield", (rows.get(status_id) or {}).get("_name_en") or "", re.I):
        got = zh_inline_shield(skill_row)
        if got:
            return got

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


def effects(rows, r, claimed=None):
    """-> (effects, unknown) read straight off the opcode slots.

    `claimed` is the clause ledger: a set this fills with (kind, clause index) for every
    prose clause an opcode consumed, so `uncovered_*` can emit exactly the clauses
    nothing took. Keyed by KIND as well as index because one fragment can state two
    families -- `_ZH_SPLIT` cuts on ，。；、 and not on 並, so 復活…並回復其50%體力 is a
    single fragment that both the revive and heal readers see (224 skills), and a bare
    index would let the first claim silence the second.

    A repeated (op, operand) pair is emitted VERBATIM, one entry per slot, carrying its
    slot index. It is ambiguous by nature -- sometimes stacks, sometimes the same effect
    under two different triggers -- and folding repeats into a count is wrong about half
    the time. See contract doc 6.4.1.
    """
    acts, ids = r.get("_action") or [], r.get("_actID") or []
    out, unknown = [], []
    # Prose clauses are consumed IN ORDER, one per opcode of that kind. Re-reading the
    # note per opcode always returned clause #1, so a skill with two gauge opcodes and
    # two gauge clauses got the first one twice -- Ocean Strike's 目標行動值-10% and
    # 自身的行動值+10% compiled as -10% to the target, twice. The queues run out rather
    # than wrap: more opcodes than clauses means the extras carry no prose, which is the
    # honest answer and is what `clause_percent` already falls back to.
    if claimed is None:
        claimed = set()
    zh_gauge_q, zh_heal_q = list(zh_gauges(r)), list(zh_heals(r))
    zh_cd_q, zh_revive_q = list(zh_cds(r)), list(zh_revives(r))
    for i, op in enumerate(acts):
        if not op:
            continue
        aid = ids[i] if i < len(ids) else 0
        if op in (OP_APPLY, OP_APPLY_CHANCE) and aid in rows:
            meta = status_meta(rows, aid)
            out.append({"op": "apply_status", "slot": i,
                        "chance": op == OP_APPLY_CHANCE,
                        # The stated odds, from the fragment that grants it. See
                        # status_chance: this is emitted for op 112 as well, because
                        # the prose states probabilities there too.
                        **status_chance(r, (rows.get(aid) or {}).get("_name"),
                                        meta.get("name")),
                        "conditional": is_conditional(
                            r, meta.get("name"), (rows.get(aid) or {}).get("_name")),
                        "requires": (condition_requires(r, meta.get("name"))
                                     or zh_condition_for(rows, r, (rows.get(aid) or {}).get("_name"))),
                        # The status row's `_name` is the ORIGINAL-language name, which
                        # status_target needs to find the clause in `_note1`.
                        "recipient": status_target(r, meta.get("name"),
                                                   (rows.get(aid) or {}).get("_name")),
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
            entry = {"op": "follow_up", "slot": i, "skill": aid,
                     "name": (rows.get(aid) or {}).get("_name_en")
                             or (rows.get(aid) or {}).get("_name"),
                     # "若目標擁有出血…進行追擊": the 若 fragment before the 追擊 gates it.
                     "requires": zh_condition_for(rows, r, None, follow_up=True)}
            # The pursuit's own damage and odds, which live in the PARENT's prose
            # because the sub-skill's row has no numbers at all. See zh_pursuit_numbers.
            entry.update(zh_pursuit_numbers(r))
            out.append(entry)
        elif op == OP_MODIFY_CD:
            entry = {"op": "modify_cd", "slot": i}
            entry.update(cd_effect(r))
            ci, zh = zh_cd_q.pop(0) if zh_cd_q else (None, None)
            if zh:                                   # the original language wins
                _note_disagreement("cd", entry.get("turns"), zh.get("turns"))
                claimed.add(("cd", ci))
                entry.update(zh)
            out.append(entry)
        elif op == OP_HP_RIDER and zh_hp_rider(r):
            out.append({"op": "attack_rider", "slot": i, "opcode": OP_HP_RIDER,
                        **zh_hp_rider(r)})
        elif op == OP_ATTACK_RIDER and (attack_rider(r) or zh_rider(r)):
            rider = attack_rider(r) or {}
            zh = zh_rider(r)
            if zh:                                   # the original language wins
                rider = {**rider, **zh}
            out.append({"op": "attack_rider", "slot": i, **rider})
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
                # THE ORIGINAL LANGUAGE WINS where it states a value; English fills the
                # rest. "以25%機率恢復12%體力" read in English gave the CHANCE as the
                # magnitude; the Chinese reader blanks chances before it looks.
                ci, zh = zh_gauge_q.pop(0) if zh_gauge_q else (None, None)
                if zh:
                    claimed.add(("gauge", ci))
                    _note_disagreement("gauge", entry.get("percent"),
                                       zh.get("percent"))
                    if zh.get("percent") is not None:
                        entry.update(zh)
                    elif entry.get("target") is None:
                        entry["target"] = zh.get("target")
            elif name in ("heal", "revive"):
                # Same recipient problem as the gauge: a heal on an attack skill goes to
                # allies, not to the enemy being hit.
                entry["target"] = clause_target(r, name)
                if name == "revive":
                    ci, zh = zh_revive_q.pop(0) if zh_revive_q else (None, None)
                    if zh:
                        claimed.add(("revive", ci))
                        _note_disagreement("revive", entry.get("percent"),
                                           zh.get("percent"))
                        if zh.get("percent") is not None:
                            entry["percent"], entry["source"] = zh["percent"], "prose_zh"
                        if zh.get("count"):
                            entry["count"] = zh["count"]
                        if zh.get("requires"):
                            entry["requires"] = zh["requires"]
                        entry["target"] = zh.get("target") or entry.get("target")
                if name == "heal":
                    entry["basis"] = heal_basis(r)
                    ci, zh = zh_heal_q.pop(0) if zh_heal_q else (None, None)
                    if zh is None:
                        # No clause left for this opcode. `clause_percent`/`heal_basis`
                        # above already filled it from the WHOLE note, which is the
                        # "always clause #1" bug in another dress -- a second heal
                        # opcode re-derives the first clause's number. Flagged rather
                        # than dropped here, because a skill with a heal opcode and no
                        # prose at all still has to emit something.
                        entry["_no_clause"] = True
                    if zh:
                        claimed.add(("heal", ci))
                        _note_disagreement("heal", entry.get("percent"),
                                           zh.get("percent"))
                        if zh.get("requires"):
                            # The gate travels with the CLAUSE, so an opcode that
                            # claimed one inherits it. Michael's 40% self-heal has a
                            # heal opcode behind it, so it never went through the prose
                            # pass and kept firing while the status beside it -- same
                            # clause, same gate -- was correctly withheld.
                            entry["requires"] = zh["requires"]
                        if zh.get("percent") is not None:
                            entry["percent"], entry["source"] = zh["percent"], "prose_zh"
                            entry["basis"] = zh["basis"]
                        if entry.get("target") is None or zh.get("target"):
                            entry["target"] = zh["target"] or entry.get("target")
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


def _clause_for(note, name, _with_strength=False):
    """-> the sentence naming this status, or None.

    Tries the qualifier-stripped name too: the prose writes "Cast Serum Injection on up
    to 1 allies with the highest ATK" while the status rows are `Serum Injection(ATK)`
    and `(CRT)`. Without this the clause is never found, so the status looks
    unconditional and un-retargeted -- which is how a party buff ended up on the boss.
    """
    if not note or not name:
        return (None, False) if _with_strength else None
    wanted = [name.lower()]
    bare = re.sub(r"\s*\([^)]*\)\s*$", "", name).strip().lower()
    if bare and bare != wanted[0]:
        wanted.append(bare)
    # An immunity row is NAMED "Freeze Immunity" and DESCRIBED as "gains immunity to
    # Freeze for 3 turns" -- the words are the same and the order is not, so a literal
    # search never found the clause. Every such status then looked undocumented, and the
    # passive compiler turned a stated 3-turn immunity into a permanent one.
    m = re.match(r"(.*?)\s*immunity\s*(?:\([^)]*\))?$", bare or "", re.I)
    if m and m.group(1):
        for part in re.split(r"[/,]| and ", m.group(1)):
            part = part.strip().lower()
            if part:
                wanted += [f"immunity to {part}", f"immune to {part}",
                           f"immunes to {part}"]

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
        # Search past the clause's own LABEL, and only past that. A clause is written
        # "Diligence: When a battle starts, inflict Diligence on ...", so the name's
        # first occurrence is the label at offset 0 -- with no text in front of it, no
        # granting verb can precede it, and the sentence that literally says "inflict
        # Diligence" was graded a weak match. Scanning EVERY occurrence instead fixes
        # that and breaks something worse: Lucifer's "if the caster is affected by The
        # Divine, grants the caster The Fallen ... removes its The Divine effect" then
        # matches on its own trailing mention, which is exactly the first-sentence
        # capture the comment above exists to prevent.
        low = sentence.lower()
        label = re.match(r"[^:]{1,40}:\s*", low)
        base = label.end() if label else 0
        for w in wanted:
            at = low.find(w, base)
            if at < 0:
                continue
            if fallback is None:
                fallback = sentence
            if _GRANTS.search(low[:at]):
                return (sentence, True) if _with_strength else sentence
            break
    return (fallback, False) if _with_strength else fallback


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


# Verbs that GRANT a status, as opposed to testing for one. 擁有/若 ("has"/"if") are
# deliberately absent: a fragment that only tests for the status is a condition, and its
# side words describe who must be holding it, not who is about to receive it.
_GRANT_VERB = re.compile(
    r"附加|賦予|給予|獲得|得到|疊加|張開|施加"
    r"|\bgrant|\bgains?\b|\bapplie|\bapply\b|\binflict|\bcasts?\b|\breceive", re.I)


# Like _FRAGMENT_SPLIT but WITHOUT 、 -- and that difference is the whole point.
#
# 、 is the Chinese LIST separator, not a clause separator: it enumerates nouns that
# share one verb and one recipient. Splitting on it tears a shared grant apart and
# leaves every item after the first as a bare noun with nothing attached to it:
#
#   行動前對我方全體附加貫通、祝福
#   |________ grants ALL ALLIES ______|  |__ a bare list tail __|
#
# so Blessing came back with no recipient while Penetrate, one item earlier in the same
# list, correctly came back as allies. Same for 使自己獲得加速、毅然.
_CLAUSE_SPLIT = re.compile(r",\s*|\s+and\s+|;\s*|，|並|且|。|；|！|？", re.I)


_SENTENCE_END = re.compile(r"[。；！？.!?]")
# "on the target", 對目標 -- a stated recipient that is not one of the three SIDES.
_EXPLICIT_TARGET = re.compile(r"\btargets?\b|目標|對象|敵人|敵方", re.I)


def _split_fragments(clause):
    """-> the clause as a list of fragments, in order."""
    parts, last = [], 0
    for m in _CLAUSE_SPLIT.finditer(clause):
        parts.append(clause[last:m.start()])
        last = m.end()
    parts.append(clause[last:])
    return parts


def _fragments_naming(clause, name):
    """-> [(index, fragment)] for every clause-level fragment mentioning `name`."""
    want = name.lower()
    return [(i, p) for i, p in enumerate(_split_fragments(clause))
            if want in p.lower()]


def _who_with_inheritance(parts, i):
    """-> the side governing fragment `i`, looking back for the verb it hangs off.

    A fragment that names NEITHER a side NOR a granting verb is a bare list tail: the
    verb and the recipient are on the head of its list, one or more fragments back.
    English does this with "and" exactly as Chinese does it with 、 --

        grants the caster The Bride │ CC Immunity effects
        |__ verb + recipient _______| |__ names neither __|

    -- and "CC Immunity" alone answered None while "The Bride", one item earlier in the
    same list, answered caster. Walking back finds the head.

    Deliberately narrow, because inheriting a recipient is exactly the kind of guess
    that puts a party buff on a boss: it only runs when the naming fragment says nothing
    at all, it stops at the first fragment that DOES grant, and it never crosses a
    sentence boundary into a different statement.
    """
    here = _who_in(parts[i])
    if here or _GRANT_VERB.search(parts[i]):
        return here
    # A tail that names the TARGET has stated its recipient and must not inherit:
    #
    #     grants the caster Iron Wrist │ 2 stacks of Beer on the target
    #                                     ^^^^^^^^^^^^^^ says who, just not in the
    #                                                    self/ally/enemy vocabulary
    #
    # `_who_in` has no token for "target" -- deliberately, since "the skill's target" is
    # the FALLBACK rather than a named side -- so without this the walk-back sails past
    # an explicit recipient and hands Beer to its caster. Returning None here is that
    # same fallback, which is what the prose actually asked for.
    if _EXPLICIT_TARGET.search(parts[i]):
        return None
    for j in range(i - 1, -1, -1):
        if _SENTENCE_END.search(parts[j]):
            break
        if _GRANT_VERB.search(parts[j]):
            return _who_in(parts[j])
    return None


def _who_for(note, name):
    """-> the side named in the fragment that grants `name`, or None if it is not named.

    THE MISS MUST NOT FALL BACK TO THE WHOLE CLAUSE. `_fragment_for` returns its input
    unchanged when it cannot find the name, which is a sane default for a narrowing
    helper and a disastrous one here: `_who_in` then reads side words from anywhere in
    the sentence, including from a CONDITION that names the caster.

    Status rows are named with a stack qualifier -- `激痛(5)`, `蓄勢(3)`, `加速(5)` --
    that the prose never writes, so the name is missed on exactly the statuses that
    stack. `_clause_for` already strips it to find the sentence; this has to strip it
    again to find the fragment, or every stacking status reads the whole sentence.

    Prelude is the case that proves the cost. Its clause is

        造成傷害時附加激痛效果，若自身擁有可解除的「持續傷害」狀態，...
        ^^^^ inflicts Agony (on the target)   ^^^^ "if the CASTER has..." -- a condition

    With `激痛(5)` the name is not found, the whole sentence is read, the condition's
    自身 wins, and Agony -- a DEF debuff -- is recorded as landing on its own caster.
    With the qualifier stripped the fragment is the first clause alone and the answer is
    correctly None.
    """
    if not note or not name:
        return None
    clause = _clause_for(note, name)
    if not clause:
        return None
    bare = re.sub(r"\s*[(（][^)）]*[)）]\s*$", "", name).strip()
    for want in (name, bare):
        if not want or want.lower() not in clause.lower():
            continue
        parts = _split_fragments(clause)
        frags = _fragments_naming(clause, want)
        if not frags:
            continue
        # PICK THE FRAGMENT THAT GRANTS, not the first one that mentions the name.
        # `_clause_for` already prefers the SENTENCE where a granting verb precedes the
        # name; the same rule is needed one level down, because a status is routinely
        # named twice in one sentence -- once as a condition and once as the thing
        # applied -- and it is also routinely a substring of the clause's own label.
        #
        #   盛怒萬解：...都將為自身疊加1層「盛怒」
        #   ^^^^ the LABEL contains the status name    ^^^^ the fragment that grants it
        #
        #   若...目標擁有全傷害激減，使我方全體獲得全傷害激減
        #   ^^^^ a CONDITION (擁有 = "has")   ^^^^ the grant (獲得 = "gains")
        #
        # Taking the first match answered None for both -- Wrath stopped landing on its
        # own caster and Delusion's party-wide damage reduction stopped being party-wide.
        for _i, frag in frags:
            if _GRANT_VERB.search(frag):
                return _who_in(frag)
        return _who_with_inheritance(parts, frags[0][0])
    # NEITHER spelling appears literally, yet `_clause_for` still found a clause -- so it
    # matched through one of its own aliases. That is the immunity shape: the row is
    # NAMED "Charm/Confuse/Headwind Immunity" and the prose DESCRIBES it as "the caster
    # permanently gains immunity to Confuse, Charm and Headwind", sharing no substring
    # with the name at all. Narrowing cannot find what is not there, so fall back to
    # `_fragment_for`, which is what this function did before narrowing existed.
    #
    # This fires only when both spellings miss, so it cannot reintroduce the whole-clause
    # read that the qualifier stripping above exists to prevent -- those names ARE found.
    clause_l = clause.lower()
    if not any(w and w.lower() in clause_l for w in (name, bare)):
        return _who_in(_fragment_for(clause, bare or name))
    return None



# ---- Chinese conditions ------------------------------------------------------------
#
# 740 status applications and 399 follow-ups on cast skills carried a 若/當 condition
# the compiler could not read, so they fired UNCONDITIONALLY -- the "too strong" failure.
# The engine already evaluates one shape, `requires` = "holds status X"; that is 362 of
# them. HP thresholds are 134 more and a small engine addition. Both are read here from
# the condition fragment that precedes the grant in the same sentence.
_ZH_COND_START = re.compile(r"^(?:若|當|如果|在)")
_ZH_COND_HOLDS = re.compile(
    r"(?P<who>目標|自身|自己|敵方|我方|對方)?[^，。]*?"
    r"(?P<neg>未|沒有|不)?(?:擁有|持有|附有|處於|帶有)"
    r"(?:\d+層)?(?:任[一何])?「?(?P<name>[^」，。的時之狀]{1,14}?)」?(?:狀態|效果)?(?:時|的|則|，|$)")
_ZH_COND_HP = re.compile(
    r"(?P<who>目標|自身|自己|敵方|我方)?(?:的)?(?:體力|血量|HP)\s*"
    r"(?P<cmp>高於|低於|不高於|不低於|大於|小於|[<>＜＞≥≤]|為滿值|全滿|滿值|未滿)?\s*"
    r"(?P<pct>\d+(?:\.\d+)?)?\s*[%％]?\s*(?P<tail>以上|以下)?")
_ZH_NAME_TO_EN = None


def _zh_status_en(rows, zh):
    """-> the English registry name for a Chinese status name (qualifiers off)."""
    global _ZH_NAME_TO_EN
    if _ZH_NAME_TO_EN is None:
        _ZH_NAME_TO_EN = {}
        for row in rows.values():
            z, e = row.get("_name"), row.get("_name_en")
            if z and e and row.get("_type") == 6:          # 6 = a status row
                _ZH_NAME_TO_EN.setdefault(sp.norm_name_zh(z), re.sub(r"\s*\([^)]*\)\s*$", "", e))
    return _ZH_NAME_TO_EN.get(sp.norm_name_zh(zh))


# \u82e5\u672c\u6b21\u653b\u64ca\u64ca\u5012\u6575\u4eba / \u82e5\u672c\u6b21\u653b\u64ca\u672a\u64ca\u5012\u6575\u4eba -- did this cast kill what it hit. Answerable
# at status-apply time: `core.execute` mutates HP through the whole swing loop before it
# runs the non-damage effects, so `not target.alive` is already final there.
_ZH_COND_KILL = re.compile(r"(?P<neg>\u672a|\u6c92\u6709|\u4e0d)?\u64ca(?:\u5012|\u6bba)")
# \u82e5\u672c\u6b21\u653b\u64ca\u66b4\u64ca / \u82e5\u653b\u64ca\u6642\u767c\u751f\u66b4\u64ca -- did any strike of this cast crit. NOT "(\u53ef\u66b4\u64ca)",
# which is a parenthetical saying a HEAL may crit, not a condition on anything.
_ZH_COND_CRIT = re.compile(r"(?P<neg>\u672a|\u6c92\u6709|\u4e0d)?(?:\u767c\u751f)?\u66b4\u64ca")
_ZH_CRIT_PAREN = re.compile(r"[(\uff08]\u53ef\u66b4\u64ca[)\uff09]")
# \u82e5\u81ea\u8eab\u300c\u76db\u6012\u300d\u9054\u52305\u5c64 / \u82e5\u81ea\u8eab\u81f3\u5c11\u67091\u5c64\u81f3\u9ad8\u69ae\u5149 / \u82e5\u653b\u64ca\u6642\u81ea\u8eab\u64c1\u67095\u5c64Reload.
# The stack COUNT, which `_ZH_COND_HOLDS` sees as `(?:\d+\u5c64)?` and throws away -- so
# "if you hold 5 stacks of Reload" shipped as "if you hold any Reload" (af911ee).
_ZH_COND_STACKS = re.compile(
    r"(?:\u9054\u5230|\u81f3\u5c11|\u64c1\u6709|\u6301\u6709|\u6709)?\s*(?P<n>\d+)\s*\u5c64\s*(?:\u4ee5\u4e0a)?"
    r"|\u300c?(?P<name>[^\u300d\uff0c\u3002]{1,14}?)\u300d?\s*\u9054\u5230\s*(?P<n2>\d+)\s*\u5c64")
# \u82e5\u653b\u64ca\u6642\u70ba\u5947\u6578\u56de\u5408 / \u5076\u6578\u56de\u5408 / \u7b2c3\u56de\u5408 / \u7e3d\u56de\u5408\u6578\u4e0d\u9ad8\u65bcN.
# \u82e5\u81ea\u8eab\u300c\u76db\u6012\u300d\u9054\u52305\u5c64 -- the status named first, the count after.
_ZH_STACK_NAME_FIRST = re.compile(
    r"\u300c?(?P<name>[^\u300d\uff0c\u3002\u82e5\u7576\u5247]{1,14}?)\u300d?\s*(?:\u9054\u5230|\u7d2f\u7a4d\u5230|\u5806\u758a\u5230|\u9054)\s*(?P<n>\d+)\s*\u5c64")
# \u82e5\u81ea\u8eab\u81f3\u5c11\u67091\u5c64\u81f3\u9ad8\u69ae\u5149 / \u82e5\u653b\u64ca\u6642\u81ea\u8eab\u64c1\u67095\u5c64Reload -- the count first, the status after.
_ZH_STACK_COUNT_FIRST = re.compile(
    r"(?:\u81f3\u5c11\s*(?:\u64c1\u6709|\u6301\u6709|\u6709)?|\u64c1\u6709|\u6301\u6709|\u6709)\s*(?P<n>\d+)\s*\u5c64\s*(?:\u4ee5\u4e0a)?\s*"
    r"\u300c?(?P<name>[^\u300d\uff0c\u3002\u5247\u6642]{1,14}?)\u300d?(?:\u72c0\u614b|\u6548\u679c)?(?:\u6642|\u7684|\u5247|\uff0c|$)")
_ZH_COND_ODD = re.compile(r"\u5947\u6578\s*\u56de\u5408")
_ZH_COND_EVEN = re.compile(r"\u5076\u6578\s*\u56de\u5408")
_ZH_COND_ROUND_N = re.compile(
    r"(?:\u7b2c|\u7e3d\u56de\u5408\u6578|\u56de\u5408\u6578)?\s*(?P<cmp>\u4e0d\u9ad8\u65bc|\u4e0d\u4f4e\u65bc|\u9ad8\u65bc|\u4f4e\u65bc)?\s*(?P<n>\d+)\s*\u56de\u5408"
    r"(?P<tail>\u4ee5\u5167|\u4ee5\u4e0a|\u4ee5\u4e0b)?")


# 若我方存活人數在3人以下 / 若敵方存活人數在2人以上(包含2人). The parenthetical always
# restates the bound inclusively, so 以上 is >= and 以下 is <=.
_ZH_COND_ALIVE = re.compile(
    r"(?P<side>我方|敵方)存活人數在?\s*(?P<n>\d+)\s*人?(?P<cmp>以上|以下)")


def _zh_parse_condition(rows, frag):
    """-> a `requires` dict for one condition fragment, or None.

    Ordered most-specific first. \u5c64 is tested before the bare hold because
    \u82e5\u81ea\u8eab\u64c1\u67095\u5c64Reload satisfies both and the count is the whole point of it.
    """
    # -- this cast's own outcome: killed / crit. Both read `ctx` in the engine rather
    # than unit state, so they are only evaluatable during the cast that raised them.
    if _ZH_COND_KILL.search(frag):
        m = _ZH_COND_KILL.search(frag)
        return {"killed": not m.group("neg")}
    if _ZH_COND_CRIT.search(frag) and not _ZH_CRIT_PAREN.search(frag):
        m = _ZH_COND_CRIT.search(frag)
        return {"crit": not m.group("neg")}

    # -- the round number. \u5947\u6578/\u5076\u6578 first: "\u7b2c1\u56de\u5408" is a different shape from
    # "\u5947\u6578\u56de\u5408" and only the latter is parity.
    # -- how many units are still standing. Michael's Faith In Chaos grants himself
    # 全傷害激減 -- incoming damage reduced to 1 -- gated on 若敵方存活人數在2人以上, and a
    # guild boss fight has exactly ONE enemy. Ungated he simply cannot be killed, which
    # is what a player reported: "Michael never dies and just keeps reviving everyone".
    m = _ZH_COND_ALIVE.search(frag)
    if m:
        return {"alive": {"side": "ally" if m.group("side") == "我方" else "enemy",
                          "cmp": "gte" if m.group("cmp") == "以上" else "lte",
                          "n": int(m.group("n"))}}

    if _ZH_COND_ODD.search(frag):
        return {"round": {"parity": 1}}
    if _ZH_COND_EVEN.search(frag):
        return {"round": {"parity": 0}}
    m = _ZH_COND_ROUND_N.search(frag)
    if m and re.search(r"\u56de\u5408\u6578|\u7b2c\s*\d+\s*\u56de\u5408|\u56de\u5408(?:\u4ee5\u5167|\u4ee5\u4e0a|\u4ee5\u4e0b)", frag):
        cmp_, tail, n = m.group("cmp") or "", m.group("tail") or "", int(m.group("n"))
        op = {"\u4e0d\u9ad8\u65bc": "<=", "\u4e0d\u4f4e\u65bc": ">=", "\u9ad8\u65bc": ">", "\u4f4e\u65bc": "<"}.get(cmp_)
        if op is None:
            op = {"\u4ee5\u5167": "<=", "\u4ee5\u4e0b": "<=", "\u4ee5\u4e0a": ">="}.get(tail, "==")
        return {"round": {"cmp": op, "n": n}}

    # -- "N stacks of X", in the three orders the corpus writes it. Tried BEFORE the
    # bare hold because \u9054\u5230 and \u81f3\u5c11\u6709 are not hold verbs, so those two forms used to
    # fall through to None and the effect fired unconditionally.
    for sm in (_ZH_STACK_NAME_FIRST.search(frag), _ZH_STACK_COUNT_FIRST.search(frag)):
        if not sm:
            continue
        # The capture can swallow the side word in front of the name -- \u82e5\u81ea\u8eab\u300c\u76db\u6012\u300d\u9054\u52305\u5c64
        # has no separator between \u81ea\u8eab and the name, so the regex starts at \u81ea. Strip it
        # here (and keep it, because it is also the answer to WHOSE stacks these are).
        raw = sm.group("name")
        lead = re.match(r"^(?:\u82e5|\u7576|\u5982\u679c|\u5728|\u653b\u64ca\u6642|\u81ea\u8eab|\u81ea\u5df1|\u6211\u65b9|\u76ee\u6a19|\u5c0d\u8c61|\u6575\u65b9|\u5c0d\u65b9|\u6575\u4eba|\u64c1\u6709|\u6301\u6709)+", raw)
        name = raw[lead.end():] if lead else raw
        name = name.strip("\u300c\u300d\u300e\u300f\"' ")
        if not name:
            continue
        en = _zh_status_en(rows, name)
        before = frag[:sm.start()] + (lead.group(0) if lead else "")
        who = "caster" if re.search(r"\u81ea\u8eab|\u81ea\u5df1|\u6211\u65b9", before) and not re.search(
            r"\u76ee\u6a19|\u6575\u65b9|\u5c0d\u65b9|\u6575\u4eba", before) else "target"
        return {"status": en or name, "on": who, "negate": False,
                "resolved": bool(en), "count": int(sm.group("n"))}

    m = _ZH_COND_HOLDS.search(frag)
    if m:
        # The holder is whichever side word precedes 擁有 in the fragment -- "若攻擊時
        # 自身擁有5層Reload" names 自身 mid-fragment, not at its start.
        before = frag[:m.end("name")]
        who = "caster" if re.search(r"自身|自己|我方", before) and not re.search(
            r"目標|敵方|對方|敵人", before) else "target"
        en = _zh_status_en(rows, m.group("name"))
        out = {"status": en or m.group("name"), "on": who,
               "negate": bool(m.group("neg")), "resolved": bool(en)}
        # "\u81f3\u5c11\u67091\u5c64" is a MINIMUM, and every stack phrasing in the corpus is one --
        # \u9054\u52305\u5c64, \u64c1\u67095\u5c64, 3\u5c64\u4ee5\u4e0a. Shipping this as a bare hold (af911ee) made
        # "if you have 5 stacks of Reload" true on the first stack.
        sm = _ZH_COND_STACKS.search(frag)
        if sm and not out["negate"]:
            out["count"] = int(sm.group("n") or sm.group("n2"))
        return out
    m = _ZH_COND_HP.search(frag)
    if m and (m.group("cmp") or m.group("tail")):
        who = m.group("who") or ""
        cmp_, pct, tail = m.group("cmp") or "", m.group("pct"), m.group("tail") or ""
        if cmp_ in ("為滿值", "全滿", "滿值"):
            op, val = ">=", 100.0
        elif cmp_ == "未滿":
            op, val = "<", float(pct) if pct else 100.0
        elif pct is None:
            return None
        elif cmp_ in ("高於", "大於", ">", "＞") or tail == "以上":
            op, val = (">=" if tail == "以上" else ">"), float(pct)
        elif cmp_ in ("低於", "小於", "<", "＜") or tail == "以下":
            op, val = ("<=" if tail == "以下" else "<"), float(pct)
        elif cmp_ == "不高於" or cmp_ == "≤":
            op, val = "<=", float(pct)
        elif cmp_ == "不低於" or cmp_ == "≥":
            op, val = ">=", float(pct)
        else:
            return None
        return {"hp": {"on": "caster" if who in ("自身", "自己", "我方") else "target",
                       "cmp": op, "pct": val}}
    return None


_PURSUIT_WORD = re.compile(r"追擊|追加攻擊")
# The figure in the parenthetical that follows 追擊 -- 追擊(造成120%攻擊力傷害). This is
# the unambiguous form, and it is checked first because the sentence often ALSO carries
# the parent skill's own coefficient (200%攻擊力的傷害，並…追擊(造成120%攻擊力傷害)).
_PURSUIT_COEF_PAREN = re.compile(
    r"(?:追擊|追加攻擊)[^。]{0,24}?[（(][^）)]*?(\d+)\s*%\s*攻擊力")
_ATK_PCT = re.compile(r"(\d+)\s*%\s*攻擊力")
_CHANCE_PCT = re.compile(r"(\d+)\s*%\s*(?:固定)?機率")


def zh_pursuit_numbers(r):
    """-> {"coefficient": x, "chance_pct": y} for a 追擊, from the Chinese. Either may
    be absent.

    A pursuit sub-skill's own design row carries NO numbers -- 100000341 is `_note1_jp`
    "脊砕き 追加技能" and five zeroed columns -- so the engine had no coefficient and
    `formula.strike` returned None, i.e. every pursuit fired and dealt nothing (1,190 of
    1,282 corpus-wide). The figures are stated by the PARENT instead, which is what this
    reads.

    Deliberately conservative: an AMBIGUOUS fragment yields nothing rather than a guess.
    Several casts pursue twice with different odds ("分別以60%、30%機率…最多兩次追擊"),
    and picking one of two numbers for both would be worse than leaving the odds alone.
    """
    note = r.get("_note1") or ""
    if not note:
        return {}
    got = {}
    m = _PURSUIT_COEF_PAREN.search(note)
    if m:
        got["coefficient"] = int(m.group(1)) / 100.0
    for sentence in re.split(r"[。\n]", note):
        if not _PURSUIT_WORD.search(sentence):
            continue
        for frag in _split_fragments(sentence):
            if not _PURSUIT_WORD.search(frag):
                continue
            if "coefficient" not in got:
                hits = _ATK_PCT.findall(frag)
                if len(hits) == 1:
                    got["coefficient"] = int(hits[0]) / 100.0
            # 必然追擊 -- "certainly pursues". An explicit 100%, and it must not be
            # confused with a missing number, which also fires every time but for the
            # wrong reason.
            if "必然" in frag:
                got.pop("chance_pct", None)
                return got
            # 分別 ("respectively") distributes several odds across several pursuits:
            # "分別以60%、30%機率…最多兩次追擊". The fragment splitter breaks that list
            # apart, so the 追擊 fragment is left holding ONE of the numbers and looks
            # unambiguous when it is not -- it gave both pursuits 30%. Refuse the whole
            # sentence instead; a pursuit that fires too often is a smaller error than
            # one whose odds are confidently wrong.
            if "分別" in sentence:
                return got
            odds = _CHANCE_PCT.findall(frag)
            if len(odds) == 1:
                got["chance_pct"] = float(odds[0])
            return got
    return got


def zh_condition_for(rows, r, zh_name, follow_up=False):
    """-> the condition gating the grant of `zh_name` in this skill's Chinese, or None.

    The condition is the 若/當 fragment that precedes the granting fragment within the
    same sentence (a full stop ends a condition's reach). For a follow-up the "grant" is
    the 追擊 fragment instead.
    """
    note = r.get("_note1") or ""
    if follow_up:
        want = re.compile(r"追擊|追加攻擊|再次使用|進行追加")
    for sentence in re.split(r"[。\n]", note):
        parts = _split_fragments(sentence)
        for i, frag in enumerate(parts):
            hit = want.search(frag) if follow_up else (
                zh_name and sp.norm_name_zh(zh_name) in sp.norm_name_zh(frag)
                and _GRANT_VERB.search(frag))
            if not hit:
                continue
            for j in range(i, max(-1, i - 3), -1):
                if _ZH_COND_START.search(parts[j].strip()) or (j == i and "若" in parts[j]):
                    got = _zh_parse_condition(rows, parts[j])
                    if got:
                        return got
            break
    return None



def status_target(r, status_name, zh_name=None):
    """-> who a status is applied to: "caster", "allies", "targets", or None.

    **The recipient is not the skill's target.** This has now caused four live bugs in
    a row -- a move gauge handed to the enemy it was cast at, a heal that restored the
    raid boss, Metatron's "Cast Serum Injection on up to 1 allies with the highest ATK"
    buffing the boss instead, and 127 buffs (plus 182 shields) granted to whoever the
    caster had just hit. An attack skill routinely aims at an enemy and applies
    something to its own side.

    Two things this used to get wrong, both of which `passive_who` in this same file has
    always got right -- it reads the ORIGINAL language and it narrows to the fragment
    naming the status. This function did neither, and the two failures compound:

    READ THE CHINESE (section 3). `_note1` is the original and `_note1_en` a
    translation, so the original wins outright on a disagreement. Recorded rather than
    silently preferred, so the count of disagreements stays visible.

    NARROW TO THE FRAGMENT. `_clause_for` splits on `[.!?]`, which is English
    punctuation -- a Chinese line has none of it, so the whole line comes back as one
    "sentence" and any side word anywhere in it wins. Shark Shark Attack IV is the case
    that shows the cost:

        行動開始前對我方全體附加超級防曬乳，…，若攻擊時自身擁有5層Reload，…
                    ^^^^ recipient: all allies      ^^^^ a CONDITION, 44 chars later

    Reading the whole line matched 自身 from the condition and answered "caster" -- a
    wrong answer, which is worse than the None it used to return from the English. The
    `_fragment_for` narrowing that passives already use picks the right half.

    `_who_in` is used rather than testing the three regexes here, because it also strips
    the exclusion parenthetical ("all allies EXCLUDING the caster") and the possessive
    stat source ("the caster's Max HP" names whose ATK the magnitude reads, not who
    receives it) -- neither of which this function used to account for.

    `None` still means the prose does not say, and the engine falls back to the skill's
    targets -- right for the ordinary "inflicts X on the target" case.
    """
    zh = _who_for(r.get("_note1"), zh_name)
    en = _who_for(r.get("_note1_en"), status_name)
    if zh and en and zh != en:
        _WHO_DISAGREEMENTS.append((r.get("_id"), status_name, zh, en))
    who = zh or en
    # Enemy means "fall through to the skill's own targets": the ordinary case is an
    # attack that inflicts something on what it hit, and naming the enemy explicitly
    # does not change that.
    return {"self": "caster", "ally": "allies"}.get(who)


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


# A Chinese clause that states a CONDITION. 若 and 如果 are unambiguous; 當 only counts
# when a state test follows it. That last restriction is load-bearing: 當回合開始時 is a
# TIMING, not a condition, and the compiler already learned once (see the `conditional =
# False` note in annotate_passive) that conflating the two makes every derived rule roll
# at CONDITIONAL_CHANCE -- a boss's permanent trait becomes a coin flip.
_ZH_CONDITIONAL = re.compile(
    r"若|如果|當[^，。]{0,12}(以上|以下|低於|高於|超過|大於|小於|擁有|處於|存活)")


def _zh_is_conditional(note, zh_name):
    """-> whether the fragment granting `zh_name` sits under a condition, or None.

    OWN FRAGMENT PLUS THE ONE BEFORE IT, which is the whole difficulty. 若目標擁有暈眩，
    則附加惡化 splits on the comma, so the fragment naming the status states no condition
    and the one before it states nothing else. Reading only the own fragment answers
    "not conditional" for the commonest shape in the corpus; reading the whole line
    answers "conditional" for every status in any note containing a 若 anywhere, which is
    the `_clause_for` trap `status_target` documents. Narrowing to own+prev is the same
    rule `zh_heals` and `_zh_side_of` settled on.
    """
    if not zh_name or zh_name not in (note or ""):
        return None
    parts = [p for p in _ZH_SPLIT.split(note) if p.strip()]
    for i, part in enumerate(parts):
        if zh_name in part:
            prev = parts[i - 1] if i else ""
            return bool(_ZH_CONDITIONAL.search(part)
                        or _ZH_CONDITIONAL.search(prev))
    return None


def is_conditional(r, status_name, zh_name=None):
    """Is this status applied only under a condition the opcodes do not encode?

    **The Chinese decides** (section 3). This read `_note1_en` alone, and both failure
    modes are live at scale across 17,746 application sites:

      * 6,915 sites (39%) have NO English clause at all -- the untranslated mob and boss
        rows -- so conditionality was `null` and nothing was ever gated on them.
      * of the 10,500 where both languages answer, they disagree on 23%. 2,258 are
        conditional in English and unconditional in the original, and each of those
        takes CONDITIONAL_POLICY at runtime: a status the prose says ALWAYS lands was
        firing on a 50% roll.

    English is kept as the fallback for the rows the Chinese cannot answer, and every
    disagreement is recorded rather than quietly resolved.
    """
    zh = _zh_is_conditional(r.get("_note1"), zh_name)
    clause = _clause_for(r.get("_note1_en") or "", status_name)
    en = None if clause is None else bool(_CONDITIONAL.search(clause))
    if zh is not None and en is not None and zh != en:
        _COND_DISAGREEMENTS.append((r.get("_id"), status_name, zh, en))
    return zh if zh is not None else en


# A percentage that belongs to a HEAL is not the skill's damage coefficient, however
# much it looks like one: 以40%攻擊力回復自身體力 states 攻擊力 and a number, which is
# exactly the shape `_COEF_ZH` hunts for. Blanking the heal fragments first is what keeps
# the two apart -- without it the guard below rejected the heal as "just the damage
# coefficient restated" and `damage()` read a passive with no attack as a 40% hit.
_HEAL_FRAGMENT = re.compile(r"[^，。\n]*(?:回復|恢復|補血)[^，。\n]*")
# ...and the ENGLISH note needs the same treatment, for the same reason and on its own
# punctuation. `_COEF` matches `N% HP` as readily as `N% ATK`, so "restores 18% HP" read
# as an 18% damage coefficient and the guard then threw the heal away as a restatement
# of it. Blanking only the Chinese fragments left every mob heal written this way --
# Healing Breath, HP Regen and 66 more -- compiling to nothing.
_HEAL_FRAGMENT_EN = re.compile(r"[^.,;\n]*\b(?:restor\w*|recover\w*|heal\w*)\b[^.,;\n]*",
                               re.I)


def _without_heal_clauses(note):
    return _HEAL_FRAGMENT_EN.sub(" ", _HEAL_FRAGMENT.sub(" ", note or ""))


def _is_damage_coefficient(r, pct):
    """Is `pct` just the skill's own damage coefficient restated?"""
    m = _COEF.search(_without_heal_clauses(r.get("_note1_en") or ""))
    if m and abs(float(m.group(1)) - pct) < 1e-6:
        return True
    m = _COEF_ZH.search(_without_heal_clauses(r.get("_note1") or ""))
    if m and abs(float(m.group(2) or m.group(3)) - pct) < 1e-6:
        return True
    return False


# --- additional damage sized on the TARGET's max HP ----------------------------------
#
# 150 attack skills state one and NONE of them compiled it. `damage()` below reads the
# FIRST coefficient it finds and stops, so `100%攻擊力的2段傷害，附加敵方35%最大體力的
# 傷害` produced the 100% ATK and silently dropped the rest -- and `unmodelled` stayed
# null, so nothing flagged it either. That clause is the Guild Weekly boss's entire
# damage output: Special Sanction is 35% of a ~35,000 HP unit, ten times what its ATK
# hits do, which is why a level-150 boss could not dent a level-100 party.
#
# Emitted as an attack_rider, not a second damage effect, because `core.execute` runs
# every damage effect once per SWING -- Special Sanction has two, so a damage-op form
# would pay 35% twice.
#
# The side is always the TARGET: 142 rows say 敵方 and 9 say 目標, none say otherwise.
# Status ROWS describing self-inflicted max-HP damage (「受到傷害後自身獲得15%最大體力
# 的傷害」) are a different mechanic and are excluded by requiring one of those two words.
_ZH_EXTRA_MAXHP = re.compile(
    r"(?:額外|附加)[^。\n]{0,14}?"
    r"(?:敵方|目標)\s*"
    r"(?:(\d+(?:\.\d+)?)\s*[%％]\s*(?:的)?\s*最大體力"
    r"|最大體力\s*(\d+(?:\.\d+)?)\s*[%％])"
    r"[^。\n]{0,8}?傷害")

# 239 of the 150 skills' rows carry the same parenthetical exemption, and it is a real
# gate rather than flavour: 對擁有「精英」狀態的敵人不會發動 -- "does not trigger against
# enemies with the Elite status". `Elite` is status 140 and the engine matches by name.
_ZH_ELITE_EXEMPT = re.compile(r"對擁有\s*[「『]?\s*精英\s*[」』]?\s*狀態的敵人不會發動")


def extra_maxhp_damage(r):
    """-> an attack_rider for an "additionally deal N% of the target's Max HP" clause.

    The prose says this one is mitigated -- 此傷害會計算防禦與屬性, "calculates defence
    and attribute" -- which is why `_rider` routes the `target_max_hp` basis through
    `formula.strike` instead of paying it flat like the caster_*_hp riders.
    """
    note = r.get("_note1") or ""
    m = _ZH_EXTRA_MAXHP.search(note)
    if not m:
        return None
    pct = m.group(1) or m.group(2)
    out = {"op": "attack_rider", "kind": "bonus_damage", "basis": "target_max_hp",
           "percent": round(float(pct), 4), "source": "prose_zh"}
    if _ZH_ELITE_EXEMPT.search(note):
        out["requires"] = {"status": "Elite", "on": "target", "negate": True,
                           "resolved": True}
    return out


_ZH_REVIVE_PCT = re.compile(r"以\s*(\d+(?:\.\d+)?)\s*[%％]\s*(?:的)?體力[^，。]{0,6}復活"
                            r"|復活[^，。]{0,20}?(\d+(?:\.\d+)?)\s*[%％]\s*(?:的)?體力")
# The count is its own search: folding it into the percent pattern with `(\d+)?` let the
# lazy middle match nothing and report no count at all on 復活我方被擊倒的隨機2人.
_ZH_REVIVE_N = re.compile(r"復活[^，。]{0,20}?(\d+)\s*[人名]")


def zh_revives(r):
    """-> every revive the Chinese states: {percent, count, target}.

    `復活我方被擊倒的隨機2人` with `以50%體力` in front of it. The percent is the HP they
    come back on and the count is how many, both of which the opcode path only gets when
    there IS a revive opcode -- and a revive stated inside a follow-up clause has none.
    """
    out = []
    for i, c, prev in _zh_clauses_with(r.get("_note1"), re.compile(r"復活")):
        if re.search(r"禁止復活|不會復活|無法復活|復活道具", c):
            continue                              # a ban, a caveat, or the retry UI
        if re.search(r"復活(?:後|時)", c) and not re.search(r"復活我方|復活.*[人名體]", c):
            # 復活後清除此狀態 -- "AFTER reviving, clear this status". The clause REFERS
            # to the revive the sentence before it grants; it does not grant a second
            # one. Invisible until the clause ledger replaced value-keyed dedupe, which
            # had been collapsing the reference into the real revive by luck: Soul
            # Resurrection states one revive and compiled to two.
            continue
        m = _ZH_REVIVE_PCT.search(c) or _ZH_REVIVE_PCT.search(prev or "")
        pct = (m.group(1) or m.group(2)) if m else None
        if pct is None:
            pm = re.search(r"(\d+(?:\.\d+)?)\s*[%％]", c)
            pct = pm.group(1) if pm else None
        if pct is None:
            continue
        # ALWAYS allies. A revive raises the caster's own fallen, which is what the
        # pack writes (復活我方被擊倒的...) and what `core.execute` says in as many words:
        # "A revive raises the CASTER's fallen allies, not the units it is aimed at."
        # Reading the side off the clause let a neighbouring 敵方 turn 14 of them into
        # revives aimed at the enemy team, which resolve to an empty pool and do nothing.
        entry = {"percent": float(pct), "target": "allies"}
        n = _ZH_REVIVE_N.search(c)
        if n:
            entry["count"] = int(n.group(1))
        gate = _zh_governing_condition(dd.rows("skill") or {}, r.get("_note1"), i)
        if gate:
            entry["requires"] = gate
        out.append((i, entry))
    return out


# 攻擊後吸收N%傷害 -- life steal, on 28 mob basic attacks. `吸收` is the pack's own word
# for it: the sibling family 攻擊吸收 is translated "Life Steal" and writes the mechanic
# out in full (擊傷時最多1次，以25%機率恢復6%體力). Everywhere else 吸收 means a SHIELD
# and says so -- 吸收7000點傷害的護盾, 232 of them carry 護盾 or 點 -- so the percent form
# with neither is the drain, not a barrier.
_ZH_LIFESTEAL = re.compile(r"吸收\s*(\d+(?:\.\d+)?)\s*[%％]\s*(?:的)?傷害")


def lifesteal_rider(r):
    """-> an attack_rider healing a share of the damage dealt, or None."""
    note = r.get("_note1") or ""
    if "護盾" in note:
        return None                              # a shield sized in percent, not a drain
    m = _ZH_LIFESTEAL.search(note)
    if not m:
        return None
    return {"op": "attack_rider", "kind": "heal", "basis": "damage_dealt",
            "percent": round(float(m.group(1)), 4), "source": "prose_zh"}


def uncovered_revives(r, claimed):
    """-> revive effects for the revive clauses no opcode claimed."""
    out = []
    for i, v in zh_revives(r):
        if ("revive", i) in claimed:
            continue
        LEDGER["revive.unclaimed"] += 1
        out.append({"op": "revive", "percent": v["percent"],
                    "target": v.get("target"), "source": "prose_zh",
                    **({"requires": v["requires"]} if v.get("requires") else {}),
                    **({"count": v["count"]} if v.get("count") else {})})
    return out


# A cleanse stated in prose with no remove_status opcode behind it. The CATEGORY forms
# only -- 清除自身可堆疊類型的能力下降狀態 -- because `status.remove_category` removes by
# category and that is exactly what these name.
#
# The NAMED forms are deliberately left alone: 清除我方全體混亂、幻惑、凍結狀態 lists
# three specific statuses, and the engine's removal takes a category, so compiling it
# would clear every `misc` status the ally holds rather than those three. Over-removing
# is worse than not removing, and it would be invisible.
_ZH_CLEANSE_CAT = [
    (re.compile(r"能力(?:下降|低下)"), "debuff"),
    (re.compile(r"能力(?:上升|上昇|提升)"), "buff"),
    (re.compile(r"持續傷害"), "damage_over_time"),
    (re.compile(r"護盾"), "shield"),
]
_ZH_CLEANSE_VERB = re.compile(r"(?<!可)(?<!無法)(?<!被)(清除|解除|消除)(?!不可)")


def uncovered_removes(r, effects_so_far):
    """-> remove_status effects for prose cleanses no opcode covered.

    NOT on the clause ledger, unlike the three families below it. The opcode walker
    emits `remove_status` from OP_REMOVE, which carries a status id or a category and
    never a clause index, so there is nothing for a clause to be claimed BY -- matching
    the two sides needs a category comparison, not a queue.

    Until that lands this stays all-or-nothing, and that is a known gap, not a design:
    726 of the 2,177 skills with an opcode remove also state a cleanse in prose that
    this bail drops. 153002001 clears the target's 持續回復 by opcode and states a
    second cleanse -- 行動後清除敵方攻擊力最高2名的能力上升狀態 -- that never compiles.
    """
    if any(e.get("op") == "remove_status" for e in effects_so_far):
        return []                                 # the walker already found one
    out, seen = [], set()
    for _i, c, _prev in _zh_clauses_with(r.get("_note1"), _ZH_CLEANSE_VERB):
        # 若...擁有可清除的... is a CONDITION on the clause, not the cleanse itself.
        if re.search(r"若[^，。]*可(?:清除|解除)", c):
            continue
        for pat, cat in _ZH_CLEANSE_CAT:
            if not pat.search(c):
                continue
            if cat in seen:
                break
            seen.add(cat)
            entry = {"op": "remove_status", "category": cat, "source": "prose_zh"}
            cm = _ZH_CHANCE.search(c)
            if cm:
                entry["chance_pct"] = float(cm.group(1))
            out.append(entry)
            break
    return out


def uncovered_gauges(r, claimed):
    """-> gauge effects the Chinese states that no opcode emitted.

    Same seam as `uncovered_heals`: `modify_gauge` reaches `effects` through the opcode
    walker, so a clause with no gauge opcode behind it -- 我方全體行動值+15% on a passive,
    or the 使其行動值-25% riding a follow-up -- produced nothing.
    """
    out = []
    for i, g in zh_gauges(r):
        if ("gauge", i) in claimed:
            continue
        LEDGER["gauge.unclaimed"] += 1
        out.append({"op": "modify_gauge", "percent": g["percent"],
                    "target": g.get("target"), "source": "prose_zh",
                    **({"requires": g["requires"]} if g.get("requires") else {}),
                    **({"chance_pct": g["chance_pct"]} if g.get("chance_pct") else {})})
    return out


def _heal_key(e):
    """The identity of a heal for dedupe: what it pays, off what, to whom.

    `basis` is normalised because the two readers spell the same thing differently --
    the op-1 rider leaves it unset and defaults to ATK in the engine, while the heal
    opcode writes "atk" -- and `caster_max_hp` and `max_hp` are the same pool when the
    recipient IS the caster.
    """
    basis = (e.get("basis") or "atk").replace("caster_", "")
    return (e.get("percent"), basis, e.get("target"))


def drop_clauseless_repeats(effects):
    """Drop a heal an opcode derived from the whole note when another heal says it.

    The narrow survivor of value-keyed dedupe, and the ledger says exactly when to use
    it: an opcode flagged `_no_clause` had no clause of its own, so its magnitude came
    from re-reading the note and duplicating a heal that IS clause-backed. Angel Healing
    VI states one 50% party heal and has two heal opcodes; without this it pays twice,
    which is the Gate of Judgement bug that was verified on a device.

    A `_no_clause` heal with nothing to duplicate is KEPT -- a skill with a heal opcode
    and an empty note still heals, and `damage` makes the same call for the same reason.
    """
    keys = {_heal_key(e) for e in effects
            if e.get("op") == "heal" and not e.get("_no_clause")}
    out = []
    for e in effects:
        if e.pop("_no_clause", False):
            key = _heal_key(e)
            if key in keys:
                LEDGER["heal.clauseless_repeat"] += 1
                continue
            keys.add(key)
        out.append(e)
    return out


def drop_rider_duplicates(effects):
    """Fold heals that a RIDER already pays. -> the list.

    The whole residue of value-keyed dedupe, in one place, and the ledger says exactly
    why it has to stay: `zh_rider`/`zh_hp_rider` search the WHOLE note rather than a
    clause, so they have no index to claim with and the ledger cannot see them. Two
    shapes, both of them shipped bugs:

      * two rider opcodes read one sentence -- Spine Break V states 以75%攻擊力回復自身
        體力 once and both its rider slots emitted it;
      * a rider and a heal opcode read one sentence -- Michael's Gate of Judgement
        states 以200%攻擊力恢復我方全體體力 once, and he healed himself 20,000 where the
        prose says 10,000. That fix was verified on a device; `server/test_engine.py`
        holds the line.

    The rider wins, because it is the one carrying the animation. This goes away when
    those two readers move onto `_zh_clauses_with` and can claim like everything else;
    the same double-read affects `bonus_damage` riders and is deliberately NOT touched
    here -- widening it changes a family this work has no business changing.
    """
    seen, out = set(), []
    for e in effects:
        if e.get("op") == "attack_rider" and e.get("kind") == "heal":
            key = _heal_key(e)
            if key in seen:
                LEDGER["heal.rider_repeat"] += 1
                continue
            seen.add(key)
        out.append(e)
    riders = {_heal_key(e) for e in out
              if e.get("op") == "attack_rider" and e.get("kind") == "heal"}
    kept = []
    for e in out:
        if e.get("op") == "heal" and _heal_key(e) in riders:
            LEDGER["heal.rider_covered"] += 1
            continue
        kept.append(e)
    return kept


def uncovered_heals(r, claimed):
    """-> heal effects for the heal clauses nothing claimed.

    Heals reach `effects` through the OPCODE walker, so a clause with no heal opcode
    behind it -- which is most passives, and any active skill whose heal rides its
    attack -- produced nothing. `tools/clause_coverage.py` ranked the damage: roughly
    190 fragments across six shapes, all heals, the largest single family in the corpus.

    Emits by clause alone: a heal a rider already pays is folded afterwards by
    `drop_rider_duplicates`, which is where the last of the value-keyed dedupe lives.
    """
    out = []
    for i, h in zh_heals(r):
        if ("heal", i) in claimed:
            continue
        LEDGER["heal.unclaimed"] += 1
        entry = {"op": "heal", "percent": h["percent"], "basis": h["basis"],
                 "target": h["target"], "source": "prose_zh"}
        if h.get("requires"):
            entry["requires"] = h["requires"]
        if h.get("count"):
            entry["count"] = h["count"]
        out.append(entry)
    return out


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
        m = _COEF_ZH.search(_without_heal_clauses(r.get("_note1") or ""))
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


# --- passives: WHEN a clause fires ---------------------------------------------------
#
# A passive is `at TRIGGER, if CONDITION, apply STATUS to SELECTION`. Everything but the
# trigger was already compiled per effect -- the status, the recipient, the condition and
# the numbers all come from the sentence naming the status. The trigger is stated in that
# same sentence and was simply never read, which is why `engine/passives.py` had to
# hand-write the whole tuple for six casts and every other passive in the game did
# nothing at all.
#
# The patterns are anchored on the EVENT WORD rather than the whole phrase: the pack says
# the same thing a dozen ways ("when a battle starts", "at the start of the battle",
# "when the battle begins", "before the battle starts" are one trigger and four
# spellings), and matching the event survives that.
PASSIVE_TRIGGERS = [
    # Death first: "when knocked out" also contains "when", and nothing else keys on it.
    ("on_death", re.compile(
        r"\b(?:when|after|upon|if)\b[^,.;]{0,40}?"
        r"\b(?:defeated|knocked\s*out|dies|died|death)\b", re.I)),
    # Damage TAKEN before DEALT: "when taking damage from attacks" and "while dealing
    # damage" share every word but the verb.
    ("on_damage_taken", re.compile(
        r"\b(?:when|while|after|every\s+time|each\s+time|upon|if)\b[^,.;]{0,40}?"
        r"(?:tak(?:e|es|ing)\s+(?:damage|attacks?|enemy)|damaged|受到傷害|被攻擊)", re.I)),
    ("on_damage_dealt", re.compile(
        r"\b(?:when|while|after|every\s+time|each\s+time|upon|if)\b[^,.;]{0,40}?"
        r"(?:deal(?:s|ing)?\s+(?:any\s+)?(?:damage|attack)|land(?:s|ing)\s+a\s+critical"
        r"|造成傷害)", re.I)),
    ("battle_start", re.compile(
        r"\b(?:when|at|before|on|upon|in)\b[^,.;]{0,30}?\bbattles?\b[^,.;]{0,20}?"
        r"(?:start|begin)|(?:start|beginning)\s+of\s+(?:the\s+|a\s+|each\s+)?battle"
        r"|戰鬥開始", re.I)),
    ("after_action", re.compile(
        r"\bafter\b[^,.;]{0,20}?\b(?:the\s+)?(?:action|attack(?:ing)?|turn|acting)\b"
        r"|行動後", re.I)),
    # ...and everything else keyed on the holder's own turn, which the pack writes as
    # both "when a turn starts" and "before the action".
    ("turn_start", re.compile(
        r"\b(?:when|at|before|on|upon)\b[^,.;]{0,30}?"
        r"\b(?:turns?|actions?|attacking|acting)\b[^,.;]{0,20}?(?:start|begin)?"
        r"|before\s+(?:the\s+)?(?:action|turn|attacking)|行動前", re.I)),
]

# "When affected by this effect, SPD-100" describes what a STATUS does to whoever holds
# it. That is the status registry's business; turning it into a rule would make the
# holder apply its own debuff to itself every turn.
_GLOSSARY_CLAUSE = re.compile(
    r"\bwhen\s+affected\s+by\s+(?:this|the)\s+(?:effect|status)", re.I)

# Any verb that describes an effect happening -- deliberately WIDER than `_GRANTS`. It is
# used to decide whether a clause is still unclaimed, and an unclaimed clause SUPPRESSES
# the implicit-trait default below. Over-matching here therefore costs coverage, never
# correctness, which is the direction to err in.
_DESCRIBES_EFFECT = re.compile(
    r"\b(grants?|inflicts?|applies|apply|gives?|gains?|cast|increases?|reduces?|raises?"
    r"|lowers?|boosts?|restores?|heals?|deals?|removes?|opens?|absorbs?|immune|immunity)\b"
    # A stat change is often written with no verb at all -- Jacqueline's whole passive is
    # "Before the action, if HP>90%, ATK+25%". Without this the clause read as describing
    # nothing, so it never counted as unclaimed, so the two statuses it governs fell
    # through to the implicit-trait default and became permanent battle-start buffs with
    # the HP gate dropped.
    r"|\b(?:ATK|DEF|SPD|HP|CRI|CRIT)\s*[+\-]\s*\d"
    r"|[+\-]\s*\d+(?:\.\d+)?\s*%", re.I)

# "there is a 40% fixed chance to inflict Stun" -- when the pack states a probability it
# is always written this way, and it beats op 113's neutral 0.75 stand-in.
_STATED_CHANCE = re.compile(
    r"(\d+(?:\.\d+)?)\s*%\s*(?:fixed\s+)?(?:chance|probability)"
    r"|(?:chance|probability)\s*(?:of|is)?\s*(\d+(?:\.\d+)?)\s*%", re.I)

# A permanent effect with no stated timing happens once, at battle start. "All allies
# SPD+5% permanently" has no trigger word in it at all, and leaving it untriggered means
# it never happens -- which is worse than the one inference being wrong.
_ALWAYS_ON = re.compile(
    r"\bpermanent(?:ly)?\b|\bentire\s+battle\b|\bwhole\s+battle\b|\bfull\s+battle\b"
    r"|\balways\b|\bconstantly\b|\bfor the rest of\b|整場戰鬥|永久", re.I)

_HIGHEST = re.compile(
    r"(?:the\s+)?(\d+|one|two|three)?\s*"
    r"(ally|allies|allied|enemy|enemies|enemy\s+targets?)\b[^,.;]{0,40}?"
    r"\bwith\s+the\s+(?:highest|greatest)\s+(ATK|DEF|SPD|HP)", re.I)
_ON_ATTACKER = re.compile(r"\b(?:on|to)\s+the\s+attacker\b", re.I)


def passive_trigger(clause):
    """-> the trigger a passive clause states, or None when it states no timing."""
    if not clause or _GLOSSARY_CLAUSE.search(clause):
        return None
    for name, rx in PASSIVE_TRIGGERS:
        if rx.search(clause):
            return name
    return None


# Where one clause stops talking about one status and starts on the next. "grant the
# caster CC Immunity for two turns AND inflict Headwind on all enemies" is two effects
# with two different recipients in one sentence, and reading the recipient off the whole
# sentence gives both of them the first one's answer.
# Fragment boundaries, in both languages. The Chinese SENTENCE terminators belong here
# and were missing: `_clause_for` splits sentences on `[.!?]`, which a Chinese line does
# not contain, so without 。 here a "fragment" ran straight through a full stop into the
# next sentence. Unity Candle Ceremony V is the case that shows it -- the fragment for
# 捧花 began in the previous sentence and picked up its 敵人:
#
#   ...對擁有「精英」狀態的敵人不會發動)。行動結束後對我方全體附加捧花
#                        ^^^^ a different sentence      ^^^^ the real recipient
#
# giving "enemy" for a status the prose grants to 我方全體. A full stop is a strictly
# stronger boundary than the 、／，this list already had, so adding it cannot widen a
# fragment -- only narrow one.
_FRAGMENT_SPLIT = re.compile(r",\s*|\s+and\s+|;\s*|、|，|並|且|。|；|！|？", re.I)


def _fragment_for(clause, name):
    """-> the part of `clause` that governs `name`, or the whole clause."""
    if not clause or not name:
        return clause
    low, want = clause.lower(), name.lower()
    at = low.find(want)
    if at < 0:
        return clause
    start = 0
    for m in _FRAGMENT_SPLIT.finditer(clause):
        if m.end() <= at:
            start = m.end()
        else:
            break
    end = len(clause)
    for m in _FRAGMENT_SPLIT.finditer(clause):
        if m.start() >= at + len(want):
            end = m.start()
            break
    return clause[start:end]


def _who_in(text):
    """-> "self" | "ally" | "enemy" | None for one fragment."""
    if not text:
        return None
    text = _EXCLUSION.sub(" ", text)
    stripped = _POSSESSIVE_SOURCE.sub(" ", text)
    for probe in (stripped, text):
        if _GAUGE_ENEMY.search(probe):
            return "enemy"
        if _GAUGE_SELF.search(probe):
            return "self"
        if _GAUGE_ALLY.search(probe):
            return "ally"
    return None


def passive_who(r, name, zh_name=None):
    """-> (who, disagreed): the side a passive's status lands on, Chinese preferred.

    The pack's English is a TRANSLATION and it is not always faithful. Beelzebub's
    passive is the case that proved it matters: the English says "inflict Headwind on all
    allies" where the Chinese says 對敵方全體附加逆風 -- "on all ENEMIES". Headwind stops a
    unit's move gauge, so believing the English gave her a self-inflicted gauge block and
    she took zero turns in a 62-attack fight.

    `_note1` is the ORIGINAL language, which is why it wins outright on a disagreement
    rather than merely being consulted. The disagreement is recorded so the count of them
    is a thing we can look at rather than a thing we assume is small.
    """
    # `_who_for`, the same reader status_target uses -- NOT `_fragment_for` on the first
    # mention. A status is routinely named twice in one passive, once as something the
    # holder is IMMUNE to and once as something it INFLICTS, and the first mention wins
    # the old way. Gabriel (SP), the Guild Weekly boss, is the case: her prose reads
    #
    #     鋼鐵身軀：戰鬥開始時，自身免疫暈眩，持續3回合。
    #     撼地鐵拳：戰鬥開始時，對敵方「技」屬性速度最高的2人附加暈眩(1回合)
    #
    # and the first fragment naming 暈眩 is the immunity, so Daze came out as `self`:
    # the boss dazed HERSELF for her opening turns. Seen on a phone 2026-08-25 -- the
    # saved battle showed her carrying Daze Immunity and Daze at once. `_who_for` picks
    # the fragment with a GRANTING verb (附加), and 免疫 is deliberately not one.
    en = _who_for(r.get("_note1_en"), name)
    zh = _who_for(r.get("_note1"), zh_name) if zh_name else None
    if zh and en and zh != en:
        return zh, True
    return (zh or en), False


def passive_chance(clause):
    """-> the probability the clause states, as a PERCENT, or None.

    Percent, not a fraction, because it is stored under `chance_pct` and that key has to
    mean one thing. It used to mean two: this function wrote 0.4 while `effects()` and
    core.execute's gauge and follow-up paths wrote 40.0 and divided by 100. Nothing was
    visibly broken only because the two paths never read each other's specs -- and
    `status_chance` above now writes the key on effects that DO reach both. Unified
    here, with `engine/passives.py` dividing at the point of use.
    """
    m = _STATED_CHANCE.search(clause or "")
    if not m:
        return None
    pct = float(m.group(1) or m.group(2))
    return pct if 0 < pct <= 100 else None


def passive_select(clause):
    """-> the SELECTION a clause names, beyond the coarse recipient.

    "inflict Diligence on the 1 ally with the highest DEF" is a different rule from "on
    all allies", and the pack states it in one very regular phrasing.
    """
    if not clause:
        return None
    if _ON_ATTACKER.search(clause):
        return {"who": "attacker"}
    m = _HIGHEST.search(clause)
    if m:
        n, who, stat = m.group(1), m.group(2).lower(), m.group(3).upper()
        return {"who": "ally" if who.startswith("all") else "enemy",
                "top": stat, "n": sp.WORD_NUM.get((n or "").lower(), None)
                                or (int(n) if (n or "").isdigit() else 1)}
    return None


def _norm_clause(text):
    """A comparison key for a clause.

    `_clause_for` searches the WHOLE note and returns "Diligence: When a battle starts,
    ..."; `_passive_clauses` splits each line at its "Name:" label and returns "When a
    battle starts, ...". Same sentence, two slices -- so comparing them literally left
    every clause looking unclaimed, and the implicit-trait default never fired.
    """
    text = re.sub(r"^[^:]{1,40}:\s*", "", (text or "").strip())
    return re.sub(r"\s+", " ", text).strip().lower()


def _passive_clauses(note):
    """The sentences of a passive's note that describe an effect happening.

    Glossary lines (`* Name: body`) and trailing fragments ("Lasts for 1 turn.",
    "(unremovable)") are neither rules nor evidence that a rule went unread, so they are
    not clauses.
    """
    out = []
    for line in (note or "").splitlines():
        line = line.strip()
        if not line or line.startswith("*"):
            continue
        body = line.split(":", 1)[1].strip() if ":" in line[:40] else line
        for sentence in re.split(r"(?<=[.!?])\s+", body):
            sentence = sentence.strip()
            if len(sentence) >= 12 and _DESCRIBES_EFFECT.search(sentence):
                out.append(sentence)
    return out


# Effects with no status to name them still have a clause: "After the action, reduces
# the Move Gauge of the enemy with the highest HP by 30%" is Zero Cal's second rule, and
# leaving it unlocatable made its whole clause look unaccounted for -- which in turn
# suppressed the implicit-trait default for the two traits that boss does carry.
_PASSIVE_EFFECT_WORD = dict(EFFECT_WORD)
_PASSIVE_EFFECT_WORD["remove_status"] = re.compile(r"\bremoves?\b|移除|解除", re.I)
_PASSIVE_EFFECT_WORD["damage"] = re.compile(r"\bdeals?\b[^,.;]{0,20}damage|造成.{0,6}傷害", re.I)
_PASSIVE_EFFECT_WORD["modify_cd"] = re.compile(r"cooldown|冷卻", re.I)


def _sentence_at(note, pos):
    """The sentence of `note` containing offset `pos`."""
    start = max(note.rfind(".", 0, pos), note.rfind("\n", 0, pos)) + 1
    ends = [e for e in (note.find(".", pos), note.find("\n", pos)) if e != -1]
    return note[start:(min(ends) + 1 if ends else len(note))].strip()


def annotate_passive(r, spec, rows=None):
    """Give every effect of a passive its trigger, in place.

    Three outcomes per effect, and the third is the one that needed care:

      * the clause naming the effect states a timing -> that trigger, source "prose".
      * the effect is named NOWHERE in the note, and every clause in the note has
        already been claimed by some other effect -> an undocumented always-on trait.
        `Elite`, `CC Immunity (SP)` and `Revive Block Immunity` are never described in
        prose on any boss that carries them; they are simply what a raid boss IS.
        Applied to the holder at battle start, permanently, source "implicit_trait".
      * the effect is unnamed but some clause is still unclaimed -> AMBIGUOUS, and left
        without a trigger. Dark Sanction forces this: its `Duel of the Fates(ATK)` row is
        described under the clause name "Destiny", so the status looks undocumented while
        its clause -- conditional, and keyed on "before attacking" -- sits unread.
        Defaulting that to a permanent battle-start buff would hand the boss an ATK bonus
        the prose gates behind a mark the party may never carry.

    Everything left over goes in `unmodelled`, so "which passives do we only partly
    execute?" stays a query the harness can answer rather than a silence.
    """
    note = r.get("_note1_en") or ""
    clauses = _passive_clauses(note)
    claimed, unmodelled, deferred = set(), [], []

    for e in spec.get("effects") or []:
        op = e.get("op")
        if op == "apply_status":
            name = ((e.get("status") or {}).get("name")) or ""
            clause, strong = _clause_for(note, name, _with_strength=True)
            if not strong:
                # A WEAK match -- the name merely appears somewhere, with no granting
                # verb in front of it -- is not evidence about timing. Trusting it is
                # worse than having nothing: Jacqueline's rows are named `Body Strike II`
                # while her prose calls the clause `Healthy Strike II`, so the weak
                # fallback landed on an unrelated sentence and derived `battle_start` for
                # a rule the prose keys on "before the action". A missing rule is a gap;
                # a wrong one is a bug.
                clause = None
        else:
            word = _PASSIVE_EFFECT_WORD.get(op)
            m = word.search(note) if (word and note) else None
            clause, name = (_sentence_at(note, m.start()) if m else None), op
        if not clause:
            deferred.append((e, name))
            continue
        claimed.add(_norm_clause(clause))
        trigger = passive_trigger(clause)
        source = "prose"
        if trigger is None and _ALWAYS_ON.search(clause):
            trigger, source = "battle_start", "always_on"
        e["trigger"] = trigger
        e["trigger_source"] = source if trigger else None
        sel = passive_select(clause)
        if op == "apply_status":
            zh_name = ((rows or {}).get((e.get("status") or {}).get("id")) or {}).get("_name")
            who, disagreed = passive_who(r, name, zh_name)
            if who:
                sel = dict(sel or {})
                sel.setdefault("who", who)
            if disagreed:
                e["prose_disagreed"] = True
                unmodelled.append({"effect": name, "why": "the English and Chinese prose "
                                   "name different recipients -- Chinese used",
                                   "clause": clause[:160]})
        if sel:
            e["select"] = sel
        # English only -- this whole annotator reads `_note1_en` -- so it must not
        # overwrite a probability `status_chance` already read out of the original.
        # On the 40 sites where the two languages state different odds, the English is
        # the wrong one, and a passive's are no more trustworthy than a cast's.
        stated = passive_chance(clause)
        if stated is not None and e.get("chance_source") != "prose_zh":
            e["chance_pct"], e["chance_source"] = stated, "prose"
        if trigger is not None and not e.get("requires"):
            # `is_conditional` flags any clause containing if/when/while, and on a
            # passive the trigger IS that word -- "When a battle starts, inflict
            # Diligence" is not a conditional application, it is an unconditional one
            # with a stated timing. Left set, every derived rule fired at the
            # CONDITIONAL_POLICY roll of 50% and a boss's permanent trait became a coin
            # flip. A real condition still arrives as `requires`, which is evaluatable
            # and is checked first.
            e["conditional"] = False
        if trigger is None:
            unmodelled.append({"effect": name, "why": "clause states no timing",
                               "clause": clause[:160]})

    # Containment either way: a note line can hold several sentences, so the slice
    # `_clause_for` returns and the one `_passive_clauses` yields need not be equal.
    def _is_claimed(c):
        key = _norm_clause(c)
        return any(key in got or got in key for got in claimed)

    unclaimed = [c for c in clauses if not _is_claimed(c)]

    # CLAIM BY STAT before giving up. A clause can describe a status by its EFFECT
    # rather than its name -- Gabriel (SP) II's prose says "increases the caster's SPD
    # by 30% for 1 turn (unremovable)" and never says "Linear Speedup", the row that
    # does exactly that. Unmatched, that one clause tripped the conservative rule
    # below and threw away EVERY unnamed status on the passive -- including
    # `Steady (SP)`, "Move Gauge will not decrease", the boss's whole defence against
    # gauge lock. On a phone (2026-08-26) the tier-1 AI then knocked her gauge back
    # every turn and she never took one: 110 rounds, 87 party actions, zero of hers.
    #
    # The match is deliberately narrow: the status's own description and the clause
    # must name the SAME stat and the SAME direction, and exactly one clause may fit.
    _STAT = re.compile(r"\b(HP|ATK|DEF|SPD|CR[TI]|CDI)\b", re.I)
    _UP = re.compile(r"increas|rais|\bup\b|提升|增加|上升", re.I)
    _DOWN = re.compile(r"decreas|reduc|lower|\bdown\b|下降|減少|降低", re.I)

    def _stat_dir(text):
        m = _STAT.search(text or "")
        if not m:
            return None
        stat = m.group(1).upper().replace("CRT", "CRI")
        return (stat, "up" if _UP.search(text) else "down" if _DOWN.search(text) else None)

    for e, name in list(deferred):
        if e.get("op") != "apply_status" or not unclaimed:
            continue
        srow = (rows or {}).get((e.get("status") or {}).get("id")) or {}
        want = _stat_dir(srow.get("_note1_en") or "")
        if not want or want[1] is None:
            continue
        fits = [c for c in unclaimed if _stat_dir(c) == want]
        if len(fits) != 1:
            continue
        clause = fits[0]
        unclaimed.remove(clause)
        claimed.add(_norm_clause(clause))
        deferred.remove((e, name))
        trigger = passive_trigger(clause)
        if trigger is None and _ALWAYS_ON.search(clause):
            trigger = "battle_start"
        e["trigger"] = trigger
        e["trigger_source"] = "prose_by_stat" if trigger else None
        who = _who_in(clause)
        if who:
            sel = dict(e.get("select") or {})
            sel.setdefault("who", who)
            e["select"] = sel
        e["conditional"] = False
        # The clause carries the timing the status row cannot: "for 2 turns" /
        # "(2回合)", or "the entire battle" / "整場戰鬥". Without this a claimed
        # 2-turn SPD buff would fall to the engine's unstated-duration default.
        nums = e.setdefault("numbers", {})
        dm = re.search(r"(\d+)\s*(?:turns?|回合)", clause)
        if dm:
            nums["duration"] = int(dm.group(1))
            nums["permanent"] = False
        elif re.search(r"entire battle|whole battle|整場|持續整場", clause, re.I):
            nums["duration"] = None
            nums["permanent"] = True
        # ...AND THE MAGNITUDE, which is the whole reason the clause is worth claiming.
        # This set the trigger, the recipient and the duration and then dropped the
        # number, so a status matched this way applied, drew its icon, counted down and
        # moved nothing. Raphael's 待客之道 is the case that showed it: 自身防禦力+75%
        # for 3 turns reached the fight as a 3-turn `Dedication` with magnitude None --
        # and because his counter is 140% of DEF, the buff missing cost him the counter
        # too, not just the defence.
        #
        # The stat and direction come from `want`, which is how the clause was matched
        # in the first place -- the same pair on both sides is the condition for getting
        # here, so there is nothing to re-derive and nothing to guess.
        if nums.get("magnitude") is None:
            pm = re.search(r"(\d+(?:\.\d+)?)\s*[%％]", clause)
            if pm:
                nums["magnitude"] = float(pm.group(1))
                nums["magnitude_sign"] = 1 if want[1] == "up" else -1
                nums["stat"] = want[0]
                nums["source"] = "prose_by_stat"
                nums["raw"] = clause[:120]
                continue
        unmodelled.append({"effect": name, "why": "claimed by stat, not by name",
                           "clause": clause[:160]})

    for e, name in deferred:
        cat = ((e.get("status") or {}).get("category") or "").lower()
        if unclaimed:
            e["trigger"] = None
            unmodelled.append({"effect": name, "why": "not named in prose, and a clause "
                               "is unaccounted for", "clause": unclaimed[0][:160]})
        elif e.get("op") != "apply_status":
            # Only a STATUS can be an always-on trait. A bare damage or heal opcode with
            # no prose has no recipient, no magnitude and no timing -- there is nothing
            # to default it to.
            e["trigger"] = None
            unmodelled.append({"effect": name, "why": "no prose for a non-status effect"})
        elif cat == "debuff":
            # An undocumented DEBUFF is not a trait -- a trait is something the holder
            # has, and a debuff is something it does to somebody else. Whom it hits is
            # exactly what the missing prose would have said.
            e["trigger"] = None
            unmodelled.append({"effect": name,
                               "why": "undocumented debuff -- no recipient stated"})
        elif name and name.lower() in note.lower():
            # The prose DOES mention it, just not in a sentence that grants it -- most
            # often because the name is the clause's own label ("Jealousy Vortex:" for a
            # status row called `Jealousy`). Something is being said about this status
            # that this compiler cannot read, so inventing a permanent self-buff for it
            # is a guess against evidence rather than in the absence of it.
            e["trigger"] = None
            unmodelled.append({"effect": name, "why": "named in prose, but never granted "
                               "by a clause -- probably a clause label"})
        else:
            e["trigger"] = "battle_start"
            e["trigger_source"] = "implicit_trait"
            e["recipient"] = "caster"
            # PERMANENT, and the duration is dropped rather than kept. A trait is by
            # definition unnamed in this skill's prose, so any duration it carries came
            # from `corpus_default` -- the average of what OTHER skills say when they
            # grant the same status. For `CC Immunity (SP)` that default is 2 turns, read
            # off 36 player skills; applying it to a raid boss would give the boss two
            # turns of immunity in an 84-turn fight, which is not what "the boss is immune
            # to crowd control" means. Evidence about other skills is not evidence about
            # this one.
            nums = e.setdefault("numbers", {})
            nums["duration"] = None
            nums["permanent"] = True

    for c in unclaimed:
        unmodelled.append({"why": "clause matched no effect in the act list",
                           "clause": c[:160]})
    if unmodelled:
        spec["unmodelled"] = unmodelled


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
    # ...and the "additionally deal N% of the target's Max HP" clause the coefficient
    # reader above stops before. Skills only; the status rows that describe self-damage
    # in the same words are excluded by the pattern itself.
    if spec["type"] != "status":
        extra = extra_maxhp_damage(r)
        if extra:
            spec["effects"].append(extra)
    # The clause ledger. `effects` fills it with the (kind, index) of every prose
    # clause an opcode consumed; the `uncovered_*` pass emits the clauses nothing took.
    # Before `annotate_passive` so a passive's heal is given the trigger its own
    # sentence states.
    claimed = set()
    eff, unknown = effects(rows, r, claimed)
    spec["effects"].extend(eff)
    _count_clauses(r, claimed)
    spec["effects"] = drop_clauseless_repeats(spec["effects"])
    spec["effects"].extend(uncovered_heals(r, claimed))
    spec["effects"].extend(uncovered_gauges(r, claimed))
    spec["effects"].extend(uncovered_removes(r, spec["effects"]))
    spec["effects"].extend(uncovered_revives(r, claimed))
    spec["effects"] = drop_rider_duplicates(spec["effects"])
    if spec["type"] != "status":
        steal = lifesteal_rider(r)
        if steal and not any(e.get("basis") == "damage_dealt"
                             for e in spec["effects"]):
            spec["effects"].append(steal)
    if unknown:
        spec["unknown"] = unknown
    if typ == "passive":
        annotate_passive(r, spec, rows)
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

    # Section 3: the English is a translation and it has real errors. Printed rather
    # than accumulated quietly, so a growing count is something somebody notices.
    if _WHO_DISAGREEMENTS:
        print(f"  recipient prose disagreements: {len(_WHO_DISAGREEMENTS)} "
              f"(Chinese used; sample below)")
        for sid, name, zh, en in _WHO_DISAGREEMENTS[:5]:
            print(f"    skill {sid} / {name}: zh={zh} en={en}")
    if _COND_DISAGREEMENTS:
        on = sum(1 for _s, _n, zh, _e in _COND_DISAGREEMENTS if zh)
        print(f"  conditional prose disagreements: {len(_COND_DISAGREEMENTS)} "
              f"(Chinese used; {on} gained a condition, "
              f"{len(_COND_DISAGREEMENTS) - on} lost one)")
        for sid, name, zh, en in _COND_DISAGREEMENTS[:5]:
            print(f"    skill {sid} / {name}: zh={zh} en={en}")

    # The clause ledger. One line, always: see LEDGER. `cd` has no `uncovered_cd`, so
    # its unclaimed clauses are a measured gap rather than something that gets emitted.
    for k in ("heal", "gauge", "cd", "revive"):
        seen, claimed = LEDGER[k + ".seen"], LEDGER[k + ".claimed"]
        print(f"  clauses/{k:<7}  : {seen} seen, {claimed} claimed by an opcode, "
              f"{seen - claimed} not ({LEDGER[k + '.unclaimed']} emitted from prose, "
              f"{LEDGER[k + '.disagree']} value conflicts)")
    print(f"  heal clauses a rider already covered: {LEDGER['heal.rider_covered']}"
          f"; repeated rider heals dropped: {LEDGER['heal.rider_repeat']}"
          f"; clause-less heal repeats dropped: {LEDGER['heal.clauseless_repeat']}")

    if args.stats:
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

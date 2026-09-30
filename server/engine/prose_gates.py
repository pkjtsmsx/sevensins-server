"""Gate names the compiler read out of the prose but could not map to a status row.

198 effects carry a `requires` whose status name never matched a row, so the gate is
unevaluatable and falls through to `UNSTATED_CHANCE` -- a 75% roll on every cast,
regardless of what the prose actually asks. Belphegor's Flipped Out pays a 600% DEF
bonus "if the caster holds Harden or Harden UL"; today it pays three casts in four
whether he holds it or not.

Most of the names are not really unknown. They are a CATEGORY rather than a status, or a
real status under a name the row does not use:

    可解除 / 可清除        "a removable status" -- a category, never a row name
    能力下降              "a stat-down", i.e. any debuff that lowers a stat
    不可堆疊能力下降       the same, restricted to the non-stacking ones
    控制異常              "a control effect" -- the 20 rows the pack tags
                          "(Crowd Control Debuff)" in their own description
    疲勞                  Fatigue 7004, whose row is named 疲労(5) -- the Chinese 勞
                          against the Japanese kanji 労, one character apart
    金剛 / 超 •金剛        Harden 2003 and Harden UL 2037
    『客人就是神』達5次以上  Customer is God 3823, with the brackets and the stack
                          count glued onto the name
    5傾心                 Affection 7026, five stacks, same shape

NOT Rigidity. A contributed version of this table mapped 金剛 to Rigidity, which is a
DIFFERENT status -- 剛體(5), row 3002. The prose that uses the gate settles it:
若攻擊時自己擁有金剛或超 •金剛，附加600%防禦力傷害, and Belphegor's own Out of It applies
2003 at rungs I-V and 2037 at VI. The kit hands itself the status the gate asks for.

DELIBERATELY LEFT UNRESOLVED: 5層, 3層, 2層 -- a stack count with the status name
elided ("if it has 5 stacks", of what the sentence never says). Four effects. Guessing
which marker is meant would be worse than the roll, because a wrong gate is a rule that
fires on the wrong turns rather than one that is merely imprecise.
"""
import re

from . import specs, status as _status

# name -> how to answer it. `status`/`any_of` name rows; `kind` names a category.
GATE_ALIASES = {
    "可解除": {"kind": "removable"},
    "可清除": {"kind": "removable"},
    "能力下降": {"kind": "stat_down"},
    "不可堆疊能力下降": {"kind": "stat_down"},
    "控制異常": {"kind": "crowd_control"},
    "疲勞": {"status": "Fatigue"},
    "5傾心": {"status": "Affection", "count": 5},
    "『客人就是神』達5次以上": {"status": "Customer is God", "count": 5},
    "金剛或超 •金剛": {"any_of": ("Harden", "Harden UL")},
}

_CC_TAG = re.compile(r"\(\s*crowd control[^)]*\)", re.I)
_CC_IDS = None


def _cc_ids():
    """The status ids the pack itself tags as crowd control, read from its own text.

    The tag is the pack's classification rather than ours -- compile_statuses already
    treats `(Crowd Control Debuff)` as authoritative over its pattern scan, for the same
    reason. Regen carries the tag too, which looks odd and is the game's own answer.

    MATCHED AS THE PARENTHETICAL, not as the words anywhere in the text. A loose
    substring pulls in 17 more rows that merely MENTION crowd control -- "removes the
    caster's crowd control", "will not be changed while being affected by crowd control
    and debuffs" -- none of which is a control effect itself. Same regex shape as
    compile_statuses.CC_TAG_RE, for the same reason.
    """
    global _CC_IDS
    if _CC_IDS is None:
        _CC_IDS = {int(sid) for sid, row in (specs.statuses() or {}).items()
                   if _CC_TAG.search(str(row.get("description") or ""))}
    return _CC_IDS


def resolve(name):
    """-> the alias entry for an unresolved gate name, or None."""
    return GATE_ALIASES.get((name or "").strip())


def holds(unit, alias, snapshot=None):
    """Does `unit` satisfy this alias? -> True/False, or None if unanswerable.

    None rather than False for a category asked of a unit we cannot read: the caller's
    contract is tri-state, and "no statuses visible" is not the same as "no debuff".
    """
    if unit is None:
        return None
    actives = [a for a in getattr(unit, "statuses", []) or []
               if isinstance(a, _status.Active)]
    kind = alias.get("kind")
    if kind == "removable":
        return any(not getattr(a, "unremovable", False) for a in actives)
    if kind == "stat_down":
        # The pack's own classification, not a sign test: `category` is what marks a row
        # as a debuff, and a stat_mod debuff is exactly what 能力下降 names. A sign test
        # would also catch a buff stored with a negative magnitude.
        return any(a.kind == "stat_mod" and a.category == "debuff" for a in actives)
    if kind == "crowd_control":
        return any(int(a.status_id or 0) in _cc_ids() for a in actives)
    names = alias.get("any_of") or ((alias.get("status"),) if alias.get("status") else ())
    if not names:
        return None
    from .core import _holds_status
    for nm in names:
        got = _holds_status(unit, nm, snapshot, alias.get("count"))
        if got:
            return True
        if got is None:
            return None            # unanswerable beats a false negative
    return False

# ---- riders the prose bills PER HIT ------------------------------------------
# `_rider` pays a target-Max-HP bonus ONCE, at swing 0, and that is right for the 144 rows
# on that branch which state their figure once -- the Guild Weekly boss's Special Sanction
# says 35% and a per-swing reading would double it.
#
# Seven rows say otherwise, in as many words:
#
#     140%攻擊力的2段傷害，每段傷害額外造成敵方最大體力10%的傷害
#     ("...2-hit damage, EACH HIT additionally deals 10% of the enemy's max HP")
#
# Belial's Marvelous Combo ladder plus its alt-band rung. `swings` is 2 on every one, so
# firing once paid half the clause. Verified by intersecting the per-hit prose with the
# effects that actually reach the target_max_hp branch: exactly these seven of 151.
#
# NOT GENERALISED from "the prose mentions 每段": 240 skills say it and 75 carry some
# rider, but most of those riders are ordinary ATK bonuses on a different branch, where
# `execute` already runs the damage effect once per swing. Only the Max-HP branch fires
# once by construction, so only it needs this.
PER_HIT_MAXHP_RIDER = frozenset({
    2029101, 2029102, 2029103, 2029104, 2029105, 2029106,   # Marvelous Combo I-VI
    130000871,                                              # Marvelous Combo V, alt band
})


def rider_per_hit(skill_id):
    """-> True if this skill's target-Max-HP rider lands once per SWING."""
    return int(skill_id or 0) in PER_HIT_MAXHP_RIDER

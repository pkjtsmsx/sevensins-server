"""Damage arithmetic. Every tunable is named and lives in this file.

**This is the one genuinely invented part of the engine.** There is no damage calculation
anywhere in the client -- no `CalcDamage`, no `GetDamage`; the `Formula` static class and
the `formula` design form are both progression costs (gold curves, rank-up XP). The pack
gives us the *coefficient* (`108% ATK`) and nothing about what turns it into a number.

So the rule here is: keep the arithmetic small, keep every constant named and in one
place, and never bury a magic number in the middle of an expression. When calibration
says the curve is wrong, it should be obvious what to turn.

## The stat model is the client's, not ours

`BattleAttributeData` (the in-battle unit-detail popup) already carries the full stat
line, and we have been sending most of it as zeros:

    hp atk def spd scv  cri tgn cdi cdr prc  ehit eanti ddi ddr

Those map onto the mechanics below: `cri` crit rate, `cdi`/`cdr` crit damage up/down,
`ehit`/`eanti` effect accuracy and resistance (the "EFF ACC" of the attribute rules),
`ddi`/`ddr` damage dealt up / damage taken down, `prc` defence pierce. They are real
fields the client will render, so the engine models them rather than inventing parallel
ones.

## Attribute advantage

STR / AGI / TEC. The client states that the triangle exists -- text 17051, "Allies with
advantageous attributes deal increased damage, while damage dealt by allies with other
attributes is reduced" -- but states no numbers anywhere in the pack. The rules encoded
in `ADVANTAGE` / `DISADVANTAGE` below are **community-documented, not extracted**, and
are flagged as such so nobody later mistakes them for something read out of the data.

Note the shape is not a flat multiplier, which is why it is worth writing down properly:
on advantage a non-crit is *upgraded* into a "strong attack", so an advantaged hit is
always either a crit or +30%.
"""
import math

# --- the invented core ------------------------------------------------------------
#
# Defence uses the standard rational curve rather than flat subtraction: subtraction
# inverts (high DEF making a hit heal) and needs clamping everywhere, while
#   mitigation = K / (K + DEF)
# is monotonic, never negative, and has exactly one tunable. K is the DEF value at which
# incoming damage is halved.
DEFENCE_HALF_AT = 1200.0

# Damage variance, applied last. Small, and symmetric so it does not shift the mean.
VARIANCE = 0.05

# Crit, when the unit's own `cri` is unset.
BASE_CRIT_RATE = 0.05
BASE_CRIT_MULT = 1.5

MIN_DAMAGE = 1          # a connecting hit always shows a number; 0 reads as a miss

# --- attribute triangle -----------------------------------------------------------
#
# SOURCE: community documentation, NOT the design pack. The pack confirms the triangle
# exists (text 17051) and gives no numbers. Treat these as calibration inputs.
# The attribute lives in `char._job`, and these ARE the design-pack values -- not an
# enum of ours. `UICharacterRoom.UpdateCharInfo` loads the badge beside the name from
# sprite `Job + 51801` and hides it when Job == 0, and those sprite rows name them:
#
#   0  icon_charclass_void      no attribute; the badge is hidden      (237 rows)
#   1  icon_charclass_hex       ABYSS   Lucifer, Satan, Mammon, Belial, Metatron
#   2  icon_charclass_strength  STR     (1,155 rows)
#   3  icon_charclass_speed     AGI     (1,092 rows)  -- 使速度型 in the mission text
#   4  icon_charclass_skill     TEC     (1,145 rows)  -- 使技巧型
#   5  icon_charclass_psychic   SOLAR   Leviathan, Panagia, Michael, Sariel, Gabriel
#
# `hex`/`psychic` are the internal names; ABYSS/SOLAR are what the game shows. Only ten
# characters carry them and all are 5-star, which is why the low job values looked like
# noise next to the ~1,100-row STR/AGI/TEC blocks.
NONE, ABYSS, STR, AGI, TEC, SOLAR = 0, 1, 2, 3, 4, 5

ATTRIBUTE_NAME = {NONE: None, ABYSS: "ABYSS", STR: "STR", AGI: "AGI",
                  TEC: "TEC", SOLAR: "SOLAR"}

# Which attribute beats which.
#
# The in-game triangle is drawn in COLOUR, not in words: red beats yellow beats blue
# beats red. Mapping colour to attribute by cropping the icons out of
# `common/main/atlas_main_lobby` at the rects the NGUI `UIAtlas` gives (verified
# orientation -- the as-is crops have transparent corners, the y-flipped ones land
# mid-atlas on opaque pixels):
#
#   STR  icon_charclass_strength  most-saturated pixel (254,  44, 189)  red / magenta
#   TEC  icon_charclass_skill                          (218, 247,  24)  yellow
#   AGI  icon_charclass_speed                          (115,  77, 255)  blue / violet
#
# so red->yellow->blue->red is STR -> TEC -> AGI -> STR.
#
# SOLAR / ABYSS are NOT in the triangle -- only ten characters have them. They are
# treated as mutually advantaged, which is the one genuinely unverified entry here;
# `advantage()` derives both directions from this dict, so changing it cannot leave the
# two halves inconsistent.
BEATS = {STR: TEC, TEC: AGI, AGI: STR, SOLAR: ABYSS, ABYSS: SOLAR}

ADVANTAGE = {
    "crit_rate": +0.15,
    # A non-crit is upgraded to a "strong attack" instead of being wasted, so an
    # advantaged hit is never just a normal hit.
    "strong_attack_mult": 1.30,
    "eff_acc": +0.15,
}
DISADVANTAGE = {
    "damage_mult": 0.90,
    "crit_rate": -0.15,
    "eff_acc": -0.15,
    "miss_chance": 0.30,        # a "miss" is not a whiff -- it is a damage penalty
    "miss_mult": 0.70,
}


def advantage(attacker_attr, defender_attr):
    """-> +1 advantaged, -1 disadvantaged, 0 neutral.

    `NONE` (job 0 -- the 237 rows whose badge the client hides) is neutral both ways, as
    is any attribute we could not determine. An unknown attribute must never become a
    penalty: that would turn a data gap into a gameplay change.

    SOLAR/ABYSS is a MUTUAL entry, so both directions return +1 -- each is advantaged
    against the other, and neither is ever disadvantaged by it. That is a deliberate
    reading of "they oppose each other" and it is the part most likely to be wrong; the
    alternative is that they simply do not interact. `BEATS` is the only place to change
    it: drop the two entries for neutral, or point them at a third attribute for a
    one-directional counter.
    """
    if not attacker_attr or not defender_attr:
        return 0
    if BEATS.get(attacker_attr) == defender_attr:
        return 1
    if BEATS.get(defender_attr) == attacker_attr:
        return -1
    return 0


def _basis_value(basis, caster, target):
    """The stat a coefficient multiplies.

    `MAX_HP` coefficients read the TARGET's pool -- "deals 20% of the target's Max HP as
    damage" is how the pack words them -- while ATK/DEF read the caster.
    """
    if basis == "MAX_HP":
        return float(target.max_hp)
    if basis == "DEF":
        return float(caster.defence)
    return float(caster.atk)


def mitigation(defence, pierce=0.0):
    """-> the fraction of raw damage that survives the target's DEF."""
    eff = max(0.0, float(defence) * (1.0 - min(1.0, max(0.0, pierce))))
    return DEFENCE_HALF_AT / (DEFENCE_HALF_AT + eff)


def strike(caster, target, coefficient, basis="ATK", rng=None, extra_mult=1.0):
    """-> (amount, detail) for one landed blow.

    `detail` records which branch fired -- crit, strong attack, attribute miss -- because
    when a number looks wrong the first question is always "which multiplier did that?"
    and reconstructing it afterwards from a bare integer is guesswork.

    A `coefficient` of None means the pack never stated one (986 damage effects, mostly
    untranslated mob skills). The caller decides the policy; this refuses to invent one.
    """
    if coefficient is None:
        return None, {"reason": "coefficient unknown"}
    r = rng or __import__("random").Random()
    adv = advantage(getattr(caster, "attribute", None), getattr(target, "attribute", None))

    raw = _basis_value(basis, caster, target) * float(coefficient)
    detail = {"raw": raw, "advantage": adv}

    crit_rate = getattr(caster, "cri", None)
    crit_rate = BASE_CRIT_RATE if crit_rate is None else float(crit_rate)
    if adv > 0:
        crit_rate += ADVANTAGE["crit_rate"]
    elif adv < 0:
        crit_rate += DISADVANTAGE["crit_rate"]

    mult = extra_mult
    crit = r.random() < max(0.0, min(1.0, crit_rate))
    if crit:
        mult *= BASE_CRIT_MULT * (1.0 + getattr(caster, "cdi", 0.0)
                                  - getattr(target, "cdr", 0.0))
    elif adv > 0:
        # The advantage rule: a failed crit becomes a strong attack rather than nothing.
        mult *= ADVANTAGE["strong_attack_mult"]
        detail["strong_attack"] = True
    detail["crit"] = crit

    if adv < 0:
        mult *= DISADVANTAGE["damage_mult"]
        if r.random() < DISADVANTAGE["miss_chance"]:
            mult *= DISADVANTAGE["miss_mult"]
            detail["attribute_miss"] = True

    mult *= (1.0 + getattr(caster, "ddi", 0.0)) * (1.0 - getattr(target, "ddr", 0.0))
    mit = mitigation(target.defence, getattr(caster, "prc", 0.0))
    amount = raw * mult * mit
    amount *= 1.0 + r.uniform(-VARIANCE, VARIANCE)

    detail["mitigation"] = mit
    detail["multiplier"] = mult
    return max(MIN_DAMAGE, int(math.floor(amount))), detail


def effect_lands(caster, target, chance, rng=None):
    """-> whether a chance-based status application sticks.

    `ehit`/`eanti` are the client's own effect-accuracy fields, shifted by the attribute
    triangle exactly as the rules state.
    """
    r = rng or __import__("random").Random()
    adv = advantage(getattr(caster, "attribute", None), getattr(target, "attribute", None))
    acc = float(chance) + getattr(caster, "ehit", 0.0) - getattr(target, "eanti", 0.0)
    if adv > 0:
        acc += ADVANTAGE["eff_acc"]
    elif adv < 0:
        acc += DISADVANTAGE["eff_acc"]
    return r.random() < max(0.0, min(1.0, acc))

"""Starshards (runes): drop/display, wearing, and upgrading (BackpackType 2).

Split out of the former monolithic core.py; depends only on .core.
"""


import random

import battle as bt

from .core import (
    ATTR_CDI,
    ATTR_CRI,
    ATTR_EANTI,
    ATTR_EHIT,
    ATTR_PATK,
    ATTR_PDEF,
    ATTR_PHP,
    BP_STORAGE_EQUIPMENT,
    CHAR_EQUIP_SLOTS,
    CURRENCY_COIN,
    RUNE_ATTR_LEVEL,
    RUNE_ATTR_SUB,
    RUNE_ATTR_SUB_ENHANCE,
    RUNE_BONUS_ATTRS,
    RUNE_MAX_LEVEL,
    char_equips,
    uid,
)


# CharAttribute.PercentStyleAttrs -- percentages are stored MULTIPLIED BY TEN.
# We no longer compute any of these ourselves (see below), but the mapping is worth
# keeping: it is what makes `GetValueText` render 420 as "42.0%".
PERCENT_ATTRS = {ATTR_CRI, ATTR_CDI, ATTR_EHIT, ATTR_EANTI,
                 ATTR_PHP, ATTR_PATK, ATTR_PDEF}

# EquipDefine: RunePrimaryAttrNumber 1, RuneBonusAttrNumber 4, RuneMaxLevel 15.
RUNE_PRIMARY_ATTRS = 1


def wear_runes(state, char_uid, equips):
    """Apply a char_wear_rune request. -> (the stored 18-slot array, stolen-from).

    `stolen-from` maps every OTHER cast we took a piece off to its rebuilt array; the
    caller must push a `char_update_equip` for each, or the client keeps showing the
    starshard on its old wearer as well (`PlayerChar.receivedUpdateEquip` is the only
    thing that clears `DBCharData`, and it only touches the uid it is handed).

    Raises KeyError for an unknown cast and ValueError for a uid we do not hold, so the
    caller can decline instead of sending a reply the client would choke on.
    """
    entry = state["roster"][char_uid]
    # **The request is SHORTER than the reply.** Verified on the wire 2026-08-08:
    # UIRuneEquipment passes its own `_charEquips`, which is the SIX rune slots only, so
    # cmd 295 arrives with 7 strargs (6 + uid) while cmd 549 still demands all 19. Only
    # overwrite the slots the client actually sent -- padding the rest with "" would
    # silently strip bloodpacts and anything else living in slots 6..17.
    slots = char_equips(entry)
    sent = list(equips)[:CHAR_EQUIP_SLOTS]
    for i, uid in enumerate(sent):
        slots[i] = str(uid or "")
    owned = {e.get("uid") for e in state["backpack"]
             .get(str(BP_STORAGE_EQUIPMENT), {}).values()}
    # Validate ONLY the slots this request carried. The rest of the array is
    # soulmirrors (6..14) and bloodpacts (12..14), which live in other storages --
    # checking them against storage 2 refused every starshard change on a cast that
    # happened to wear a soulmirror, and the client had already applied it locally.
    for uid in slots[:len(sent)]:
        if uid and uid not in owned:
            raise ValueError(f"equip uid {uid!r} is not in storage {BP_STORAGE_EQUIPMENT}")
    # One piece can only be worn once: drop it from whoever else was wearing it.
    worn = {u for u in slots if u}
    stolen = {}
    for other_uid, other in state["roster"].items():
        if other_uid == char_uid:
            continue
        cur = char_equips(other)
        if any(u in worn for u in cur):
            other["equips_list"] = [("" if u in worn else u) for u in cur]
            stolen[other_uid] = other["equips_list"]
    entry["equips_list"] = slots
    return slots, stolen


# ---- upgrading starshards (Backpack 99 EnchantGem_Req -> 100 + a 145 push) --
#
# `SendEnchantGemReq(itemUID, cnt)` (0x18E8AE0) sends cmd 99 with `intargs=[cnt]`,
# `strargs=[itemUID]`. **`cnt` is a LEVEL COUNT** -- it is `_curEnhancedTimes`, the
# panel's +/- stepper, and `UIRuneEnhance.OnClickConfirmBtn` (0x189ECDC) prices the
# click as `GetRangeGemCost(row._rarity, lv, lv + cnt, row._param2)`.
#
# **The reply carries nothing.** `HandleEnchantGemRply` (0x18EBA7C) is a single RET, so
# cmd 100 is a bare ack; the client learns the new state only from a cmd-145
# BackpackChange push (see backpacks_all_json) plus a currency sync.
#
# Cost, from `Formula.GetGemCost` (0x18F662C) -- note the call site passes `_rarity` as
# the parameter named `star` and `_param2` as the one named `rarity`, so the names are
# effectively swapped relative to the design columns:
#     cost(lv) = a*(lv+1) + (lv+1)**(1 + k*star) * (c*rarity + b*star)
# rounded DOWN to a multiple of 50, summed over each level crossed. Verified against the
# client's own display: item 201201 is _rarity 1 / _param2 1, giving 300 for lv0->1 and
# 600 for lv1->2 -- exactly the "Cost 900" the panel showed for a "+2" upgrade.
# `_param3 != 0` would instead route to DesignRuneSettingForm.GetEnchantMoneyCost; no
# shipped starshard item has that, so it is not modelled.
RUNE_GEM_COST = {          # star -> (k, a, b, c)
    1: (0.09, 135, 125, 50),
    2: (0.10, 150, 150, 75),
    3: (0.11, 175, 175, 100),
    4: (0.12, 275, 325, 175),
    5: (0.14, 375, 540, 250),
}
RUNE_GEM_COST_DEFAULT = (0.15, 500, 1200, 375)      # star 6 and above


def rune_level_cost(item_id, before_lv):
    """Coin cost of taking a starshard from `before_lv` to `before_lv + 1`."""
    row = bt.dd.row("item", int(item_id)) or {}
    star = int(row.get("_rarity") or 0)
    rarity = int(row.get("_param2") or 0)
    k, a, b, c = RUNE_GEM_COST.get(star, RUNE_GEM_COST_DEFAULT)
    raw = a * (before_lv + 1) + (before_lv + 1) ** (1 + k * star) * (c * rarity + b * star)
    return 50 * int(raw / 50)


def rune_upgrade_cost(item_id, before_lv, to_lv):
    """Formula.GetRangeGemCost: the per-level costs summed over the range."""
    return sum(rune_level_cost(item_id, lv) for lv in range(before_lv, to_lv))


def find_rune(state, uid):
    """-> (slot key, entry) for a starshard uid, or (None, None)."""
    for sid, entry in state["backpack"].get(str(BP_STORAGE_EQUIPMENT), {}).items():
        if entry.get("uid") == uid:
            return sid, entry
    return None, None


# ---- sub-stat growth ------------------------------------------------------------
#
# THE BUG THIS FIXES. The client derives every displayed number in `GetEquipGrowValue`
# (see the RUNE_ATTR_* block in core.py):
#
#     ppv_<i> = row._AttrInitV + row._AttrUpV * lv      <- primary, grows with lv
#     bpv_<j> = (be_<j> + 1) * row._AttrInitV           <- sub-stat, grows with be_
#
# **The sub-stat term contains no `lv`.** Upgrading wrote only `lv`, and `be_<j>` was
# written once at creation (core.py `_rune_attr`/`_soulfrag_attr`) and by nothing else,
# so the primary crept up on every upgrade while all four sub-stats stayed frozen at
# their roll value for the life of the piece.
#
# CADENCE IS AN ASSUMPTION, and the only invented part. How often an upgrade enhances a
# sub-stat is not in the pack -- `rune_setting` carries cost tables only (`_type` 1..5,
# `_para` = [rarity, ?, level]) and no other form has it. Every 3 levels divides
# RUNE_MAX_LEVEL = 15 into 5 enhances. Retune this one constant when a real rate is
# observed; the mechanism is right either way.
#
# Reported by a server user; the bug and the formula were both re-verified here against
# the decompilation notes before porting.
RUNE_LEVELS_PER_ENHANCE = 3


def enhance_steps(before_lv, to_lv, per=RUNE_LEVELS_PER_ENHANCE):
    """How many sub-stat enhances a climb from `before_lv` to `to_lv` earns.

    Counts multiples of `per` CROSSED, so one 0->15 click awards exactly what fifteen
    single-level clicks would. Level 0 is the unenhanced roll.
    """
    if per <= 0:
        return 0
    return max(0, int(to_lv) // per - int(before_lv) // per)


def apply_enhances(attr, steps, count, rng=None):
    """Spread `steps` enhances over the sub-stats present in `attr`.

    -> {sub index: new be_ value}, for logging.

    Each step lands on the least-enhanced sub-stat so no single stat runs away; ties
    break randomly rather than always taking `bid_1`, which would make the first slot
    grow twice as fast as the rest.
    """
    if steps <= 0 or count <= 0:
        return {}
    r = rng or random
    slots = [j for j in range(1, int(count) + 1) if f"{RUNE_ATTR_SUB}{j}" in attr]
    if not slots:
        return {}
    touched = {}
    for _ in range(int(steps)):
        low = min(int(attr.get(f"{RUNE_ATTR_SUB_ENHANCE}{j}", 0) or 0) for j in slots)
        pick = r.choice([j for j in slots
                         if int(attr.get(f"{RUNE_ATTR_SUB_ENHANCE}{j}", 0) or 0) == low])
        key = f"{RUNE_ATTR_SUB_ENHANCE}{pick}"
        attr[key] = int(attr.get(key, 0) or 0) + 1
        touched[pick] = attr[key]
    return touched


def upgrade_rune(state, uid, levels):
    """Apply an EnchantGem request. -> (entry, gained levels, coins spent).

    Raises LookupError for an unknown starshard and ValueError when it is already
    maxed or the player cannot afford the levels asked for.
    """
    _, entry = find_rune(state, uid)
    if entry is None:
        raise LookupError(f"no starshard {uid!r} in storage {BP_STORAGE_EQUIPMENT}")
    attr = entry.setdefault("attr", {})
    before = int(attr.get(RUNE_ATTR_LEVEL, 0))
    to_lv = min(before + max(0, int(levels)), RUNE_MAX_LEVEL)
    if to_lv <= before:
        raise ValueError(f"starshard {uid} is already at level {before}")
    cost = rune_upgrade_cost(entry["iid"], before, to_lv)
    have = int(state["currency"].get(str(CURRENCY_COIN), 0))
    if have < cost:
        raise ValueError(f"upgrade costs {cost} coins, holding {have}")
    state["currency"][str(CURRENCY_COIN)] = have - cost
    attr[RUNE_ATTR_LEVEL] = to_lv
    # Sub-stats grow through `be_<j>`, not `lv` -- see the block above.
    apply_enhances(attr, enhance_steps(before, to_lv), RUNE_BONUS_ATTRS)
    return entry, to_lv - before, cost

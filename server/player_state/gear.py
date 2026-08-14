"""Gear tables & ops: Soulmirrors (SoulFrag), Bloodpacts, the equipment padlock, and their cmd-514 cost tables.

Split out of the former monolithic core.py; depends only on .core.
"""


import json, time
import battle as bt

from .core import (
    BP_STORAGE_EQUIPMENT,
    BP_STORAGE_SOULFRAG,
    CURRENCY_COIN,
    GAME_RULE_DECOMPOSE_MAGNIFICATION_KEYS,
    GAME_RULE_EMPTY_DICTS,
    GAME_RULE_MAGNIFICATION_KEYS,
    RUNE_ATTR_LEVEL,
    RUNE_ATTR_SUB,
    RUNE_ATTR_TS,
    SOULFRAG_MAX_LEVEL,
    SOULFRAG_SLOT_INDEX,
    SUPER_LIMIT_DEFINE,
    char_equips,
    grant_item,
    grant_soulmirror,
    item_count,
    make_rune,
    make_soulmirror,
    rune_uid,
    soulfrag_slot,
    spend_item,
    uid,
)



def soulfrag_owner(item_id):
    """The char id a soulmirror belongs to (item `_param3`)."""
    return (bt.dd.row("item", int(item_id)) or {}).get("_param3")


def wear_soulmirror(state, char_uid, equip_uid, index):
    """Apply a char_wear_soulfrag request. -> the stored 18-slot array.

    `index` comes from the client (the jump-table value), so it is authoritative for
    WHERE the piece goes; we validate that it agrees with the item's own action rather
    than trusting it blindly. An empty `equip_uid` unequips the slot.
    """
    entry = state["roster"][char_uid]
    if index not in SOULFRAG_SLOT_INDEX.values():
        raise ValueError(f"index {index} is not a Soulmirror slot")
    slots = char_equips(entry)
    if equip_uid:
        owned = {e.get("uid"): e for e in state["backpack"]
                 .get(str(BP_STORAGE_SOULFRAG), {}).values()}
        piece = owned.get(equip_uid)
        if piece is None:
            raise ValueError(
                f"{equip_uid!r} is not in storage {BP_STORAGE_SOULFRAG}")
        want = soulfrag_slot(piece["iid"])
        if want != index:
            raise ValueError(
                f"Soulmirror {piece['iid']} belongs at index {want}, not {index}")
        # One piece, one wearer.
        for other_uid, other in state["roster"].items():
            cur = char_equips(other)
            if equip_uid in cur:
                other["equips_list"] = [("" if u == equip_uid else u) for u in cur]
        slots = char_equips(entry)
    slots[index] = str(equip_uid or "")
    entry["equips_list"] = slots
    return slots


# ---- bloodpacts (storage 4, equips slots 12..14) ----------------------------
#
# The third and last equip type. `GetItemSpace` routes `_action` 131..133 to the
# bloodpact list; in practice every shipped pact is `_action` **131**, so the action does
# NOT encode a slot the way runes (111..116) and soulmirrors (101..109) do. The item's
# `_param3` is the pact TYPE (1..15: Frenzy, Cruelty, Excitement, Serenade, Insight,
# Eclipse, Guidance, Void, Battlecry, Unlaws...) and `_param2` the grade (3 SR / 4 UR /
# 5 LR, plus two test rows).
#
# **Slots 12..14 are the gap the soulmirror jump table leaves** (0x3703540 maps soulfrag
# actions to 6..11 and 15..17). Cmd 297 `char_wear_bloodpact` carries NO slot index and
# NO intargs -- just `strargs=[char_uid, equip_uid]`, uid FIRST, the reverse of 296 --
# so the server chooses the slot.
#
# Instance shape is the same as the other two: `lv` is mandatory (storage 4 goes through
# RefreshEquipAttribute, cbpType 2..4) and `ts` is mandatory for the icon
# (`UIBloodPactIcon.SetData`, `InitPlayerBloodPactList` and `BloodPactComparer.Compare`
# all read it). Everything else GetEquipGrowValue guards with ContainsKey.
BP_STORAGE_BLOODPACT = 4
BLOODPACT_ACTION_RANGE = range(131, 134)
BLOODPACT_SLOT_BASE = 12                 # equips indices 12, 13, 14


def is_bloodpact(item_id):
    action = (bt.dd.row("item", int(item_id)) or {}).get("_action")
    return action in BLOODPACT_ACTION_RANGE


def bloodpact_slots():
    return range(BLOODPACT_SLOT_BASE, BLOODPACT_SLOT_BASE + BLOODPACT_SLOTS)


def bloodpact_skill_rows(item_id):
    """The ReplaceSkill rows a pact can carry.

    **A bloodpact's "skills" are `equipment_bonus` rows with `_Type == 2`
    (ReplaceSkill)** -- confirmed on device: seeding `bid_1`/`bid_2` with rows from the
    pact's own `_bonusID` group made the Forge screen render them as real named skills
    ("Hunting Blink -- Bloodpact Effect: Skill Damage+20%"). This is also why
    `GetEquipGrowValue` never turns them into stats: it only takes `_Type == Attribute`.
    """
    row = bt.dd.row("item", int(item_id)) or {}
    equip = bt.dd.row("equipment", row.get("_param1")) or {}
    group = equip.get("_bonusID")
    return sorted(int(rid) for rid, r in (bt.dd.rows("equipment_bonus") or {}).items()
                  if r.get("_group") == group and r.get("_Type") == 2)


def bloodpact_skill_slots(item_id):
    """How many skill slots the pact's grade opens (SR 1 / UR 2 / LR 3), per the
    `bloodpact_rarity_slot` table we publish."""
    grade = int((bt.dd.row("item", int(item_id)) or {}).get("_param2") or 0)
    table = bloodpact_game_rule()["bloodpact_rarity_slot"]
    return table[grade] if 0 <= grade < len(table) else 0


def make_bloodpact(state, item_id, level=0, skills=None, rng=None):
    """Build one bloodpact for storage 4.

    `lv` is mandatory (storage 4 goes through RefreshEquipAttribute) and `ts` is
    mandatory for the icon; `bid_N` carry the ReplaceSkill rows, one per open slot.
    """
    import random as _r
    rng = rng or _r
    if not is_bloodpact(item_id):
        raise ValueError(f"item {item_id} is not a bloodpact (_action 131..133)")
    attr = {RUNE_ATTR_LEVEL: int(max(0, min(level, BLOODPACT_MAX_LV))),
            RUNE_ATTR_TS: int(time.time())}
    pool = bloodpact_skill_rows(item_id)
    if skills is None:
        n = min(bloodpact_skill_slots(item_id), len(pool))
        skills = rng.sample(pool, n) if n else []
    for i, rid in enumerate(skills, start=1):
        attr[f"{RUNE_ATTR_SUB}{i}"] = int(rid)
    return {"iid": int(item_id), "amount": 1, "attr": attr}


def grant_bloodpact(state, item_id, level=0):
    """Put a bloodpact in storage 4. -> the stored entry."""
    bag = state["backpack"].setdefault(str(BP_STORAGE_BLOODPACT), {})
    sid = max((int(k) for k in bag), default=0) + 1
    n = len(bag) + 1
    uid = f"{state['player_id']}b{n:04d}"
    while any(e.get("uid") == uid for e in bag.values()):
        n += 1
        uid = f"{state['player_id']}b{n:04d}"
    entry = make_bloodpact(state, item_id, level)
    entry["sid"] = sid
    entry["uid"] = uid
    bag[str(sid)] = entry
    return entry


def wear_bloodpact(state, char_uid, equip_uid):
    """Apply a char_wear_bloodpact request. -> (slot index, the 18-slot array).

    The request names no slot, so we place it in the first free one of 12..14 (or the
    slot it already occupies). An empty `equip_uid` clears every bloodpact slot, since
    there is no index to say which.
    """
    entry = state["roster"][char_uid]
    slots = char_equips(entry)
    if not equip_uid:
        for i in bloodpact_slots():
            slots[i] = ""
        entry["equips_list"] = slots
        return None, slots
    owned = {e.get("uid"): e for e in state["backpack"]
             .get(str(BP_STORAGE_BLOODPACT), {}).values()}
    if equip_uid not in owned:
        raise ValueError(f"{equip_uid!r} is not in storage {BP_STORAGE_BLOODPACT}")
    # One pact, one wearer.
    for other_uid, other in state["roster"].items():
        cur = char_equips(other)
        if equip_uid in cur:
            other["equips_list"] = [("" if u == equip_uid else u) for u in cur]
    slots = char_equips(entry)
    target = next((i for i in bloodpact_slots() if not slots[i]),
                  BLOODPACT_SLOT_BASE)
    slots[target] = equip_uid
    entry["equips_list"] = slots
    return target, slots


def find_bloodpact(state, uid):
    """-> (slot key, entry) for a bloodpact uid, or (None, None)."""
    for sid, entry in state["backpack"].get(str(BP_STORAGE_BLOODPACT), {}).items():
        if entry.get("uid") == uid:
            return sid, entry
    return None, None


def bloodpact_upgrade_cost(item_id, before_lv, to_lv):
    """Sum of the enhance table over the levels bought -- the same cells the panel
    totals in `UpdateCostItems`, so the server charges exactly what was displayed."""
    grade = int((bt.dd.row("item", int(item_id)) or {}).get("_param2") or 1)
    tbl = _bloodpact_cost_table(2000)
    row = tbl[max(0, min(grade, BLOODPACT_GRADES)) - 1]
    return sum(row[lv] for lv in range(before_lv, min(to_lv, len(row))))


def upgrade_bloodpact(state, uid, levels):
    """Apply an EnhanceBloodpact request. -> (entry, gained levels, coins spent).

    The cost item is item 2 "Coin", which the panel renders against the coin balance,
    so it is charged to currency 16 rather than as a backpack item.
    """
    _, entry = find_bloodpact(state, uid)
    if entry is None:
        raise LookupError(f"no bloodpact {uid!r} in storage {BP_STORAGE_BLOODPACT}")
    attr = entry.setdefault("attr", {})
    before = int(attr.get(RUNE_ATTR_LEVEL, 0))
    to_lv = min(before + max(0, int(levels)), BLOODPACT_MAX_LV)
    if to_lv <= before:
        raise ValueError(f"bloodpact {uid} is already at level {before}")
    cost = bloodpact_upgrade_cost(entry["iid"], before, to_lv)
    have = int(state["currency"].get(str(CURRENCY_COIN), 0))
    if have < cost:
        raise ValueError(f"upgrade costs {cost} coins, holding {have}")
    state["currency"][str(CURRENCY_COIN)] = have - cost
    attr[RUNE_ATTR_LEVEL] = to_lv
    return entry, to_lv - before, cost


def mix_bloodpact(state, from_uid, to_uid, from_index, to_index):
    """Forge/"transcribe": copy one skill off a material pact onto the target.

    Verified on the wire 2026-08-09: cmd 260 carries `intargs=[fromIndex, toIndex]` and
    `strargs=[fromUID, toUID]`, with **1-based** slot indices that map straight onto the
    `bid_N` attr keys -- `int=[2, 1]` moved the material's slot-2 skill into the
    target's slot 1, replacing it, exactly as the panel's TRANSFER/REPLACE ticks showed.
    The material is consumed.

    -> (target entry, the skill row moved, coins spent, removed sid).
    """
    _, target = find_bloodpact(state, to_uid)
    if target is None:
        raise LookupError(f"no target bloodpact {to_uid!r}")
    src_sid, source = find_bloodpact(state, from_uid)
    if source is None:
        raise LookupError(f"no material bloodpact {from_uid!r}")
    if from_uid == to_uid:
        raise ValueError("a bloodpact cannot be its own material")
    skill = source.get("attr", {}).get(f"{RUNE_ATTR_SUB}{int(from_index)}")
    if skill is None:
        raise ValueError(f"material {from_uid} has no skill in slot {from_index}")
    slots = bloodpact_skill_slots(target["iid"])
    if not 1 <= int(to_index) <= slots:
        raise ValueError(
            f"target {to_uid} has {slots} slot(s); {to_index} is out of range")

    grade = int((bt.dd.row("item", int(target["iid"])) or {}).get("_param2") or 1)
    tbl = bloodpact_game_rule()["bloodpact3"]["formula"][0]["tbl"]
    cost = tbl[max(0, min(grade, len(tbl))) - 1]
    have = int(state["currency"].get(str(CURRENCY_COIN), 0))
    if have < cost:
        raise ValueError(f"forge costs {cost} coins, holding {have}")

    state["currency"][str(CURRENCY_COIN)] = have - cost
    target.setdefault("attr", {})[f"{RUNE_ATTR_SUB}{int(to_index)}"] = int(skill)
    # The material is destroyed -- take it off whoever was wearing it first.
    for _uid, other in state["roster"].items():
        cur = char_equips(other)
        if from_uid in cur:
            other["equips_list"] = [("" if u == from_uid else u) for u in cur]
    del state["backpack"][str(BP_STORAGE_BLOODPACT)][src_sid]
    return target, int(skill), cost, src_sid


def dismantle_bloodpacts(state, uids):
    """Dismantle pacts for their return. -> [[item id, amount], ...].

    `HandleDecomposeBloodpactRply` (0x18EC88C) parses `strargs[0]` as a
    **`List<List<uint>>`** of `[itemId, amount]` pairs, builds an ItemStruct per pair and
    shows them through `PanelItemMsg.ShowItemListPopUp`; `intargs[0]` is only logged.
    Each inner list must have at least two entries or it throws.

    The return comes from the `bloodpact2` table at `[grade - 1][lv]` -- the panel showed
    2500 for an unenhanced LR, which is exactly that cell.
    -> ([[item id, amount]], [removed sid, ...]).
    """
    tbl = bloodpact_game_rule()["bloodpact2"]["formula"][0]
    item_id = int(tbl["id"])
    gained = 0
    removed = []
    for uid in uids:
        sid, entry = find_bloodpact(state, uid)
        if entry is None:
            raise LookupError(f"no bloodpact {uid!r}")
        grade = int((bt.dd.row("item", int(entry["iid"])) or {}).get("_param2") or 1)
        row = tbl["tbl"][max(0, min(grade, len(tbl["tbl"]))) - 1]
        lv = int(entry.get("attr", {}).get(RUNE_ATTR_LEVEL, 0))
        gained += row[min(lv, len(row) - 1)]
        for _u, other in state["roster"].items():
            cur = char_equips(other)
            if uid in cur:
                other["equips_list"] = [("" if u == uid else u) for u in cur]
        del state["backpack"][str(BP_STORAGE_BLOODPACT)][sid]
        removed.append(sid)
    # Item 2 is the coin entry, so the payout lands in the coin currency.
    if item_id == BLOODPACT_COST_ITEM:
        state["currency"][str(CURRENCY_COIN)] = \
            int(state["currency"].get(str(CURRENCY_COIN), 0)) + gained
    else:
        grant_item(state, item_id, gained)
    return [[item_id, gained]], removed


# ---- equipment padlock (Backpack 105 EquipLock -> 106) ----------------------
#
# `SendEquipLockReq(equip_uid, toLock)` (0x18E8CC8) sends cmd 105 with
# `intargs=[toLock], strargs=[equip_uid]`. The caller computes the new state itself --
# `UIRuneInventory.OnClickLockBtn` reads `attr["l"]` and sends `1 - it` (defaulting to 1
# when the key is absent) -- so the request is absolute, not a toggle.
#
# **The lock flag lives in `attr["l"]`** (a single character; it is the stray 'l' that
# shows up when scanning PanelSoulFrag.SetPanelDirty for attr keys).
#
# `HandleEquipLockReply` (0x18EB45C) does NOT write that flag. It only:
#   * bails silently unless `AllEquipDic` already knows the uid,
#   * maps cbpType -> BackpackType with the usual bitmask (2->4, 3->1, 4->8, else 2),
#   * nudges `_lockCount[bpType]` by +1/-1 -- which `GetBpFixCount` subtracts from
#     quantity, so a lock reduces the *usable* count, and
#   * raises BackpackEvent 8.
# So the persisted flag has to arrive separately, via a cmd-145 push of the storage.
EQUIP_LOCK_ATTR = "l"
EQUIP_LOCK_STORAGES = (BP_STORAGE_EQUIPMENT, BP_STORAGE_SOULFRAG,
                       BP_STORAGE_BLOODPACT)


def set_equip_lock(state, uid, to_lock):
    """Lock/unlock any equipment-family item. -> (storage, entry)."""
    for storage in EQUIP_LOCK_STORAGES:
        for entry in state["backpack"].get(str(storage), {}).values():
            if entry.get("uid") == uid:
                entry.setdefault("attr", {})[EQUIP_LOCK_ATTR] = \
                    1 if int(to_lock) else 0
                return storage, entry
    raise LookupError(f"no equipment {uid!r} in storages {EQUIP_LOCK_STORAGES}")


def find_soulmirror(state, uid):
    """-> (slot key, entry) for a Soulmirror uid, or (None, None)."""
    for sid, entry in state["backpack"].get(str(BP_STORAGE_SOULFRAG), {}).items():
        if entry.get("uid") == uid:
            return sid, entry
    return None, None


def upgrade_soulmirror(state, uid, levels):
    """Apply an EnhanceSoulFrag request. -> (entry, gained, coins, essence).

    Charges the same coin and Soul Essence the client priced the click at (see the
    cost-table notes). Raises LookupError for an unknown mirror and ValueError when it
    is maxed or unaffordable.
    """
    _, entry = find_soulmirror(state, uid)
    if entry is None:
        raise LookupError(f"no Soulmirror {uid!r} in storage {BP_STORAGE_SOULFRAG}")
    row = bt.dd.row("item", int(entry["iid"])) or {}
    rarity = int(row.get("_param2") or 1)
    action = int(row.get("_action") or 0)
    sf_type = (1 if 101 <= action <= 103 else
               2 if 104 <= action <= 106 else
               3 if 107 <= action <= 109 else 0)
    if not sf_type:
        raise ValueError(f"item {entry['iid']} is not a Soulmirror")
    attr = entry.setdefault("attr", {})
    before = int(attr.get(RUNE_ATTR_LEVEL, 0))
    to_lv = min(before + max(0, int(levels)), SOULFRAG_MAX_LEVEL)
    if to_lv <= before:
        raise ValueError(f"Soulmirror {uid} is already at level {before}")

    if sf_type == 3:
        coins, mats = soulfrag_ultra_cost(before, to_lv)
    else:
        coins = soulfrag_enhance_coin_cost(rarity, to_lv - before)
        mats = {SOULFRAG_ENHANCE_MATERIAL:
                soulfrag_material_cost(rarity, sf_type, before, to_lv)}
    have_coin = int(state["currency"].get(str(CURRENCY_COIN), 0))
    if have_coin < coins:
        raise ValueError(f"upgrade costs {coins} coins, holding {have_coin}")
    for iid, need in mats.items():
        have_mat = item_count(state, iid)
        if have_mat < need:
            raise ValueError(f"upgrade needs {need} of item {iid}, holding {have_mat}")

    state["currency"][str(CURRENCY_COIN)] = have_coin - coins
    for iid, need in mats.items():
        if need:
            spend_item(state, iid, need)
    attr[RUNE_ATTR_LEVEL] = to_lv
    return entry, to_lv - before, coins, sum(mats.values())


def _soulmirror_tier(entry):
    """-> (rarity, sfType) for a stored mirror. sfType 0 means "not a Soulmirror"."""
    row = bt.dd.row("item", int(entry["iid"])) or {}
    action = int(row.get("_action") or 0)
    return (int(row.get("_param2") or 1),
            1 if 101 <= action <= 103 else
            2 if 104 <= action <= 106 else
            3 if 107 <= action <= 109 else 0)


def soulmirror_refund(rarity, sf_type, lv):
    """What one mirror gives back. -> (item id, amount).

    `Formula.GetDustCountDecomposeSoulFrag` (0x18F7770):

        base  = (int)(Decimal(10) * Decimal(2**(rarity-1)))
        spent = sum(GetSoulFragDustCost(rarity, l, charId, sfType) for l in 0..lv-1)
        return (int)(spent * 0.8 + base)      -- spent term is 0 when lv < 1

    So it is a flat base plus **80% of the material actually sunk into levelling it**,
    truncated once at the end rather than per level. The same helpers that price the
    upgrade price the refund, so the two can never drift apart.

    The client's other half, `GetItemCountDecomposeSoulFrag` (0x18F78EC), adds a second
    payout read from `PlayerGeneral`'s decompose dictionary plus 80% of
    `GetRangeSoulFragItemCost`. **Both of those are empty in our game-rule sync** -- for
    rarity <= 4 the upgrade spends no items at all, only dust and coin -- so that half
    evaluates to nothing and the popup we produce matches what the panel previewed.
    Populating the decompose dictionary later means teaching this function about it too.
    """
    base = 10 * (2 ** (max(1, int(rarity)) - 1))
    if sf_type == 3:
        # Apoc. mirrors are priced from formula.json and eat a different material, so
        # refund the one that was actually spent -- see soulfrag_ultra_cost.
        _, mats = soulfrag_ultra_cost(0, lv)
        item = SOULFRAG_ULTRA_MATERIAL
        spent = mats.get(item, 0)
    else:
        item = SOULFRAG_ENHANCE_MATERIAL
        spent = soulfrag_material_cost(rarity, sf_type, 0, lv)
    return item, int(spent * 0.8 + base)


def dismantle_soulmirrors(state, uids):
    """Dismantle Soulmirrors for their material. -> ([[item id, amount], ...], [sid]).

    `SendDecomposeSoulFragReq` (0x18E9100) sends cmd 119 with `strargs` = the uids and
    **no intargs**, and opens a `PanelWaitingBlock` first -- so an unanswered request is
    not a no-op, it is a hard freeze with a modal blocker over the whole UI and no way
    out but restarting the app. That is exactly the symptom this fixes.

    Refuses the whole batch rather than silently skipping, because a partial dismantle
    would leave the client's selection and our storage disagreeing about what still
    exists. A locked mirror is refused too: the panel's AUTO SELECT already excludes
    them, so a locked uid arriving here means the request did not come from that path.
    """
    per_item = {}
    removed = []
    seen = set()
    for uid in uids:
        if uid in seen:
            raise ValueError(f"Soulmirror {uid!r} listed twice")
        seen.add(uid)
        sid, entry = find_soulmirror(state, uid)
        if entry is None:
            raise LookupError(f"no Soulmirror {uid!r} in storage {BP_STORAGE_SOULFRAG}")
        attr = entry.get("attr", {})
        if int(attr.get(EQUIP_LOCK_ATTR, 0)):
            raise ValueError(f"Soulmirror {uid!r} is locked")
        rarity, sf_type = _soulmirror_tier(entry)
        if not sf_type:
            raise ValueError(f"item {entry['iid']} is not a Soulmirror")
        item, amount = soulmirror_refund(rarity, sf_type,
                                         int(attr.get(RUNE_ATTR_LEVEL, 0)))
        per_item[item] = per_item.get(item, 0) + amount
        # A worn mirror has to come off the cast as well, or its equips slot keeps
        # pointing at a uid that no longer exists.
        for _u, other in state["roster"].items():
            cur = char_equips(other)
            if uid in cur:
                other["equips_list"] = [("" if u == uid else u) for u in cur]
        del state["backpack"][str(BP_STORAGE_SOULFRAG)][sid]
        removed.append(sid)
    for item, amount in per_item.items():
        if amount:
            grant_item(state, item, amount)
    return [[item, amount] for item, amount in per_item.items()], removed


# ---- fuse / transmute (Backpack 117 -> 118) --------------------------------
#
# Rarity 4 is the ONLY fusable rarity: `DesignTransmuteForm.CheckSoulfragTransmuteLegal`
# (0x1719BD0) walks `SoulfragTransmuteDic[sfType][rarity][charId][action]`, and the
# design form only carries `_param1` (rarity) == 4 -- 336 rows for sfType 1 and 336 for
# sfType 2, i.e. 112 characters x 3 slots each. Apoc. (sfType 3) cannot be fused at all.
SOULFRAG_TRANSMUTE_RARITY = 4
# **Read out of the panel prefab, not the binary.** `_uiSoulFragTransmuteIconList` in
# PanelSoulFrag (ngui_prefabs_panels_inventory) is a list of exactly 3 icons, and
# InitTransmuteInfo only fills slots that exist, so 3 is the hard ceiling on a fuse --
# see the memory note about reading [SerializeField] values from the bundle.
SOULFRAG_TRANSMUTE_NUM = 3


def soulfrag_transmute_cost():
    """Coin cost of one fuse, shown by InitTransmuteInfo as "-<cost>".

    Ours to author (it rides in the game-rule sync, not any design file). Taken off the
    same curve `soulfrag_enhance_coin_cost` uses at the fusable rarity, so it stays in
    proportion with everything else the tier costs instead of being an invented number.
    """
    return SOULFRAG_ENHANCE_COIN_BASE * (2 ** (SOULFRAG_TRANSMUTE_RARITY - 1))


_transmute_pool_cache = None


def transmute_pool():
    """-> {sfType: {charId: {action, ...}}} of everything legally fusable.

    Straight out of the `transmute` design form, whose rows are
    `_type`=sfType, `_param1`=rarity, `_param2`=charId, `_param3`=action -- exactly the
    four keys CheckSoulfragTransmuteLegal indexes with. Types 11/12 belong to a
    different transmute family (their actions are 4/5/6, not 101..106) and are skipped.
    """
    global _transmute_pool_cache
    if _transmute_pool_cache is None:
        pool = {}
        for r in (bt.dd.rows("transmute") or {}).values():
            if not isinstance(r, dict):
                continue
            sf_type, rarity = r.get("_type"), r.get("_param1")
            if sf_type not in (1, 2) or rarity != SOULFRAG_TRANSMUTE_RARITY:
                continue
            pool.setdefault(sf_type, {}).setdefault(int(r["_param2"]), set()).add(
                int(r["_param3"]))
        _transmute_pool_cache = pool
    return _transmute_pool_cache


def soulmirror_items_for(rarity, char_id, action):
    """-> the item ids of every Soulmirror matching that cell, newest-agnostic.

    Several distinct mirrors can share (rarity, char, slot) -- they differ only by
    `_param1`, the equipment_bonus group that decides which stats they roll -- so this
    returns all of them and the caller picks. Ones `make_soulmirror` would reject (no
    usable bonus rows) are filtered out here rather than blowing up at grant time.
    """
    out = []
    for iid, row in (bt.dd.rows("item") or {}).items():
        if not isinstance(row, dict):
            continue
        if (int(row.get("_action") or 0) == action
                and int(row.get("_param3") or 0) == char_id
                and int(row.get("_param2") or 0) == rarity):
            try:
                make_soulmirror(None, int(iid))
            except ValueError:
                continue
            out.append(int(iid))
    return sorted(out)


def fuse_soulmirrors(state, uids, rng=None):
    """Fuse Soulmirrors into one. -> (new entry, [consumed sid], coins spent).

    The OUTCOME RULE is read off `PanelSoulFrag.GetTransmutePredictText` (0x163F248),
    which is what the player is shown before confirming and therefore what the server
    has to honour. It tallies the selection by charId and by action and picks its
    wording from how uniform they are:

      * all three share a charId -> the text is String.Format'd with that character's
        Title and Name, i.e. **the output is that character's mirror**;
      * charIds differ -> the text names nobody, i.e. **a random character**;
      * likewise for action: all three share one -> that slot; otherwise a random slot.

    (sfType picks which block of text ids is used -- 121900+action / 122008 for Break,
    +7/+11 and 122018/122014 for EX Break -- but the uniformity logic is identical, and
    the two tiers never mix because a mirror's action determines its tier.)

    So a uniform selection is deterministic and a mixed one is a gamble, which is the
    whole point of the feature. Everything else is validation.
    """
    import random as _r
    rng = rng or _r
    if len(uids) != SOULFRAG_TRANSMUTE_NUM:
        raise ValueError(f"a fuse takes exactly {SOULFRAG_TRANSMUTE_NUM} Soulmirrors, "
                         f"got {len(uids)}")
    if len(set(uids)) != len(uids):
        raise ValueError("the same Soulmirror was listed more than once")

    pool = transmute_pool()
    picked = []
    sf_types, char_ids, actions = set(), set(), set()
    for mirror_uid in uids:
        sid, entry = find_soulmirror(state, mirror_uid)
        if entry is None:
            raise LookupError(f"no Soulmirror {mirror_uid!r} in storage {BP_STORAGE_SOULFRAG}")
        if int(entry.get("attr", {}).get(EQUIP_LOCK_ATTR, 0)):
            raise ValueError(f"Soulmirror {mirror_uid!r} is locked")
        row = bt.dd.row("item", int(entry["iid"])) or {}
        rarity, action = int(row.get("_param2") or 0), int(row.get("_action") or 0)
        char_id = int(row.get("_param3") or 0)
        _, sf_type = _soulmirror_tier(entry)
        if action not in pool.get(sf_type, {}).get(char_id, ()) \
                or rarity != SOULFRAG_TRANSMUTE_RARITY:
            raise ValueError(f"Soulmirror {mirror_uid!r} (item {entry['iid']}, rarity "
                             f"{rarity}) is not fusable")
        picked.append((sid, entry, mirror_uid))
        sf_types.add(sf_type)
        char_ids.add(char_id)
        actions.add(action)
    if len(sf_types) != 1:
        raise ValueError(f"a fuse cannot mix tiers (got sfTypes {sorted(sf_types)})")
    sf_type = sf_types.pop()

    coins = soulfrag_transmute_cost()
    have = int(state["currency"].get(str(CURRENCY_COIN), 0))
    if have < coins:
        raise ValueError(f"a fuse costs {coins} coins, holding {have}")

    # Uniform -> keep it; mixed -> roll. The action is drawn from the CHOSEN character's
    # own legal set, so a random pick can never produce a combination the design form
    # does not list.
    tier = pool[sf_type]
    out_char = next(iter(char_ids)) if len(char_ids) == 1 else rng.choice(sorted(tier))
    legal_actions = sorted(tier[out_char])
    if len(actions) == 1:
        only = next(iter(actions))
        out_action = only if only in tier[out_char] else rng.choice(legal_actions)
    else:
        out_action = rng.choice(legal_actions)
    candidates = soulmirror_items_for(SOULFRAG_TRANSMUTE_RARITY, out_char, out_action)
    if not candidates:
        raise ValueError(f"no Soulmirror item for char {out_char} action {out_action}")
    out_item = rng.choice(candidates)

    # **Grant BEFORE freeing the inputs.** grant_soulmirror allocates `max(sid) + 1`, so
    # removing the inputs first would let the new mirror land on a slot id that is also
    # in `removed` -- and backpacks_all_json writes tombstones AFTER the live entries,
    # so the iid-0 tombstone would overwrite the reward and the fuse would look like it
    # consumed three mirrors and produced nothing. Allocating first makes the collision
    # impossible rather than merely unlikely.
    new = grant_soulmirror(state, out_item, rng=rng)
    removed = []
    for sid, _entry, mirror_uid in picked:
        for _u, other in state["roster"].items():
            cur = char_equips(other)
            if mirror_uid in cur:
                other["equips_list"] = [("" if u == mirror_uid else u) for u in cur]
        del state["backpack"][str(BP_STORAGE_SOULFRAG)][sid]
        removed.append(sid)
    assert str(new["sid"]) not in {str(s) for s in removed}
    state["currency"][str(CURRENCY_COIN)] = have - coins
    return new, removed, coins


def roll_rune(state, item_id, slot, level=0, enhance=0, rng=None, reserved=()):
    """Roll a starshard WITHOUT storing it -> an entry with its sid/uid reserved.

    The Starshard Temple shows the player two candidates and keeps only the one they
    pick, so the attributes have to be rolled up front (the panel prints them) while the
    grant waits on the selection. Rolling again at selection time would hand over a
    different shard from the one on screen.

    **`reserved` must carry the candidates already rolled in this batch.** Nothing is in
    the bag yet, so without it every candidate reserves the SAME sid and uid -- and the
    client keys BackpackItemData by those, so a panel showing two of them is showing one
    id twice.
    """
    bag = state["backpack"].setdefault(str(BP_STORAGE_EQUIPMENT), {})
    taken_sids = {int(k) for k in bag} | {int(e["sid"]) for e in reserved if e.get("sid")}
    taken_uids = {e.get("uid") for e in bag.values()} | {e.get("uid") for e in reserved}
    sid = max(taken_sids, default=0) + 1
    n = len(bag) + len(reserved) + 1
    uid = rune_uid(state, n)
    while uid in taken_uids:
        n += 1
        uid = rune_uid(state, n)
    entry = make_rune(state, item_id, slot, level, enhance, rng)
    entry["sid"] = sid
    entry["uid"] = uid
    return entry


def store_rune(state, entry):
    """Commit a rolled starshard into storage 2, re-deriving sid/uid if they were taken
    while the player was choosing. -> the stored entry."""
    bag = state["backpack"].setdefault(str(BP_STORAGE_EQUIPMENT), {})
    if str(entry.get("sid")) in bag:
        entry["sid"] = max((int(k) for k in bag), default=0) + 1
    n = len(bag) + 1
    while any(e.get("uid") == entry.get("uid") for e in bag.values()):
        entry["uid"] = rune_uid(state, n)
        n += 1
    bag[str(entry["sid"])] = entry
    return entry


def grant_rune(state, item_id, slot, level=0, enhance=0, rng=None):
    """Roll a starshard and put it straight into storage 2. -> the stored entry."""
    return store_rune(state, roll_rune(state, item_id, slot, level, enhance, rng))


def backpack_json(state, cbp_type):
    return json.dumps({"sid": state["backpack"].get(str(cbp_type), {})},
                      separators=(",", ":"))


# cmd 145 (PlayerBackpack.HandleBackpackChagne) is the ONLY thing that dispatches
# BackpackEvent **1**, which is what an already-open panel listens on -- UICharacterRoom
# subscribes to it in InitListener and refreshes via ClearSendGiftData +
# MarkCharRoomDirty. The full sub-backpack sync (cmd 84 / HandleSyncSubBackpackRply)
# dispatches event **4** instead, so it updates the data but leaves an open panel
# showing stale item counts until it is reopened.
#
# Its strargs[0] is a `BackpacksData`, i.e. one level deeper than cmd 84's
# `BackpackItemsData`:
#   BackpacksData     { Dictionary<int, BackpackItemsData> backpacksData }   <- key "?"
#   BackpackItemsData { Dictionary<int, BackpackItemData>  backpackItemData } <- key "sid"
#   BackpackItemData  { sid, iid, amount, uid, attr }                         <- own names
#
# The outer JsonProperty name IS recoverable -- just not from the 2.2.7 dump, which
# never shows attribute arguments. `tools/json_keys.py --class BackpacksData` against
# the 2.2.4 binary gives it as **`backpack_type`**, and none of the four spellings this
# used to shotgun ("backpacksData"/"bid"/"sid"/"bpid") was right. That is precisely the
# `event execution error, message=Object reference not set` noted before: with no key
# bound, `backpacksData` deserialized to null and HandleBackpackChagne dereferenced it.
# Full verified shape:
#   BackpacksData     { "backpack_type": {cbpType: BackpackItemsData} }
#   BackpackItemsData { "sid":           {slotId:  BackpackItemData}  }
#   BackpackItemData  { "sid", "iid", "amount", "uid", "attr" }
#
# strargs[1] is optional (HandleBackpackChagne passes null when strargs.Count < 2) and
# goes straight to SetBackpackInfo, so send the same payload as cmd 83 to keep the
# per-BackpackType counters in step with the change.
_BACKPACKS_DATA_KEY = "backpack_type"


def backpacks_all_json(state, storages=None, removed=None, only=None):
    """strargs[0] for cmd 145 -- BackpacksData over `storages` (default: all).

    **cmd 145 MERGES; it never deletes by omission.** `HandleBackpackChagne` does
    `set_Item(slotId, ...)` per entry, so a slot left out of the payload simply keeps its
    old value -- which is why dismantling appeared to work (the counter, driven by the
    separate cmd-83 info payload, went down) while the icons stayed on screen.
    Deletion has an explicit protocol: `ChangeEquip` (0x18ED150) branches on the built
    ItemStruct's `_id`, and routes to `RemoveEquipment` when it is **0**. So a removed
    slot must be sent back as a TOMBSTONE with `iid: 0`.

    `attr` must still be present -- HandleBackpackChagne assigns `_attr` and throws a
    NullReference if it is null -- but `amount: 0` keeps it out of RefreshEquipAttribute.

    `removed` is `{storage int: [sid, ...]}`.

    `only` is `{storage int: [sid, ...]}` -- emit just those LIVE slots of that storage
    instead of all of them. Since 145 merges, resending untouched slots is pure work for
    the client: every entry of an equipment storage costs a `ChangeEquip`, which does a
    `FindIndex` over `_equipList` and then `SetEquipment`. Deleting one Soulmirror out of
    26 meant 26 of those, 25 of which changed nothing. Narrow the push and the client
    touches only what actually moved.
    """
    inner = {}
    for sid, items in state["backpack"].items():
        if storages is not None and int(sid) not in storages:
            continue
        keep = (only or {}).get(int(sid))
        inner[sid] = {"sid": dict(items) if keep is None else
                      {k: v for k, v in items.items() if k in {str(s) for s in keep}}}
    for storage, sids in (removed or {}).items():
        bag = inner.setdefault(str(storage), {"sid": {}})["sid"]
        for sid in sids:
            bag[str(sid)] = {"sid": int(sid), "iid": 0, "amount": 0,
                             "uid": "", "attr": {}}
    return json.dumps({_BACKPACKS_DATA_KEY: inner}, separators=(",", ":"))


# PlayerBackpack.SetBackpackInfo parses the cmd-83 payload as List<List<int>> and
# stores entry i under dictionary key **i + 1**, so the list is positional and its
# length decides which BackpackTypes exist. Each entry is
# [capacity, quantity, buyCapacityLimit, buyCapacity] (BackpackInfo's field order).
#
# Sending "[]" left that dictionary empty, and CommonUtil.CheckAndShowReadyGoMsg
# indexes it at BackpackType SoulFrag(1), Equipment(4) and Bloodpact(8)
# *unconditionally* -- before the checkRune guard -- so the Go button on the battle
# preparation panel threw KeyNotFoundException and silently did nothing. Eight
# entries cover keys 1..8, which is every type in the enum (1/2/4/8 are the real
# ones, the gaps are inert padding).
BACKPACK_INFO_SLOTS = 8
BACKPACK_CAPACITY = 999

# **The info list is indexed by `BackpackType`, which is a BITMASK -- not by the
# ClientBackpackType the storages themselves use.** `SetBackpackInfo` (0x18ECC58) walks
# the outer list and stores each row at `_backpackInfo[i + 1]`, so a row's LIST POSITION
# is its key; `GetBpFixCount` (0x18E7DF0) then reads `_backpackInfo[bpType] - lockCount`
# with bpType straight off the enum:
#     BackpackType: SoulFrag=1, Storage=2, Equipment=4, Bloodpact=8
#     ClientBackpackType (the storage/sync ids): Normal=1, Equipment=2, Soulfrag=3,
#                                                Bloodpact=4
# So equipment belongs at list index 3, not index 1. Publishing them in storage order
# put the starshard count under key 2 while the Starshards panel asked for key 4 -- and
# key 4 held the (empty) bloodpact bag, which is exactly the `Inventory 0/999` the panel
# showed while the shard itself rendered fine in the grid. Indices 2/4/5/6 are inert
# padding for the gaps in the bitmask.
BP_TYPE_TO_STORAGE = {
    1: 3,   # SoulFrag  <- StorageSoulfrag
    2: 1,   # Storage   <- StorageNormal
    4: 2,   # Equipment <- StorageEquipment (starshards)
    8: 4,   # Bloodpact <- StorageBloodpact
}


def backpack_info_json(state):
    """The cmd-83 backpack-infos payload, one row per BackpackType key 1..8.

    Each row is `RpcBackpackInfoPattern` order: capacity, quantity, buyCapacityLimit,
    buyCapacity. The Go check is `GetBpFixCount(type) >= capacity`, so the capacity has
    to exceed the number of items actually held or the client reports the bag as full
    and refuses to start."""
    rows = []
    for key in range(1, BACKPACK_INFO_SLOTS + 1):
        storage = BP_TYPE_TO_STORAGE.get(key)
        held = len(state["backpack"].get(str(storage), {})) if storage else 0
        rows.append([BACKPACK_CAPACITY, held, 0, 0])
    return json.dumps(rows, separators=(",", ":"))


# ---- Soulmirror upgrade tables (cmd 514) -----------------------------------
#
# **The Soulmirror upgrade cost lives in the GAME-RULE SYNC, not in a design file.**
# `Formula.GetSoulFragItemCost` (0x18F71CC) reads
#     PlayerGeneral.SoulfragEnhanceItemDic[rarity][sfType][nowlv]  ->  [[itemId, count]]
# and `GetSoulFragCoinCost` (0x18F6B80) reads the magnification lists. We were sending
# every one of those empty, which is exactly why the upgrade panel showed a blank Target
# Value stepper and a blank Cost: with no row for the level, the client has nothing to
# price and nothing to step to. The `Owned 0` beside it is a genuinely empty bag.
#
# `sfType` = `PlayerBackpack.GetSoulfragTypeByAction` (0x18E85D8): actions 101-103 -> 1
# (Limit / "Break"), 104-106 -> 2 (Super / "EX Break"), 107-109 -> 3 (Ultra / "Apoc.").
# **sfType 3 is priced from `DesignFormulaForm` instead** of these tables, so the Apoc.
# tier is unaffected by anything here.
#
# `rarity` is the mirror's own grade -- item `_param2`, 1..5 (普通/優良/稀有/史詩/傳說).
#
# The list length per [rarity][sfType] IS the level cap: GetSoulFragItemCost returns an
# empty cost when `nowlv >= Count`, so 15 rows allow levels 0->15
# (EquipDefine.SoulFragMaxLevel).
#
# Coin cost, from GetSoulFragCoinCost's non-formula path:
#     coin = 10000 * 2**(rarity-1) * coin_mag[rarity-1] * charRarityMult
# where charRarityMult is 1.0 except for rarity-5 mirrors on a cast of rarity < 6, which
# take `soulfrag_enhance_coin_char_rarity_magnification[charRarity-1]`. Note it does NOT
# scale with the mirror's current level.
#
# **THESE NUMBERS ARE AUTHORED, NOT RECOVERED.** The real tables were live-ops data and
# are not in the client pack -- same situation as the gacha/roulette/box drop tables. The
# SHAPE is exact (read off the two formula functions above); the values are a sane curve
# chosen so the system functions and stays affordable. The Soulmirror material is item
# **30** 靈魂記憶精華 "Soul Essence", whose `_note1_en` is literally "Required material
# for upgrading Soulmirrors."
SOULFRAG_ENHANCE_MATERIAL = 30
SOULFRAG_RARITIES = (1, 2, 3, 4, 5)
SOULFRAG_TYPES = (1, 2, 3)
SOULFRAG_ENHANCE_COIN_BASE = 10000       # the literal in GetSoulFragCoinCost


def _soulfrag_material_count(rarity, sf_type, lv):
    """Soul Essence to take a mirror from `lv` to `lv + 1`.

    **This is the real formula, not an authored one.** `UpdateEnhanceInfo` prices the
    material line with `GetRangeSoulFragDustCost` for every mirror of rarity <= 4 (and
    for rarity 5 it adds the same dust on top of the LR materials), counting it against
    `EquipDefine.SoulFragEnhanceMaterialId` -- so "dust" IS the Soul Essence. From
    `Formula.GetSoulFragDustCost` (0x18F6E98):

        dust(lv) = ((20*lv - 20) * ((lv + 1) / 3) + 10) * 2**(rarity-1) * dust_mag * mult

    with C# integer division on `(lv + 1) / 3`. Verified against the client's own
    display: a ★4 tier-1 mirror summed over lv 0..14 gives 6250 * 8 = **50000**, exactly
    the figure the upgrade panel showed for a +15.
    """
    base = (20 * lv - 20) * ((lv + 1) // 3) + 10
    return base * (2 ** (max(1, int(rarity)) - 1))


def soulfrag_enhance_item_dic():
    """[rarity][sfType][lv] -> [[itemId, count], ...].

    **This table only feeds the ★5 (LR) path.** `UpdateEnhanceInfo` takes the
    `GetRangeSoulFragItemCost` + `GetLRMaterialList` branch only when the mirror's
    `_param2 > 4`; every lower grade is priced purely by dust + coin, so these lists are
    empty -- no LR material is invented, and a ★5 upgrade costs the same dust and coin
    as any other.

    The rows still have to EXIST: `GetSoulFragItemCost` (0x18F71CC) returns an empty
    cost when `nowlv >= Count` but throws ArgumentOutOfRange on `Count <= nowlv` after
    that guard, so the list is sized MAX + 1 to stay indexable at the cap.
    """
    return {
        str(rarity): {
            str(sf): [[] for _ in range(SOULFRAG_MAX_LEVEL + 1)]
            for sf in SOULFRAG_TYPES
        }
        for rarity in SOULFRAG_RARITIES
    }


def soulfrag_enhance_show_item_dic():
    """[rarity][sfType] -> [itemId]; the material icon above the Cost row."""
    return {str(rarity): {str(sf): [SOULFRAG_ENHANCE_MATERIAL]
                          for sf in SOULFRAG_TYPES}
            for rarity in SOULFRAG_RARITIES}


def soulfrag_enhance_coin_cost(rarity, levels=1):
    """Mirror the client's own GetRangeSoulFragCoinCost for a `levels`-step upgrade.

    Only the plain path is modelled: our magnifications are all 1 and the char-rarity
    branch needs a rarity-5 mirror, so it collapses to base * 2**(rarity-1) per level.
    """
    return SOULFRAG_ENHANCE_COIN_BASE * (2 ** (max(1, int(rarity)) - 1)) * int(levels)


def soulfrag_material_cost(rarity, sf_type, before_lv, to_lv):
    """Total Soul Essence for a range upgrade."""
    return sum(_soulfrag_material_count(rarity, sf_type, lv)
               for lv in range(before_lv, to_lv))


# ---- the Apoc. (Ultra, sfType 3) tier prices from formula.json instead --------
#
# `GetSoulFragCoinCost` / `GetSoulFragDustCost` / `GetSoulFragItemCost` all divert to
# `DesignFormulaForm.GetSoulFragEnhance{Coin,Dust,Item}` when sfType == 3, so NONE of the
# game-rule tables apply to Apoc. mirrors. `formulaDic` is
# `{type: {param1: [[...], ...]}}`, and the item function (0x19CE9A0) reads:
#     base        = formulaDic[21][nowLv + 1]     -- per level, carries the item id
#     rarityMul   = formulaDic[22][rarity]
#     charRarMul  = formulaDic[23][charRarity]    (falls back to key 1)
# with coin using 1/2/3 and dust 11/12/13 the same way.
#
# **Only the BASE tables are modelled here.** The two multiplier groups combine their
# rows as a numerator/denominator pair whose element order I have not pinned down, so
# applying them would be guesswork -- and this tier's material is a different item, so
# getting it wrong is the same class of bug as the dust/item mix-up. The base gives the
# right item and the right order of magnitude; expect the client's displayed cost to
# differ if either multiplier is not 1.
#
# Apoc. material is item **39011** 天啓蘊魂水晶 "Apocalypse Crystal", not Soul Essence.
FORMULA_TYPE_COIN_BASE = 1
FORMULA_TYPE_ITEM_BASE = 21
SOULFRAG_ULTRA_MATERIAL = 39011


def _formula_rows(ftype, param1):
    """The formula.json rows for one (type, param1) cell."""
    return [r for r in (bt.dd.rows("formula") or {}).values()
            if isinstance(r, dict) and r.get("_type") == ftype
            and r.get("_param1") == param1]


def soulfrag_ultra_cost(before_lv, to_lv):
    """-> (coins, {item id: count}) for an Apoc.-tier range upgrade (base tables)."""
    coins = 0
    items = {}
    for lv in range(before_lv, to_lv):
        for r in _formula_rows(FORMULA_TYPE_COIN_BASE, lv + 1):
            coins += int(r.get("_param2") or 0)
        for r in _formula_rows(FORMULA_TYPE_ITEM_BASE, lv + 1):
            iid = int(r.get("_param3") or 0)
            if iid:
                items[iid] = items.get(iid, 0) + int(r.get("_param2") or 0)
    return coins, items


def game_rule_json():
    """strargs[0] of the game-rule sync (cmd 514)."""
    d = {k: [] for k in GAME_RULE_MAGNIFICATION_KEYS}
    d["super_limit_define"] = SUPER_LIMIT_DEFINE
    for k in GAME_RULE_EMPTY_DICTS:
        d[k] = {}
    # All five magnification lists are parsed as decimals; 1 per rarity keeps the
    # arithmetic at the base curve rather than leaving the loops empty.
    for k in ("soulfrag_enhance_coin_magnification",
              "soulfrag_enhance_dust_magnification"):
        d[k] = ["1"] * len(SOULFRAG_RARITIES)
    for k in ("soulfrag_enhance_coin_char_rarity_magnification",
              "soulfrag_enhance_dust_char_rarity_magnification",
              "soulfrag_enhance_item_char_rarity_magnification"):
        d[k] = ["1"] * 6                    # indexed by char rarity - 1
    # INTs here, unlike the string decimals above -- see the note on the constant.
    for k in GAME_RULE_DECOMPOSE_MAGNIFICATION_KEYS:
        d[k] = [1] * 6
    d["soulfrag_enhance_item_dic"] = soulfrag_enhance_item_dic()
    d["soulfrag_enhance_show_item_id_dic"] = soulfrag_enhance_show_item_dic()
    # PlayerGeneral.get_SoulfragTransmuteDefaultNum dereferences this dictionary with no
    # null guard -- leaving it out is a NullReferenceException out of
    # PanelSoulFrag.InitTransmuteInfo the moment the transmute tab opens (seen on device
    # 2026-08-08). One entry per rarity keyed by rarity string.
    # **The client only ever reads key "4".** get_SoulfragTransmuteDefaultNum
    # (0x1902370) indexes this dictionary with the literal string "4" -- rarity 4 being
    # the only rarity the transmute design form carries -- so every other key here is
    # inert padding kept only so the shape stays obvious. The value is how many mirrors
    # a fuse consumes, and InitTransmuteInfo enables Confirm on
    # `_selectSoulfragDataList.size == num`; the panel prefab has exactly
    # SOULFRAG_TRANSMUTE_NUM icon slots, so a larger number here is unreachable and
    # would leave Confirm permanently dead.
    d["soulfrag_transmute_num"] = {str(r): SOULFRAG_TRANSMUTE_NUM
                                   for r in SOULFRAG_RARITIES}
    d["soulfrag_transmute_cost"] = soulfrag_transmute_cost()
    d.update(bloodpact_game_rule())
    return json.dumps(d, separators=(",", ":"))


# ---- bloodpact tables (cmd 514) --------------------------------------------
#
# **A null `BloodpactDecomposeTbl` is a hard NullReferenceException the moment the
# bloodpact inventory opens.** `UIBloodPactInventory.ResetSelectedBloodPactList`
# (0x17957D4) reads `PlayerGeneral+0xD0` (= BloodpactDecomposeTbl) and dereferences it
# with no guard, then walks its `datas` list to build the token display. Sending nothing
# is what produced the `PanelBloodPact.ResetInventorySelectedIndex` NRE seen on device --
# the same failure mode as the soulmirror cost tables.
#
# Shapes recovered with `tools/json_keys.py` on the 2.2.4 binary:
#   Formula2DDatas    {"ver": uint, "formula": [Formula2DItemData]}
#   Formula2DItemData {"id": costItemID, "tbl": int[][]}
#   Formula1DDatas    {"ver": uint, "formula": [Formula1DItemData]}
#   Formula1DItemData {"id": costItemID, "tbl": int[]}
# Wire keys: bloodpact1 = Enhance (2D), bloodpact2 = Decompose (2D),
#            bloodpact3 = Mix (1D).
#
# `formula` must be PRESENT and non-null even when empty: the reader takes `datas` at
# +0x18 and indexes its `_size`, so a missing key nulls the list and crashes one line
# later. An EMPTY list is safe -- the loop exits immediately -- and is the honest answer
# while the cost economics are unknown: no shipped item is a bloodpact enhance material,
# which suggests pacts are fed with other pacts. Fill these in once the panel is
# readable rather than inventing an economy now.
#
# The remaining three are AUTHORED, and are ours to define -- the C# only exposes them
# through Puerts getters (the slot logic lives in the shipped JS, not the binary), and
# cmd 297 carries no slot index, so the server is the authority on where a pact lands.
#   bloodpact_rarity_slot -- indexed by the item's rarity; how many of the three slots
#                            (equips 12..14) that grade may occupy.
#   bloodpact_slot_lv     -- per slot, the cast limit level needed to use it.
#   bloodpact_max_lv      -- enhance cap, kept in line with the other equip systems.
BLOODPACT_MAX_LV = 15
BLOODPACT_SLOTS = 3


# `UIBloodPactEnhance.UpdateCostItems` (0x17905F8) pins the 2D indexing exactly:
#
#     for each entry in BloodpactEnhanceTbl.datas:          # one entry per COST TYPE
#         grade = DesignItemRow + 0x90                      # = the pact's _param2
#         for lv in range(attr["lv"], attr["lv"] + curEnhancedTimes):
#             total += entry.costCntArray[grade - 1][lv]
#         GetCurrencyAmount(entry.costItemID, ...)          # <- CURRENCY, not an item
#
# So `tbl` is `int[grade - 1][level]` and must be at least 5 rows (grades 1..5, with
# SR/UR/LR = 3/4/5) by BLOODPACT_MAX_LV columns, or the panel throws
# ArgumentOutOfRange -- and an EMPTY `formula` throws KeyNotFound out of
# `UIBloodPactEnhanceDirty` on OnEnable, which is what the enhance tab did at first.
# `costItemID` goes to `CommonUtil.GetCurrencyAmount`, but despite that name it is an
# **ITEM id** -- verified on device: an id of 16 rendered as item 16 "Evolution Abyss
# Pass" with the player's owned count (3) beneath it. Item **2** 魔界金幣 "Coin"
# (`_action 5`) is the currency-backed entry and is what the cost rows use here.
#
# **The cost VALUES below are authored** -- the shape is exact (and the panel was seen
# rendering 10000 for LR/grade-5/level-0, confirming the indexing) but the live-ops
# numbers are not in the pack, same as the roulette/gacha tables.
BLOODPACT_GRADES = 5
BLOODPACT_COST_ITEM = 2          # 魔界金幣 "Coin" as a backpack-item id


def _bloodpact_cost_table(base):
    """int[grade-1][level]; grades 1..5 down the rows, levels 0..MAX-1 across."""
    return [[base * grade * (lv + 1) for lv in range(BLOODPACT_MAX_LV)]
            for grade in range(1, BLOODPACT_GRADES + 1)]


def bloodpact_game_rule():
    return {
        # enhance: coin per level, scaled by grade
        "bloodpact1": {"ver": 1, "formula": [
            {"id": BLOODPACT_COST_ITEM, "tbl": _bloodpact_cost_table(2000)}]},
        # decompose: what you get back (same 2D shape). Null here is the NRE in
        # UIBloodPactInventory.ResetSelectedBloodPactList.
        "bloodpact2": {"ver": 1, "formula": [
            {"id": BLOODPACT_COST_ITEM, "tbl": _bloodpact_cost_table(500)}]},
        # mix is 1D: one value per grade.
        "bloodpact3": {"ver": 1, "formula": [
            {"id": BLOODPACT_COST_ITEM,
             "tbl": [5000 * g for g in range(1, BLOODPACT_GRADES + 1)]}]},
        "bloodpact_rarity_slot": [0, 0, 0, 1, 2, 3],
        "bloodpact_slot_lv": [0] * BLOODPACT_SLOTS,
        "bloodpact_max_lv": BLOODPACT_MAX_LV,
    }

"""Roulette: the lobby Spin-To-Win wheel.

Split out of the former monolithic core.py; depends only on .core.
"""


import json

from .core import (
    _daily_period,
    bump_quest_counter,
    grant_reward,
    spend_cost,
)


# ---- roulette (the lobby "Spin-To-Win" wheel) ------------------------------
# Two lobby buttons, ONE wheel. Read out of the 2.2.7 binary 2026-08-06:
#
#   MenuBtnUpdater.UpdateData (0x16F4E1C) special-cases eMenuBtnType.Roulette (14) and
#   calls MenuBtnUpdater.UpdateRoulette(handler._curMenuBtnStateData, handler._handlerID,
#   handler.Param1) -- both of those are prefab-authored [SerializeField]s.
#   UpdateRoulette (0x16F50F8) then does, in order:
#       boxId = handleId + 101                       <- RouletteBoxId.BaseBox
#       info  = PlayerGacha.GetRouletteInfo(boxId)
#       if info == null: return -1                   <- WITHOUT touching stateData
#       if PlayerOFA.GetGroupedEventBanner(Param1) is empty:  isEnabled = 0
#       elif that group has expired and has no permanent entry: isEnabled = 0
#       else: isEnabled = 1; hasNotice/otherInfo/spriteName from the info
#   UIMainMenuBtn.Init (0x16F70F4) grows _menuTypeUnitDic[14]'s handler list to fit
#   whatever _handlerID it finds, so N roulette buttons are by design.
#
# So the lobby prefab ships TWO type-14 buttons, _handlerID 0 and 1 -> boxes 101 and
# 102. Because we answered both syncs with "{}", GetRouletteInfo returned null for both
# (the recurring `boxId=101/102, roulette info is empty` warnings) and UpdateRoulette
# bailed BEFORE the isEnabled=0 line -- so both buttons kept their raw prefab state and
# both rendered. Publishing a non-null info for each is what lets the OFA-group gate run
# and switch the unwanted one off; it is not about which box has content.
#
# They open the same wheel because PanelRoulette.InitRouletteDropInfo (0x159840C) and
# InitRouletteInfo hardcode boxId **101** -- box 102 (ItemCostBox) has no panel of its
# own. Live EoS footage shows one entry, consistent with only one OFA group being live.
#
# Wire shapes:
#   cmd 306 ReceiveSyncRoulette (0x18FFCEC)  strargs[0] -> Dictionary<string,RouletteInfo>
#           keyed by boxId.ToString() (GetRouletteInfo, 0x18FC988, stringifies the id).
#   cmd 305 ReceiveSyncRouletteDrop (0x18FFB04)
#           strargs[0] -> Dictionary<string,List<List<int>>>  wheel slots
#           strargs[1] -> Dictionary<string,List<List<int>>>  bonus slot
#           each inner list is [itemId, count] (InitRouletteDropInfo reads [0] and [1]).
#           The drop list is indexed 1:1 against the panel's _rouletteDropItemList, and
#           the bonus list is indexed at [0] unconditionally -- an empty bonus list
#           throws ArgumentOutOfRange, so it must carry at least one entry.
#
# RouletteInfo has no design form anywhere in the pack (the wheel was live-ops server
# data, like the gacha boxes), so everything below is SYNTHESISED.
# The JsonProperty key names are the field names, UNVERIFIED -- see the 2.2.7 note about
# attribute thunks not carrying their strings. A wrong key only zeroes that field; the
# object is still non-null, so the button gating works either way.
ROULETTE_BOXES = (101, 102)

# The 12 wheel slots, reconstructed from a screenshot of the live "LUCKY ROULETTE"
# panel: diamonds, stamina hearts, coin stacks, scroll cards and summon orbs. Item ids
# from the EN 2.2.7 `item` form -- 1 Diamond, 2 Coin, 5 Stamina, 202/203 Awaker/Sin
# Scroll, 210/211 Minion/Awaker Summon Orb, and **123 "Spin-Dat-Coin"**, which is the
# roulette's own cost currency (hence RouletteBoxId.ItemCostBox for 102).
# Counts are read from the screenshot; the exact per-slot pairing is a best-effort match.
# Slot COUNT matters more than contents: InitRouletteDropInfo walks the list and indexes
# the panel's _rouletteDropItemList 1:1, so more slots than the prefab has throws
# ArgumentOutOfRange. Fewer just leaves trailing icons blank, so err low if unsure.
ROULETTE_SLOTS = [
    [1, 30],        # Diamond x30
    [5, 10],        # Stamina x10
    [1, 50],        # Diamond x50
    [1, 10],        # Diamond x10
    [210, 20],      # ★3 Minion Summon Orb x20
    [2, 20000],     # Coin x20000
    [211, 20],      # ★4 Awaker Summon Orb x20
    [202, 5],       # Awaker Scroll x5
    [2, 50000],     # Coin x50000
    [203, 5],       # Sin Scroll x5
    [2, 5000],      # Coin x5000
    [5, 20],        # Stamina x20
]
# The "BONUS ... x50 / 0/4 Spins Completed!" prize in the panel's top-right corner.
ROULETTE_BONUS = [[210, 50]]


# How many free spins a day box 101 grants, and how many spins earn the bonus.
ROULETTE_DRAW_MAX = 4
# Mission case for "[Daily] Spin the roulette wheel once" (quest 10032).
QUEST_CASE_ROULETTE = 33


def expire_roulette_day(state, now=None):
    """Clear the per-day spin counters when the daily window rolls over.

    `day` was incremented on every spin and never reset, so the free daily spins were
    free ONCE and the wheel then read "no draws left today" forever -- and the
    "[Daily] Spin the roulette wheel once" mission could never be re-armed either.

    Uses `_daily_period`, the SAME 4AM local boundary as the shop resets, the daily
    passes, the free gacha pull and the mission groups: one rollover moment per account,
    not four that drift apart. -> True if anything was cleared.
    """
    period = _daily_period(now)
    if state.get("roulette_day") == period:
        return False
    state["roulette_day"] = period
    changed = False
    for rec in (state.get("roulette") or {}).values():
        if rec.get("day"):
            rec["day"] = 0
            changed = True
    return changed


def roulette_info(state, box_id):
    """One RouletteInfo, keyed for the wire.

    Wire keys read out of the **2.2.4** binary's JsonProperty thunks (see
    tools/read_json_keys.py): the 2.2.7 attribute generators resolve their strings from
    global-metadata at runtime, but 2.2.4 still has plain `adrp`+`add` into a C string,
    and RouletteInfo did not change between the two builds. Validated by reproducing
    OFABannerData's already-known idx/bgt/edt/id/type/strarg/status.
    Note **day_drawsum**, not `day_draw_sum` -- the irregular one, and the reason
    "Spin(s) Left" sat at the full allowance no matter how many spins were used.

    101 is RouletteBoxId.BaseBox -- the FREE daily spin, so it carries no cost item
    (`IsCostItemEnough` then passes trivially and GetSpriteName returns the lit variant).
    102 is ItemCostBox and pays with Spin-Dat-Coin (item 123).

    `draw_max` MUST be > 0: `get_IsLimitDraw` is literally `DrawMax > 0` (0x1901928) and
    `Roulette.UpdateRouletteInfo` (0x1AE5CD0) skips BOTH labels unless it is true -- that
    is what left the bonus counter reading `-/-`. `get_RemainDrawCount` (0x1901938) is
    `DrawMax - DayDrawSum`; the bonus label is `DrawSum/DrawMax`.
    """
    r = (state.get("roulette") or {}).get(str(box_id)) or {}
    cost_id, cost_num = (0, 0) if int(box_id) == 101 else (123, 1)
    return {
        "day_drawsum": int(r.get("day", 0)),
        "drawsum": int(r.get("sum", 0)),
        "delta_time": 0,
        "draw_max": ROULETTE_DRAW_MAX,
        "cost_item_id": cost_id,
        "cost_item_num": cost_num,
    }


def roulette_info_json(state):
    """strargs[0] of cmd 306 -- Dictionary<string, RouletteInfo>.

    Rolls the day over first (expire_roulette_day), so a client that sat open across
    4AM sees its free spins back without a relog.

    Published for BOTH boxes on purpose: 102 needs a non-null info for UpdateRoulette to
    reach its isEnabled=0 line and hide the duplicate lobby button.
    """
    expire_roulette_day(state)
    return json.dumps(
        {str(b): roulette_info(state, b) for b in ROULETTE_BOXES},
        separators=(",", ":"))


def roulette_datas_json():
    """strargs[0] of cmd 305 -- the wheel slots, [itemId, count] per slot."""
    return json.dumps({str(b): ROULETTE_SLOTS for b in ROULETTE_BOXES},
                      separators=(",", ":"))


def roulette_bonus_datas_json():
    """strargs[1] of cmd 305 -- the bonus slot. Index [0] is read unconditionally, so
    this must never be empty or InitRouletteDropInfo throws ArgumentOutOfRange."""
    return json.dumps({str(b): ROULETTE_BONUS for b in ROULETTE_BOXES},
                      separators=(",", ":"))


def roulette_draw(state, box_id):
    """Spin box `box_id`. -> (ok, results, why, buckets)

    `buckets` names every sync the caller must push -- the same contract `claim_mail`
    already has. Ignoring it is what left the two Stamina slots invisible until the
    next login: they route to `energy` and the handler pushed only currency and Normal
    storage. Taking the bucket from the grant rather than from a hardcoded list is the
    point, so a slot added to ROULETTE_SLOTS later cannot reintroduce it.

    `results` is cmd 307's strargs[0]: `List<List<int>>`. `Roulette.OnRouletteDraw`
    (0x1AE6B6C) reads each inner list at **[1] = itemId and [2] = amount** (byte offsets
    +36/+40 off the int[] payload), NOT [0]/[1] like the wheel-slot lists -- there is a
    leading element it never touches, so index 0 is the box id here.

    The landing slot is not sent: the client walks its own `_iiDropItemList` and spins to
    the first icon whose ItemID **and** ItemCount both match the result. So a result must
    reproduce a wheel slot exactly, or the wheel spins to nothing.
    """
    expire_roulette_day(state)
    box = str(int(box_id))
    if box not in (str(b) for b in ROULETTE_BOXES):
        return False, [], f"unknown box {box}", set()
    info = roulette_info(state, box)
    if info["day_drawsum"] >= info["draw_max"]:
        return False, [], "no draws left today", set()
    cost_id, cost_num = info["cost_item_id"], info["cost_item_num"]
    # spend_cost, not spend_item: the cost may be a currency-backed item id.
    if cost_id and cost_num and not spend_cost(state, cost_id, cost_num):
        return False, [], f"cannot pay {cost_num}x item {cost_id}", set()

    import random
    item_id, amount = random.choice(ROULETTE_SLOTS)
    # Collect the BUCKET each grant lands in. The wheel pays four different routings --
    # Diamond/Coin are `_action 5` currency, Stamina is `_action 6` energy, Scrolls are
    # `_action 0` backpack, the Summon Orbs are `_action 2` -- and the caller has to
    # push the matching sync or the winnings exist only in the save file.
    buckets = {grant_reward(state, item_id, amount)}

    r = state.setdefault("roulette", {}).setdefault(box, {})
    r["day"] = int(r.get("day", 0)) + 1
    # "[Daily] Spin the roulette wheel once" -- quest case 33.
    bump_quest_counter(state, QUEST_CASE_ROULETTE, 1)
    r["sum"] = int(r.get("sum", 0)) + 1
    # The bonus pays out when the spin counter reaches DrawMax, then the track resets.
    if r["sum"] >= ROULETTE_DRAW_MAX:
        for bid, bamt in ROULETTE_BONUS:
            buckets.add(grant_reward(state, bid, bamt))
        r["sum"] = 0
    # THE 4-SPIN BONUS IS PAID BUT STILL NOT REPORTED, and that is on purpose here.
    # It is a real gap -- `results` is all the reply carries, so a bonus that fires is
    # invisible and the "0/4 Spins Completed!" track resets with nothing shown for it.
    # But it cannot be fixed by appending a row: per the decompile above, the client
    # does not read a landing slot, it walks its own `_iiDropItemList` and spins to the
    # first icon whose ItemID AND ItemCount both match. ROULETTE_BONUS is [210, 50] and
    # no wheel slot is 210x50 -- 210 appears only as x20 -- so that row matches nothing
    # and the wheel spins to nothing, in the one panel where a bad payload is known to
    # hang rather than look wrong. Needs one device check, or its own channel (mail),
    # before it is written. The bucket the bonus lands in IS reported, so the item
    # itself now appears in the bag without a relog.
    return True, [[int(box), int(item_id), int(amount)]], "", buckets

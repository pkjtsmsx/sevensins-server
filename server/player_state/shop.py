"""Shop.

Split out of the former monolithic core.py; depends only on .core.
"""


import json

from .core import (
    grant_reward,
    item_count,
    spend_cost,
)


# ---- shop -----------------------------------------------------------------
# PlayerShop.HandleLoginSync (cmd 515) reads
#   strargs[0] -> List<List<int>>, one row per shop, and
#   strargs[1] -> Dictionary<int, Dictionary<int, Dictionary<int, int>>> (FreeGoodsDic).
# (The two were transposed in the old note; the deserializer generics say otherwise.)
# strargs[1] is REQUIRED -- the handler bounds-checks `_size > 1` before touching it --
# but its deserialize is SKIPPED when the string is exactly "[]".
#
# A shop row is read back through ShopData's accessors, which index the raw list:
#   [0] Id (get_Id), [1] BeginTime, [2] EndTime.
# Everything else (title, subtitle, banner) comes from the DesignShopRow the ctor looks
# up by Id, so three ints is the whole payload.
#
# PlayerShop.GetItemAvailable(begin, end) is `(begin < 1 || now >= begin) &&
# (end < 1 || now < end)`, so [id, 0, 0] means "always open".
#
# Only send ids that exist in DesignShopForm: a missing row sets PlayerShop.AnyRowNull,
# and from then on get_NormalShopList pops a confirm dialog and returns NULL rather than
# the list. DesignShopForm.Type splits the table: 1 = the store tabs (1/2/3/5, the ones
# the client puts in NormalShopList), 7 = the cash gacha shop (99999), 2/3 = the ~2100
# per-goods limited shops that quest `_pre_case 6` looks up through
# GetLimitShopGoodsHasBuy (a dictionary miss there just returns false, so they do not
# need to be synced).
STORE_SHOP_IDS = (1, 2, 3, 5)
SHOP_END_TIME = 2145916800   # 2038-01-01, safely inside int32

# Kept OFF until the goods sync exists, because a NON-EMPTY list is what lets the store
# panel trap the UI (verified on device 2026-08-03):
#   - empty list  -> PanelStore.OnEnterItemList throws ArgumentOutOfRange indexing
#     `NormalShopList[_curShopIndex]`, which happens BEFORE it opens anything modal;
#   - non-empty   -> it gets past that, sets the title, and calls
#     `PanelWaitingBlock.Open(-1.0, 0)` -- an indefinite, input-blocking overlay with no
#     timeout -- then waits on a goods flow we do not serve. The only way out is
#     restarting the client.
# Neither is a working store; the difference is only whether the player gets stuck. The
# missing piece is `PlayerShop.SendSyncShopGoodsCmd`, server cmd **260** with intargs
# [shopID, syncBaughtData], answered by cmd 516 (HandleShopSync) -- see docs §8b.
# ON as of 2026-08-05, but the store is NOT confirmed working -- if it hangs again, set
# this back to False, which keeps the failure to a throw BEFORE the modal so the player
# is not locked out of the client.
#
# Answering the goods sync (260 -> 516) alone was NOT sufficient: with a non-empty shop
# list the client never even sent 260, and no banner card rendered. Those cards ARE
# `_goBannerList`, and the tween that reveals them is the same one whose callback runs
# `AddPanelTransition(75)` -- so "no cards" and "stuck at state 74" are one failure.
# Current theory under test: the third field is ShopData.EndTime and we were sending 0,
# i.e. a window that closed at the epoch, so every shop read as expired. Now sending an
# open window (SHOP_END_TIME).
#
# NOT the cause, ruled out by reading it: IAPManager.UpdateSellList (RVA 0x1C5531C)
# returns true on EVERY path, including a null goods list, so missing Play billing
# cannot stall the coroutine.
#
# The sequence, from PanelStore.coSyncShopGoods (RVA 0x17A200C): at GlobalState 74 it
# opens `PanelWaitingBlock(-1.0, 0)` -- indefinite, no timeout -- and plays a tween over
# `_goBannerList` (group 12) whose callback (0x17A1E64) only calls
# `AddPanelTransition(75)`. The goods sync happens after that transition, so a tween that
# never completes means an overlay that never closes and a client that must be restarted.
#
# The 260 -> 516 handler is already wired and correct.
# PlayerShop.SendSyncShopGoodsCmd is
# server cmd **260** with intargs [shopID, syncBaughtData]; the reply is cmd **516**,
# whose branch in PlayerShop.OnClientCmdReceived (RVA 0x180409C) reads
# intargs[0]=shopID, intargs[1]=syncBought and deserializes strargs[0] into
# `Dictionary<int, GoodsBuyData>` (GoodsBuyData = {Count, Reset}) as the shop's freeList.
# The GOODS themselves come from the design pack (`shop` has 2196 rows: 1 Premium Shop,
# 2 Item Shop, 3 Soul Altar, 5 Dixie's Booth), so the server only owes purchase state --
# an empty dict is a valid "nothing bought yet".
SHOP_GOODS_IMPLEMENTED = True


def shop_json():
    """strargs[0] of the shop login sync: the four store tabs, always open."""
    if not SHOP_GOODS_IMPLEMENTED:
        return "[]"
    # [shopID, BeginTime, EndTime]. These land on ShopData.BeginTime/EndTime, so 0/0
    # reads as a window that closed at the epoch -- an expired shop, which is the most
    # likely reason no banner card rendered even though all four shops were delivered
    # and passed DesignShopForm.HasRow. Give them an open window instead.
    return json.dumps([[sid, 0, SHOP_END_TIME] for sid in STORE_SHOP_IDS],
                      separators=(",", ":"))


# One goods row is a List<int> of 21 fields, index == declaration order on
# PlayerShop.ShopGoodsData (verified: ItemID is [5] at RVA 0x1807FC0, Filter is [9] at
# 0x18080F8):
#   0 Id           1 BeginTime   2 EndTime    3 ResetCycle  4 Limit
#   5 ItemID       6 ItemCount   7 CostID     8 CostCount   9 Filter
#  10 BeginLevel  11 EndLevel   12 preQuest1 13 preQuest2  14 preGoods1
#  15 preGoods2   16 OnceBuyMax 17 sort      18 labelType  19 TipParam1  20 questHint
# Times/levels of 0 mean "no limit" (same convention as GetItemAvailable on the shop).
#
# WHICH goods a shop sells was live-ops server data -- it is not in the design pack, the
# same as stage drops -- so this is OUR content, not a recovered table. An EMPTY list is
# not an option: PanelStore.UpdateCurStoreGoodsDataList NREs on it, which is what froze
# the client after the goods sync started working.
# `Filter` is a TEXT ID used as the tab label, and PanelStore.UpdateStoreFilterIDList
# builds the tab strip from the DISTINCT Filter values of the goods that are currently
# available. All-zero filters therefore collapse the strip to nothing, which is why the
# tabs vanished. Valid ids come from PanelStore.eSpecificStoreFilterType:
FILTER_DIAMONDS = 16014     # "Diamonds"
FILTER_COINS = 16015        # "Coins"
FILTER_ITEMS = 16021        # "Stamina & Tickets"
FILTER_PVP = 16022          # "PVP Shop"
FILTER_SALES = 16033        # "Super Sales"

# `Limit` (index 4) is the purchase cap; 0 renders as "Purchase Cap 0/0" and blocks
# buying. OnceBuyMax (16) caps a single transaction.
GOODS_DEFAULT_LIMIT = 99
GOODS_DEFAULT_ONCE_MAX = 10


def _goods(gid, item_id, item_count, cost_id, cost_count, *, filt=FILTER_ITEMS,
           sort=None, limit=GOODS_DEFAULT_LIMIT):
    return [gid, 0, 0, 0, limit, item_id, item_count, cost_id, cost_count, filt,
            0, 0, 0, 0, 0, 0, GOODS_DEFAULT_ONCE_MAX,
            sort if sort is not None else gid, 0, 0, 0]


DEFAULT_SHOP_GOODS = {
    # 2 = Belphe's Grocery Store (Item Shop): currency and stamina for diamonds.
    "2": [
        _goods(201, 2, 100000, 1, 100, filt=FILTER_COINS),
        _goods(202, 2, 500000, 1, 450, filt=FILTER_COINS),
        _goods(203, 202, 1, 1, 300, filt=FILTER_ITEMS),
        _goods(204, 203, 1, 1, 300, filt=FILTER_ITEMS),
        _goods(205, 18, 1, 1, 50, filt=FILTER_ITEMS),   # Training Gym Pass
        _goods(206, 36, 10, 1, 200, filt=FILTER_SALES), # 10 Soul Gem
    ],
    # 1 = Mammon's Premium Shop
    "1": [
        _goods(101, 36, 10, 1, 200, filt=FILTER_SALES),
        _goods(102, 101, 5, 2, 50000, filt=FILTER_ITEMS),
    ],
    # 3 = Asmodeus's Soul Altar
    "3": [
        _goods(301, 202, 1, 1, 300, filt=FILTER_ITEMS),
        _goods(302, 32, 10, 2, 100000, filt=FILTER_COINS),
    ],
    # 5 = Dixie's Exchange Booth
    "5": [
        _goods(501, 101, 3, 2, 30000, filt=FILTER_COINS),
        _goods(502, 18, 1, 1, 50, filt=FILTER_ITEMS),
    ],
}


def find_shop_goods(state, goods_id):
    """-> (shop_id, row) for a goods id. `SendBuyCmd(goodsID, count)` carries NO shop id,
    and there is a whole GoodsIDToShopID command (261), so goods ids are GLOBALLY unique
    -- ours are numbered shop*100 + n for that reason."""
    for sid in STORE_SHOP_IDS:
        for row in (((state.get("shop_goods") or {}).get(str(sid))
                     or DEFAULT_SHOP_GOODS.get(str(sid)) or [])):
            if row[0] == int(goods_id):
                return sid, row
    return None, None


def buy_shop_goods(state, goods_id, count):
    """Purchase `count` of one goods row. -> (ok, shop_id, reason)."""
    count = max(int(count), 1)
    shop_id, row = find_shop_goods(state, goods_id)
    if not row:
        return False, None, f"unknown goods {goods_id}"
    _gid, _bt, _et, _cycle, limit, item_id, item_cnt, cost_id, cost_cnt = row[:9]
    once_max = row[16]
    if once_max and count > once_max:
        return False, shop_id, f"over per-purchase max {once_max}"

    bought = state.setdefault("shop_bought", {}).setdefault(str(shop_id), {})
    rec = bought.get(str(goods_id)) or {"Count": 0, "Reset": 0}
    if limit and rec["Count"] + count > limit:
        return False, shop_id, f"over purchase cap {limit}"

    if not spend_cost(state, cost_id, cost_cnt * count):
        return False, shop_id, f"cannot pay {cost_cnt * count}x item {cost_id}"
    grant_reward(state, item_id, item_cnt * count)
    rec["Count"] += count
    bought[str(goods_id)] = rec
    return True, shop_id, ""


def goods_reward(state, goods_id, count):
    """-> (item id, total count) a purchase hands over, for reply 513's intargs[2:4].
    Those two drive the client's 'you received' popup."""
    _sid, row = find_shop_goods(state, goods_id)
    if not row:
        return 0, 0
    return row[5], row[6] * max(int(count), 1)


def shop_bought_json(state, shop_id):
    bought = (state.get('shop_bought') or {}).get(str(shop_id)) or {}
    return json.dumps(bought, separators=(',', ':'))


def shop_goods_json(state, shop_id):
    """The THREE strargs of the goods sync (cmd 260 -> 516).

    `DeserializeGoodsAndBoughtData` (RVA 0x1805F18) reads strargs[0] as the bought data
    and then **returns early unless there are at least 2 strargs** -- which is why a
    single-strarg reply left the shop with no goods and the panel spinning. With >= 2 it
    calls `DeserializeShopGoodsData(shop, strargs[1], strargs[2] or null)`
    (RVA 0x1806294), where:
      strargs[0] `Dictionary<int, GoodsBuyData>`  -- {Count, Reset} per goods id, bought
      strargs[1] `List<List<int>>`                -- THE GOODS LIST itself
      strargs[2] `Dictionary<int, string>`        -- per-goods strings, only read on the
                                                    international build (EN is one)
    The goods list is server data, not design data -- which goods a shop sells was
    live-ops. Sending an empty list yields an empty but WORKING shop: the deserialize
    succeeds, the caller dispatches ShopEvent SYNC_SHOP_GOODS(11), and the panel closes
    its overlay instead of hanging.
    """
    bought = (state.get("shop_bought") or {}).get(str(shop_id)) or {}
    goods = ((state.get("shop_goods") or {}).get(str(shop_id))
             or DEFAULT_SHOP_GOODS.get(str(shop_id)) or [])
    return [json.dumps(bought, separators=(",", ":")),
            json.dumps(goods, separators=(",", ":")),
            "{}"]

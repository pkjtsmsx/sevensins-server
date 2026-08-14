"""Shop.

Split out of the former monolithic core.py; depends only on .core.
"""


import json

import battle as bt

from .core import (
    add_char,
    rune_slot,
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
FILTER_PVP = 16022          # "PVP Shop" -- the Medal of Pride tab; our pack has no
                            # "Medal of Pride" label, and this is the same tab by
                            # content (arena currency buying ★5 casts and Trainers).
FILTER_GUILD = 16032        # "Guild Pt"
FILTER_SALES = 16033        # "Super Sales"

# `ResetCycle` (index 3) drives the "Daily/Weekly/Monthly Reset" strip under each card
# (texts 113039/113040/113041). The int->label mapping is not in an enum; 1/2/3 in
# design order is the obvious reading and is what the store footage's counters imply,
# but it is UNVERIFIED -- check the labels in game before trusting them.
RESET_NONE, RESET_DAILY, RESET_WEEKLY, RESET_MONTHLY = 0, 1, 2, 3

# Cost currencies, as the storefront quotes them.
COST_DIAMOND = 1            # CurrencyType.Cash
COST_MEDAL = 9              # "Medal of Pride" -- a plain bag item, not a currency
COST_GUILD_PT = 4           # CurrencyType.Guild

# **Storefront bundles are `_action 2` BOX items with no contents in the pack.** They
# carry the art and the name the footage shows ("Deluxe Coin Box (1,500,000)"), so we
# sell those ids and pay out the amount printed on the card. Coins are item 2 (Mira,
# a currency) and Stamina is item 5 (energy), both of which grant_reward routes.
BUNDLE_PAYOUT = {
    736: (2, 1500000),      # Deluxe Coin Box
    735: (2, 810000),       # Great Coin Piles
    734: (2, 240000),       # Coin Piles
    733: (2, 105000),       # Small Coin Piles
    732: (2, 55000),        # Coin
    731: (2, 15000),        # Coin
    711: (5, 100),          # Stamina (100)
}

# The Guild Pt luckybags are `_action 2` as well, so they hit exactly the same wall --
# unfileable in the bag AND stripped from the reward popup. They are genuine random
# boxes, so instead of a fixed payout each resolves to a real STARSHARD drawn from its
# element at the named grade. Starshard items encode the slot in `_action` (111..116)
# and the grade in `_param2`, so "★4 (UR-LR) Random Slot Endearment" is exactly
# "any _param2 4 Endearment shard", which is what the card promises.
RUNE_BUNDLES = {
    1200021: ("Endearment", 4),
    1200022: ("Chaos", 4),
    1200023: ("Hawkeye", 4),
    1200024: ("Defender", 4),
    1200020: (None, 3),          # ★3 (UR-LR) Random Starshard Luckybag (Slot 6)
}

# "★5 Awaker Summon Orb" -- a box whose card reads "Summon a random awaker of ★5
# rarity or better". Same `_action 2` problem, so it resolves to a real cast: roll one
# of the ★5 casts and report the matching `_action 1` character item, which both
# survives the popup mask and carries the tweenPopup flag that plays the reveal.
CHAR_ORB_BUNDLES = {212: 5}
# The three ★5 casts, by `_alignment`: Sins / Virtues / Riders.
AWAKER_ALIGNMENTS = (100, 101, 102)

_rune_pool_cache = {}
_awaker_pool_cache = {}


def awaker_pool(rarity):
    """[(char id, character item id), ...] for ★`rarity` casts that have an item."""
    if rarity not in _awaker_pool_cache:
        chars = {int(cid) for cid, row in (bt.dd.rows("char") or {}).items()
                 if row.get("_rarity") == rarity
                 and row.get("_alignment") in AWAKER_ALIGNMENTS}
        star = f"\u2605{rarity}"
        out = []
        for iid, row in (bt.dd.rows("item") or {}).items():
            if row.get("_action") != 1:
                continue
            cid = int(row.get("_param1") or 0)
            if cid in chars and (row.get("_itemName") or "").startswith(star):
                out.append((cid, int(iid)))
        _awaker_pool_cache[rarity] = sorted(out)
    return _awaker_pool_cache[rarity]


def rune_bundle_pool(element, grade):
    """Starshard item ids matching an element (or any) at a grade."""
    key = (element, grade)
    if key not in _rune_pool_cache:
        pool = []
        for iid, row in (bt.dd.rows("item") or {}).items():
            if int(row.get("_action") or 0) not in range(111, 117):
                continue
            if int(row.get("_param2") or 0) != grade:
                continue
            if element and element not in (row.get("_itemName_en") or ""):
                continue
            pool.append(int(iid))
        _rune_pool_cache[key] = sorted(pool)
    return _rune_pool_cache[key]

# `Limit` (index 4) is the purchase cap; 0 renders as "Purchase Cap 0/0" and blocks
# buying. OnceBuyMax (16) caps a single transaction.
GOODS_DEFAULT_LIMIT = 99
GOODS_DEFAULT_ONCE_MAX = 10


def _goods(gid, item_id, item_count, cost_id, cost_count, *, filt=FILTER_ITEMS,
           sort=None, limit=GOODS_DEFAULT_LIMIT, reset=RESET_NONE, once_max=None):
    return [gid, 0, 0, reset, limit, item_id, item_count, cost_id, cost_count, filt,
            0, 0, 0, 0, 0, 0,
            once_max if once_max is not None else GOODS_DEFAULT_ONCE_MAX,
            sort if sort is not None else gid, 0, 0, 0]


DEFAULT_SHOP_GOODS = {
    # 2 = Belphe's Grocery Store ("Item Shop"). **Transcribed from footage of the live
    # game**, tab by tab, since the goods list was live-ops data and `shop_goods` in the
    # pack is a 44-byte stub with a null script. Prices, purchase caps and reset cycles
    # are read off the cards; item ids resolved out of the design `item` form.
    #
    # The DIAMONDS tab is deliberately absent: those five cards are real-money IAP
    # ($9.99-$99.99), which this schema cannot express -- CostID/CostCount name an ITEM,
    # not a price -- and the Mars SDK is switched off in our build, so `_bIsIAPItem`
    # goods are unbuyable anyway. Everything below is bought with in-game currency.
    "2": [
        # ---- Coins (bought with Diamonds) -------------------------------------
        _goods(2101, 736, 1, COST_DIAMOND, 490, filt=FILTER_COINS, sort=1),
        _goods(2102, 735, 1, COST_DIAMOND, 290, filt=FILTER_COINS, sort=2),
        _goods(2103, 734, 1, COST_DIAMOND, 90, filt=FILTER_COINS, sort=3),
        _goods(2104, 733, 1, COST_DIAMOND, 50, filt=FILTER_COINS, sort=4),
        _goods(2105, 732, 1, COST_DIAMOND, 30, filt=FILTER_COINS, sort=5),
        _goods(2106, 731, 1, COST_DIAMOND, 10, filt=FILTER_COINS, sort=6),

        # ---- Stamina & Tickets ------------------------------------------------
        # Three stamina cards at escalating prices, each with its own daily cap --
        # 1 at 30 gems, then 3 at 60, then 6 at 100.
        _goods(2201, 711, 1, COST_DIAMOND, 30, filt=FILTER_ITEMS, sort=1,
               limit=1, reset=RESET_DAILY, once_max=1),
        _goods(2202, 711, 1, COST_DIAMOND, 60, filt=FILTER_ITEMS, sort=2,
               limit=3, reset=RESET_DAILY, once_max=3),
        _goods(2203, 711, 1, COST_DIAMOND, 100, filt=FILTER_ITEMS, sort=3,
               limit=6, reset=RESET_DAILY, once_max=6),
        _goods(2204, 6, 1, COST_DIAMOND, 50, filt=FILTER_ITEMS, sort=4,
               limit=10, reset=RESET_DAILY),
        _goods(2205, 16, 1, COST_DIAMOND, 50, filt=FILTER_ITEMS, sort=5,
               limit=3, reset=RESET_DAILY, once_max=3),
        _goods(2206, 17, 1, COST_DIAMOND, 50, filt=FILTER_ITEMS, sort=6,
               limit=3, reset=RESET_DAILY, once_max=3),
        _goods(2207, 18, 1, COST_DIAMOND, 50, filt=FILTER_ITEMS, sort=7,
               limit=3, reset=RESET_DAILY, once_max=3),
        _goods(2208, 19, 1, COST_DIAMOND, 100, filt=FILTER_ITEMS, sort=8,
               limit=3, reset=RESET_DAILY, once_max=3),

        # ---- Medal of Pride (arena currency, item 9) --------------------------
        # The ★5 cards are `_action 1` CAST items -- buying one grants the character
        # and pushes Char `create`, exactly like the quest reward. One a month each.
        _goods(2301, 110435, 1, COST_MEDAL, 38000, filt=FILTER_PVP, sort=1,
               limit=1, reset=RESET_MONTHLY, once_max=1),
        _goods(2302, 111035, 1, COST_MEDAL, 27000, filt=FILTER_PVP, sort=2,
               limit=1, reset=RESET_MONTHLY, once_max=1),
        _goods(2303, 110585, 1, COST_MEDAL, 27000, filt=FILTER_PVP, sort=3,
               limit=1, reset=RESET_MONTHLY, once_max=1),
        _goods(2304, 110745, 1, COST_MEDAL, 27000, filt=FILTER_PVP, sort=4,
               limit=1, reset=RESET_MONTHLY, once_max=1),
        _goods(2305, 110575, 1, COST_MEDAL, 27000, filt=FILTER_PVP, sort=5,
               limit=1, reset=RESET_MONTHLY, once_max=1),
        _goods(2306, 110665, 1, COST_MEDAL, 27000, filt=FILTER_PVP, sort=6,
               limit=1, reset=RESET_MONTHLY, once_max=1),
        _goods(2307, 212, 1, COST_MEDAL, 22000, filt=FILTER_PVP, sort=7,
               limit=1, reset=RESET_MONTHLY, once_max=1),
        _goods(2308, 104, 1, COST_MEDAL, 250, filt=FILTER_PVP, sort=8,
               limit=100, reset=RESET_MONTHLY),
        _goods(2309, 103, 1, COST_MEDAL, 80, filt=FILTER_PVP, sort=9,
               limit=100, reset=RESET_WEEKLY),

        # ---- Guild Pt ---------------------------------------------------------
        _goods(2401, 1200021, 1, COST_GUILD_PT, 2500, filt=FILTER_GUILD, sort=1),
        _goods(2402, 1200022, 1, COST_GUILD_PT, 3000, filt=FILTER_GUILD, sort=2),
        _goods(2403, 1200023, 1, COST_GUILD_PT, 3000, filt=FILTER_GUILD, sort=3),
        _goods(2404, 1200024, 1, COST_GUILD_PT, 2500, filt=FILTER_GUILD, sort=4),
        _goods(2405, 1200020, 1, COST_GUILD_PT, 1200, filt=FILTER_GUILD, sort=5),
        # The three "Awaker Soulmirror+①/②/③ Selector Box" cards are mail-delivered
        # selectors; our pack has only the generic 1382, not the ①/②/③ tiers, so one
        # card stands in for the set rather than inventing two ids.
        _goods(2406, 1382, 1, COST_GUILD_PT, 5000, filt=FILTER_GUILD, sort=6,
               limit=1, reset=RESET_MONTHLY, once_max=1),
        _goods(2407, 711, 1, COST_GUILD_PT, 500, filt=FILTER_GUILD, sort=7,
               limit=2, reset=RESET_DAILY, once_max=2),
        _goods(2408, 2, 50000, COST_GUILD_PT, 250, filt=FILTER_GUILD, sort=8,
               limit=10, reset=RESET_DAILY),
        _goods(2409, 30, 500, COST_GUILD_PT, 200, filt=FILTER_GUILD, sort=9,
               limit=10, reset=RESET_DAILY),
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
        return False, None, f"unknown goods {goods_id}", []
    _gid, _bt, _et, _cycle, limit, item_id, item_cnt, cost_id, cost_cnt = row[:9]
    once_max = row[16]
    if once_max and count > once_max:
        return False, shop_id, f"over per-purchase max {once_max}", []

    bought = state.setdefault("shop_bought", {}).setdefault(str(shop_id), {})
    rec = bought.get(str(goods_id)) or {"Count": 0, "Reset": 0}
    if limit and rec["Count"] + count > limit:
        return False, shop_id, f"over purchase cap {limit}", []

    if not spend_cost(state, cost_id, cost_cnt * count):
        return False, shop_id, f"cannot pay {cost_cnt * count}x item {cost_id}", []
    new_chars, out_id, out_cnt = grant_goods(state, item_id, item_cnt * count)
    rec["Count"] += count
    bought[str(goods_id)] = rec
    state.setdefault("_last_purchase", {})[str(goods_id)] = [out_id, out_cnt]
    return True, shop_id, "", new_chars


def grant_goods(state, item_id, amount, rng=None):
    """Hand over what a storefront card promises.
    -> ([new char uid, ...], popup item id, popup count).

    The popup ids matter: `PanelItemMsg.EnqueItemPopupInfo` (0x15ABEC0) strips items
    whose `_action` is in the mask 0x214 (bits 2/4/9) plus `_action 0` of `_class 2`,
    and returns false on an empty list -- so naming a `_action 2` box gives no
    confirmation popup at all. Report the real payout instead.
    """
    import random as _r
    rng = rng or _r
    row = bt.dd.row("item", item_id) or {}
    action = row.get("_action")

    # A CAST. `_action 1` survives the popup mask, and the item row's tweenPopup flag
    # is what makes the popup build CharDatas and play the reveal, so keep the id.
    from .quests import char_reward_of          # local: quests imports core, not us
    char = char_reward_of(item_id)
    if char and action == 1:
        char_id, star, _display = char
        uids = [add_char(state, char_id, star=star) for _ in range(max(1, amount))]
        return uids, item_id, amount

    # A random STARSHARD box: roll a real shard so the player gets something they can
    # actually equip, and report the one they got.
    bundle = RUNE_BUNDLES.get(int(item_id))
    if bundle:
        pool = rune_bundle_pool(*bundle)
        if pool:
            from .gear import grant_rune       # local: gear imports core, not us
            got = None
            for _ in range(max(1, amount)):
                got = rng.choice(pool)
                grant_rune(state, got, rune_slot(got) or 1)
            return [], got, max(1, amount)

    # A random ★5+ cast orb.
    orb = CHAR_ORB_BUNDLES.get(int(item_id))
    if orb:
        pool = awaker_pool(orb)
        if pool:
            got_item = None
            uids = []
            for _ in range(max(1, amount)):
                cid, got_item = rng.choice(pool)
                uids.append(add_char(state, cid, star=orb))
            return uids, got_item, max(1, amount)

    # A fixed bundle: pay the amount printed on the card.
    payout = BUNDLE_PAYOUT.get(int(item_id))
    if payout:
        real_id, per = payout
        grant_reward(state, real_id, per * amount)
        return [], real_id, per * amount

    grant_reward(state, item_id, amount)
    return [], item_id, amount


def goods_reward(state, goods_id, count):
    """-> (item id, total count) for reply 513's intargs[2:4], the "you received" popup.

    **Report what the player actually GOT.** `PanelItemMsg.EnqueItemPopupInfo`
    (0x15ABEC0) strips items whose `_action` is in the mask 0x214 (bits 2/4/9) plus
    `_action 0` of `_class 2`, and returns false on an empty list -- so naming a
    storefront BOX (`_action 2`) produced no confirmation popup at all: the purchase
    went through, the currency moved, and the player saw nothing. grant_goods records
    the resolved payout per goods id, which also covers the random starshard boxes
    where the card cannot know in advance which shard was rolled.
    """
    last = (state.get("_last_purchase") or {}).get(str(goods_id))
    if last:
        return last[0], last[1]
    _sid, row = find_shop_goods(state, goods_id)
    if not row:
        return 0, 0
    item_id, per = row[5], row[6]
    payout = BUNDLE_PAYOUT.get(int(item_id))
    if payout:
        item_id, per = payout[0], payout[1] * per
    elif int(item_id) in CHAR_ORB_BUNDLES:
        pool = awaker_pool(CHAR_ORB_BUNDLES[int(item_id)])
        if pool:
            item_id = pool[0][1]
    elif int(item_id) in RUNE_BUNDLES:
        # Before any purchase we cannot know which shard will roll; name one from the
        # pool so the answer is at least a displayable item rather than the box.
        pool = rune_bundle_pool(*RUNE_BUNDLES[int(item_id)])
        if pool:
            item_id = pool[0]
    return item_id, per * max(int(count), 1)


# **GoodsBuyData's wire keys are `cnt` and `reset`, NOT the C# field names.**
# Recovered with tools/json_keys.py --class GoodsBuyData. Sending Count/Reset cost two
# visible bugs at once: every purchase-cap counter stayed at 0/N however much the player
# bought, and `HandleShopBuy` (0x1804598) does
# `if (!DeserializeObject(strargs[0], &shop.freeList)) return;` BEFORE building the
# "you received" popup, so a payload it cannot read swallowed the confirmation whole.
def _bought_wire(bought):
    return {gid: {"cnt": rec.get("Count", 0), "reset": rec.get("Reset", 0)}
            for gid, rec in (bought or {}).items()}


def shop_bought_json(state, shop_id):
    bought = (state.get("shop_bought") or {}).get(str(shop_id)) or {}
    return json.dumps(_bought_wire(bought), separators=(",", ":"))


def shop_goods_json(state, shop_id):
    """The THREE strargs of the goods sync (cmd 260 -> 516).

    `DeserializeGoodsAndBoughtData` (RVA 0x1805F18) reads strargs[0] as the bought data
    and then **returns early unless there are at least 2 strargs** -- which is why a
    single-strarg reply left the shop with no goods and the panel spinning. With >= 2 it
    calls `DeserializeShopGoodsData(shop, strargs[1], strargs[2] or null)`
    (RVA 0x1806294), where:
      strargs[0] `Dictionary<int, GoodsBuyData>`  -- {cnt, reset} per goods id, bought
      strargs[1] `List<List<int>>`                -- THE GOODS LIST itself
      strargs[2] `Dictionary<int, string>`        -- per-goods strings, only read on the
                                                    international build (EN is one)
    The goods list is server data, not design data -- which goods a shop sells was
    live-ops. Sending an empty list yields an empty but WORKING shop: the deserialize
    succeeds, the caller dispatches ShopEvent SYNC_SHOP_GOODS(11), and the panel closes
    its overlay instead of hanging.
    """
    bought = _bought_wire((state.get("shop_bought") or {}).get(str(shop_id)) or {})
    goods = ((state.get("shop_goods") or {}).get(str(shop_id))
             or DEFAULT_SHOP_GOODS.get(str(shop_id)) or [])
    return [json.dumps(bought, separators=(",", ":")),
            json.dumps(goods, separators=(",", ":")),
            "{}"]

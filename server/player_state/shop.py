"""Shop.

Split out of the former monolithic core.py; depends only on .core.
"""


import json
import re
import time

import battle as bt

from .core import (
    DAILY_RESET_HOUR,
    QUEST_CASE_BUY_GOODS,
    add_char,
    bump_quest_counter,
    rune_slot,
    grant_reward,
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
FILTER_HOLY_BLOOD = 16023   # "Holy Blood"     -- Soul Altar
FILTER_SKILL_UP = 16031     # "Skill Up"
FILTER_ORBS = 16052         # "Summoning Orbs"
FILTER_STAR_SHARDS = 16053  # "Summon Star Shards"
FILTER_DAILY = 16034        # "Daily Sales"    -- Mammon's three tabs are reset tiers
FILTER_WEEKLY = 16035       # "Weekly Sales"
FILTER_MONTHLY = 16036      # "Monthly Sales"

# `ResetCycle` (index 3) drives the "Daily/Weekly/Monthly Reset" strip under each card
# (texts 113039/113040/113041). The int->label mapping is not in an enum; 1/2/3 in
# design order is the obvious reading and is what the store footage's counters imply,
# but it is UNVERIFIED -- check the labels in game before trusting them.
# **ResetCycle is a number of HOURS, not an enum.** `StoreItemHandler.SetValue` computes
# `days = ResetCycle / 24` and picks the strip's wording from it:
#   24..47  -> text 113039 "Daily Reset"
#   days 7  -> text 113040 "Weekly Reset"
#   days 30 -> text 113041 "Monthly Reset"
#   anything else -> no branch runs, and the card keeps the PREFAB's default label,
#                    which reads "Left Days:{days}"
# So the old 1/2/3 rendered as "Left Days:000" on every capped card in the game -- the
# cycle was being sent, it just never matched a wording branch. The chosen text is then
# String.Format'd with the card's Limit, which is where the "0/45" comes from.
RESET_NONE, RESET_DAILY, RESET_WEEKLY, RESET_MONTHLY = 0, 24, 24 * 7, 24 * 30

# Cost currencies, as the storefront quotes them.
COST_DIAMOND = 1            # CurrencyType.Cash
COST_MEDAL = 9              # "Medal of Pride" -- a plain bag item, not a currency
COST_GUILD_PT = 4           # CurrencyType.Guild
COST_PAID_DIAMOND = 11      # CurrencyType 32 -- the $-marked gem, a separate balance
COST_HOLY_BLOOD = 3         # "Holy Blood of Saint" -- the Soul Altar's own currency
# 福利代幣 "Feel Lucky", the P token the Super Sales tab is priced in. A plain bag item
# (`_action 0`), not a CurrencyType, so it is spent through spend_item like the scrolls.
COST_FEEL_LUCKY = 46
# Unsummoning a cast pays out Mana Crystals; the Soul Altar spends them back. The rare
# grade (Prime) buys the Bunrei selector boxes, the common grade the ★5 Awaker Orb.
COST_MANA_CRYSTAL = 501
COST_PRIME_MANA_CRYSTAL = 502

# **Storefront bundles are `_action 2` BOX items with no contents in the pack.** They
# carry the art and the name the footage shows ("Deluxe Coin Box (1,500,000)"), so we
# sell those ids and pay out the amount printed on the card. Coins are item 2 (Mira,
# a currency) and Stamina is item 5 (energy), both of which grant_reward routes.
# Values are LISTS: a premium bundle pays several things at once ("Contains Evolution
# Gem x350, Popular Poster x100, Soul Essence x50,000, and ★4 Trainer x100"). The FIRST
# entry is the headline the purchase popup names, since reply 513 carries only one
# (item, count) pair.
COIN, DIAMOND, PAID_DIAMOND, STAMINA = 2, 1, 11, 5
POSTER, SOUL_ESSENCE, EVO_GEM = 487, 30, 556
TRAINER2, TRAINER3, TRAINER4, TRAINER5 = 102, 103, 104, 105
PASS_ABYSS, PASS_RAIDERS, PASS_GYM, PASS_CORRIDOR = 16, 17, 18, 19
# `_action 1` character item -> char 11001 at ★4. NOT the ★5/★6 rows (111005/6).
JACQUELINE_4STAR = 111004

BUNDLE_PAYOUT = {
    # -- Belphe's coin/stamina cards
    736: [(COIN, 1500000)],     # Deluxe Coin Box
    735: [(COIN, 810000)],      # Great Coin Piles
    734: [(COIN, 240000)],      # Coin Piles
    733: [(COIN, 105000)],      # Small Coin Piles
    732: [(COIN, 55000)],       # Coin
    731: [(COIN, 15000)],       # Coin
    711: [(STAMINA, 100)],      # Stamina (100)

    # -- Mammon's Premium Shop, contents read off each card's own description
    922: [(DIAMOND, 10), (COIN, 25000)],                      # Daily Free Bundle
    901: [(TRAINER2, 24), (TRAINER3, 16), (TRAINER4, 10),     # "Power-Leveling"
          (TRAINER5, 5), (COIN, 250000)],
    3648: [(SOUL_ESSENCE, 30000), (COIN, 50000)],             # Daily Soul Essence
    700322: [(POSTER, 12), (STAMINA, 200)],                   # Let's Grow Daily
    923: [(DIAMOND, 25), (COIN, 50000)],                      # Weekly Free Bundle
    3581: [(TRAINER2, 36), (TRAINER3, 24), (TRAINER4, 15),    # Adv. Trainer Box
           (TRAINER5, 9), (COIN, 800000)],
    902: [(EVO_GEM, 200), (COIN, 300000)],                    # Evo Bundle
    700323: [(STAMINA, 1000), (COIN, 1000000)],               # Let's Grow Weekly
    700324: [(PASS_ABYSS, 3), (PASS_RAIDERS, 3),              # Weekly Dungeon Bundle
             (PASS_GYM, 3), (PASS_CORRIDOR, 3)],
    700325: [(SOUL_ESSENCE, 30000), (COIN, 200000)],          # Weekly Soul Essence
    924: [(DIAMOND, 50), (COIN, 100000)],                     # Monthly Free Gift
    3650: [(EVO_GEM, 350), (POSTER, 100),                     # Deluxe Power-Up
           (SOUL_ESSENCE, 50000), (TRAINER4, 100)],
    700014: [(POSTER, 100), (COIN, 250000), (TRAINER3, 10)],  # Ultra Karma Deluxe
    906: [(POSTER, 50), (COIN, 150000), (TRAINER2, 10)],      # Karma Boost Bundle

    # -- QUEST reward bundles. Same `_action 2` wall as the storefront ones: no
    # inventory tab will hold them and the reward popup strips them, so claiming the
    # goal paid out nothing the player could see. Contents come from the row's own
    # CHINESE `_note1` -- the only place they are written down, since the EN string is
    # just the item name repeated.
    #   1200006 "Evolution TUT Bundle" (quest 31019, Lucifer's Note step 19)
    #   CN: 內含【★4機械之隸魔 賈桂琳x1】、【進化石x1200】
    #       = ★4 Eccentric Inventor Jacqueline x1 + Evolution Gem x1200
    # Confirmed against live footage of the claim popup, which shows exactly those two
    # cards side by side.
    1200006: [(JACQUELINE_4STAR, 1), (EVO_GEM, 1200)],
}

# The Guild Pt luckybags are `_action 2` as well, so they hit exactly the same wall --
# unfileable in the bag AND stripped from the reward popup. They are genuine random
# boxes, so instead of a fixed payout each resolves to a real STARSHARD drawn from its
# element at the named grade. Starshard items encode the slot in `_action` (111..116)
# and the grade in `_param2`, so "★4 (UR-LR) Random Slot Endearment" is exactly
# "any _param2 4 Endearment shard", which is what the card promises.
# (element, STAR, slot). The star is `_rarity` (also the id's last digit), the rank is
# `_param2` on the scale N=1 R=2 SR=3 UR=4 LR=5 -- verified across all 756-per-rank
# canonical rows -- and the card's "(UR-LR)" names the rank RANGE, so the pool is
# `_rarity == star and _param2 in (4, 5)`. The old shape filtered `_param2 == 4` alone,
# which is the UR RANK at EVERY star: a ★4 bag paid ★1..★6 shards, owner-reported by a
# player who drew the whole spread from one bag. Slot is fixed only on 1200020, whose
# card says "(Slot 6)" outright.
RUNE_BUNDLES = {
    1200021: ("Endearment", 4, None),
    1200022: ("Chaos", 4, None),
    1200023: ("Hawkeye", 4, None),
    1200024: ("Defender", 4, None),
    1200020: (None, 3, 6),       # ★3 (UR-LR) Random Starshard Luckybag (Slot 6)
    # 311..314 (the Soul Altar "Summon Star Shards" cards) used to be listed here as
    # (None, star) -- except the second field was read as a GRADE, so they rolled the
    # wrong thing in both axes. They are decoded from their names now; see
    # starshard_any_suit_box.
}
RUNE_BUNDLE_RANKS = (4, 5)       # UR, LR -- what every luckybag card promises

# **Drop Info shows "SET" icons, which are display-only and cannot be used.** They are
# the `Random ★{star} {Element}` items -- five per (element, star), one per RANK
# (N/R/SR/UR/LR) in `_param1` order, e.g. ★4 Endearment is 1016..1020 / 5241..5245.
# They exist purely to say "you will get one of this set", so they belong in the
# preview and must NEVER be granted: the purchase still rolls a real shard.
#
# Which of them a card lists depends on what the card randomises, and the live popups
# show both shapes:
#   * "★4 (UR-LR) Random SLOT Endearment"  -> one element, the UR and LR ranks (2 icons)
#   * "★3 (UR-LR) Random Starshard (Slot N)" -> one icon per ELEMENT at that star (6)
STARSHARD_ELEMENTS = ("Endearment", "Chaos", "Hawkeye", "Slayer",
                      "Nightshade", "Mystery")
RANK_UR, RANK_LR = 3, 4          # indices into the five ranks, lowest `_param1` first

_set_item_cache = {}


def starshard_set_items(element, star):
    """The five display-only set icons for one element at one star, rank order."""
    key = (element, star)
    if key not in _set_item_cache:
        want = f"Random \u2605{'I' if star == 1 else star} {element}"
        found = []
        for iid, row in (bt.dd.rows("item") or {}).items():
            if row.get("_action") != 2:
                continue
            if (row.get("_itemName_en") or "").strip() == want:
                found.append((int(row.get("_param1") or 0), int(iid)))
        _set_item_cache[key] = [i for _p, i in sorted(found)]
    return _set_item_cache[key]

# "★5 Awaker Summon Orb" -- a box whose card reads "Summon a random awaker of ★5
# rarity or better". Same `_action 2` problem, so it resolves to a real cast: roll one
# of the ★5 casts and report the matching `_action 1` character item, which both
# survives the popup mask and carries the tweenPopup flag that plays the reveal.
# "Summon a random awaker of ★N rarity or better" -- the Soul Altar's orbs, and the
# ★5 orb Belphe's sells for Medals. All `_action 2`, so each resolves to a real cast.
# **"Awaker" does NOT mean any playable cast.** The Sins (100), Virtues (101) and
# Riders (102) are their own thing and are NOT awakers -- an Awaker Summon Orb draws
# only from alignments 103 and 104. Including the ★5 casts made the ★5 orb a Lucifer
# machine, which is not what the card sells.
#
# **The orb's star is a GRADE, not a granted star.** The card says "Summon a random
# awaker of ★5 rarity or better", and rarity maps to star through the client's own
# CharRareMinStar: SSR (rarity 4, alignment 103 -- all 68 of them) IS ★5, SR (rarity 3,
# alignment 104, the 16 beginner casts) is ★4. Drawing both orbs from both alignments --
# on the earlier reading that the ★5 on the item was only the star the cast arrives
# at -- put the 16 SR casts into the ★5 pool, so the ★5 Awaker Orb sold for Mana
# Crystal and Arena medals paid out a ★4-grade cast that then sits in the Cast List as
# the 4-star unit it is. Owner-reported; alignment/rarity split verified in char.json.
AWAKER_ALIGNMENTS = (103, 104)       # both, for anything that wants every awaker
AWAKER_ALIGNMENTS_5 = (103,)         # SSR -- the ★5 orb
AWAKER_ALIGNMENTS_4 = (104,)         # SR  -- the ★4 orb
# **A "Minion" orb summons MINIONS, not casts.** Every ★3 character item belongs to
# alignment 9001 (mobs) or 905, so pooling the minion orb over the playable alignments
# found nothing at all -- it would have sold a card that grants silently nothing.
MINION_ALIGNMENTS = (9001,)
# item id -> (star to summon at, which alignments it draws from)
CHAR_ORB_BUNDLES = {
    212: (5, AWAKER_ALIGNMENTS_5),
    211: (4, AWAKER_ALIGNMENTS_4),
    210: (3, MINION_ALIGNMENTS),
}

_rune_pool_cache = {}
_awaker_pool_cache = {}


def awaker_pool(star, alignments=None):
    """[(char id, character item id), ...] summonable AT ★`star`.

    **The ★N on a character item is the star the cast is GRANTED at, not the cast's own
    rarity** -- a rarity-3 cast like Caillen has ★4/★5/★6 items, a rarity-4 cast starts
    at ★5. Keying this off `_rarity` therefore found nothing for the ★4 and ★3 orbs,
    whose whole point is to summon a lower-rarity cast at a good star. Filter by the
    item's star and keep only playable casts.
    """
    alignments = tuple(alignments or AWAKER_ALIGNMENTS)
    key = (star, alignments)
    if key not in _awaker_pool_cache:
        chars = {int(cid) for cid, row in (bt.dd.rows("char") or {}).items()
                 if row.get("_alignment") in alignments}
        star = int(star)
        prefix = f"\u2605{'I' if star == 1 else star}"
        out = []
        for iid, row in (bt.dd.rows("item") or {}).items():
            if row.get("_action") != 1:
                continue
            cid = int(row.get("_param1") or 0)
            # **Match the ENGLISH name too.** The CN name of a "Bunrei" (a separate,
            # non-summonable variant sharing the star prefix) also starts with ★N, so
            # a CN-only test dragged 70-odd of them into the pool.
            name = (row.get("_itemName_en") or "")
            if cid in chars and name.startswith(prefix) and "Bunrei" not in name:
                out.append((cid, int(iid)))
        _awaker_pool_cache[key] = sorted(out)
    return _awaker_pool_cache[key]


# The per-suit starshard boxes -- what the Netherworld Note pays for its Temple steps,
# and `_action 2` like every other box, so they land nowhere and the reward popup strips
# them. Claiming step 28 filed an unholdable item 323 in the bag and showed the player
# nothing.
#
# **`_param1` encodes them, in TWO families, and the old decode got both wrong**
# (found 2026-08-18 from a report that the stage-28 goal paid no starshard):
#
#   items 1001..1240, "Random ★N <suit>"  ->  p1 = (50 + 2*suit)*100 + star*10 + slot
#       5211 = suit 1 Endearment ★1 slot 1      5434 = suit 2 Chaos ★3 slot 4
#       prefixes run 52/54/56/58/60/62/64/66 for suits 1..8.
#   items 321..396,   "★N <suit>"         ->  p1 = 2500 + suit*10 + star
#       2513 = suit 1 Endearment ★3            2586 = suit 8 Devotee ★6
#
# The previous version matched only `^52(\d)(\d)$`, so:
#   * **210 of the 240 boxes did not match at all** -- every suit but Endearment
#     granted nothing, which is most of the Note's Temple rewards;
#   * for the 30 that did, it read the last digit as the SUIT when it is the SLOT, so
#     item 1014 "Random ★3 Endearment" handed over a ★3 **Slayer**.
#
# Its comment asserted "5234 = ★3 Slayer" and that the EN names were an unusable
# copy-paste. Both are wrong, and checkable: the names repeat across SLOTS (1001..1005
# are all "Random ★I Endearment" because the name omits the slot), not across suits, and
# all 240 rows agree with the formula above -- see test_goal_rewards.py, which asserts
# exactly that against every box in the pack.
# **The NAME is authoritative, `_param1` only corroborates.** There are THIRTEEN suits
# (equip_suit 1..13, Endearment..Bloodcraze), and the `_param1` encoding above only
# holds for the first eight: from item 1241 ("Random ★3 Innocence", p1 6666) it
# degenerates into a flat counter while the name keeps tracking suit and star. Decoding
# 6666 by formula yields "Devotee ★6" -- confidently wrong. So parse the name, and use
# `_param1` only as a cross-check on the range it does cover.
_BOX_NAME = re.compile(r"^(?:Random\s+)?\u2605(I|\d)\s+(\w+)$")
_SLOTTED_BOX_PREFIX_BASE = 50     # p1 // 100 == 50 + 2*suit, suits 1..8 only
_PLAIN_BOX_PREFIX = 25            # p1 // 100 == 25, suits 1..8 only
_suit_ids_by_name = {}


def _suit_id(name):
    if not _suit_ids_by_name:
        for sid, row in (bt.dd.rows("equip_suit") or {}).items():
            if row.get("_suitName_en"):
                _suit_ids_by_name[row["_suitName_en"]] = int(sid)
    return _suit_ids_by_name.get(name)


def _box_param1_star_suit(p):
    """(star, suit) from `_param1`, for the two families that encode it, else None."""
    prefix, rest = divmod(int(p), 100)
    if prefix == _PLAIN_BOX_PREFIX:                       # 2500 + suit*10 + star
        suit, star = divmod(rest, 10)
    elif prefix > _SLOTTED_BOX_PREFIX_BASE and not (prefix - _SLOTTED_BOX_PREFIX_BASE) % 2:
        suit = (prefix - _SLOTTED_BOX_PREFIX_BASE) // 2   # 52->1 .. 66->8
        star = rest // 10
    else:
        return None
    return (star, suit) if 1 <= suit <= 8 and 1 <= star <= 6 else None


def random_rune_box(item_id):
    """-> (star, suit) for a per-suit starshard box, or None.

    The slot, where the id carries one, is deliberately dropped: `rune_star_suit_pool`
    rolls any slot, which is what the "Random" in the name means for the player.
    """
    row = bt.dd.row("item", int(item_id)) or {}
    if row.get("_action") != 2:
        return None
    m = _BOX_NAME.match((row.get("_itemName_en") or "").strip())
    if not m:
        return None
    star = 1 if m.group(1) == "I" else int(m.group(1))
    suit = _suit_id(m.group(2))
    if not suit or not (1 <= star <= 6):
        return None
    return (star, suit)


# The canonical per-suit shard bands. A starshard id is `ELEMENT*1000 + slot*100 +
# rank*10 + star`, and elements 201..221 are the ordinary sets -- 180 ids each, the full
# 6 slots x 5 ranks x 6 stars grid. Elements 720/726/727 also carry a `_suitID` and so
# used to leak into these pools: they are fixed-stat event variants ("LR Chaos Starshard
# II_ATK"), and handing one over for a plain "★3 Chaos" box is the wrong item.
# NOTE the element is NOT 200 + suit beyond suit 8 -- 209 is suit 11, 212 is suit 9,
# 214 is suit 10 -- so this is a band test, not an arithmetic one.
STARSHARD_ELEMENT_BAND = range(201, 222)


# The ANY-SUIT boxes: "Random ★N Starshard" (291..296, any rank) and
# "Random ★N <RANK> Starshard" (311..314, a fixed rank). Name-driven for the same
# reason as the per-suit ones -- `_param1` is ambiguous across the two: 291 "★I
# Starshard" is 2401 while 311 "★I SR Starshard" is 2413, so the same 24xx prefix means
# `2400 + star` in one and `2400 + star*10 + grade` in the other.
#
# These used to be hand-listed in RUNE_BUNDLES as `313: (None, 3)`, where the 3 was fed
# to `rune_bundle_pool` as a GRADE (`_param2` = rank + 1) while the comment called it a
# star -- so "Random ★3 LR Starshard" rolled any star at SR, and paid out ★6 SR.
_ANY_SUIT_BOX = re.compile(
    r"^(?:Random\s+)?\u2605(I|\d)\s+(?:(N|R|SR|UR|LR)\s+)?Starshard$")
STARSHARD_RANKS = ("N", "R", "SR", "UR", "LR")     # id digit `rank`, 0..4


def starshard_any_suit_box(item_id):
    """-> (star, rank or None) for an any-suit starshard box, else None.

    Accepts `_action` 7 as well as 2: item 226 "★3 LR Starshard" is a SELECTOR whose
    name follows the identical convention, and the client has no panel to offer the
    choice anyway (see the selector note in grant_goods).
    """
    row = bt.dd.row("item", int(item_id)) or {}
    if row.get("_action") not in (2, SELECTOR_ACTION):
        return None
    m = _ANY_SUIT_BOX.match((row.get("_itemName_en") or "").strip())
    if not m:
        return None
    star = 1 if m.group(1) == "I" else int(m.group(1))
    rank = STARSHARD_RANKS.index(m.group(2)) if m.group(2) else None
    return (star, rank) if 1 <= star <= 6 else None


def rune_any_suit_pool(star, rank=None):
    """Every canonical shard at `star`, across all suits, optionally at one rank.

    A shard id is ELEMENT*1000 + slot*100 + rank*10 + star, so both axes are read
    straight off the id -- rank is NOT missing from the data, which is what the old
    "rank is not a field on the shard rows" note assumed.
    """
    key = ("any", star, rank)
    if key not in _rune_pool_cache:
        pool = [int(i) for i in (bt.dd.rows("item") or {})
                if int(i) // 1000 in STARSHARD_ELEMENT_BAND
                and int(i) % 10 == star
                and (rank is None or (int(i) // 10) % 10 == rank)]
        _rune_pool_cache[key] = sorted(pool)
    return _rune_pool_cache[key]


def rune_star_suit_pool(star, suit):
    """Starshard item ids of `suit` at `_rarity` == star, any slot or grade."""
    key = ("star", star, suit)
    if key not in _rune_pool_cache:
        eq = bt.dd.rows("equipment") or {}
        pool = []
        for iid, row in (bt.dd.rows("item") or {}).items():
            if int(row.get("_action") or 0) not in range(111, 117):
                continue
            if int(iid) // 1000 not in STARSHARD_ELEMENT_BAND:
                continue
            if int(row.get("_rarity") or 0) != star:
                continue
            if (eq.get(row.get("_param1")) or {}).get("_suitID") != suit:
                continue
            pool.append(int(iid))
        _rune_pool_cache[key] = sorted(pool)
    return _rune_pool_cache[key]


def rune_bundle_pool(element, star, slot=None):
    """Starshard item ids for one luckybag: element (or any), at STAR, ranks UR-LR.

    Banded to the canonical element ids (201xxx..221xxx): the pack also carries the
    legacy per-stat rows 2001..2018 ("3星霄闇攻擊" -- ★3 Nightshade et al.) whose
    `_action`/`_param2` look shard-shaped, and without the band the any-element bag
    dealt them out. They are not equippable through the Starshards panel, which is the
    "test starshards" a player reported drawing."""
    key = (element, star, slot)
    if key not in _rune_pool_cache:
        pool = []
        for iid, row in (bt.dd.rows("item") or {}).items():
            i = int(iid)
            if i // 1000 not in STARSHARD_ELEMENT_BAND:
                continue
            if int(row.get("_action") or 0) not in range(111, 117):
                continue
            if int(row.get("_rarity") or 0) != star:
                continue
            if int(row.get("_param2") or 0) not in RUNE_BUNDLE_RANKS:
                continue
            if slot and (i // 100) % 10 != slot:
                continue
            if element and element not in (row.get("_itemName_en") or ""):
                continue
            pool.append(i)
        _rune_pool_cache[key] = sorted(pool)
    return _rune_pool_cache[key]

# `Limit` (index 4) is the purchase cap. Three regimes, and the encoding is not
# obvious -- read out of `StoreItemHandler.SetValue` (0x34407C0), which does
#
#     LDR  W8, [X20,#0x20]   ; Limit
#     LSR  W8, W8, #0x1F     ; sign bit
#     EOR  W1, W8, #1        ; active = !(Limit < 0)
#     BL   GameObject$$SetActive
#
# so the "Purchase Cap" strip (text 113009) is shown only while Limit >= 0:
#   Limit > 0   capped, renders "Purchase Cap bought/Limit"
#   Limit == 0  renders "Purchase Cap 0/0" and the card CANNOT BE BOUGHT -- 0 is a
#               real cap of zero, not "no cap"
#   Limit < 0   UNCAPPED: the strip is hidden and the card can be bought forever
GOODS_DEFAULT_LIMIT = 99
GOODS_NO_LIMIT = -1
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
        # The footage's three "Awaker Soulmirror+①/②/③ Selector Box" cards are OMITTED.
        # They are `_action 7` SELECTORS ("自選", self-select), not `_action 2`
        # containers: tapping one opens a choose-your-reward flow that is delivered by
        # mail, and none of that exists here. Standing item 1382 in for them put a
        # permanent modal overlay over the game with nothing to dismiss it -- the
        # display tap never even reaches the server, so there is no reply we could fix
        # it with. A missing card is better than one that locks the client.
        _goods(2407, 711, 1, COST_GUILD_PT, 500, filt=FILTER_GUILD, sort=7,
               limit=2, reset=RESET_DAILY, once_max=2),
        _goods(2408, 2, 50000, COST_GUILD_PT, 250, filt=FILTER_GUILD, sort=8,
               limit=10, reset=RESET_DAILY),
        _goods(2409, 30, 500, COST_GUILD_PT, 200, filt=FILTER_GUILD, sort=9,
               limit=10, reset=RESET_DAILY),
    ],
    # 1 = Mammon's Premium Shop. Three tabs, one per reset tier, transcribed from
    # footage. Every card here is a `_action 2` BUNDLE, so each needs a BUNDLE_PAYOUT
    # entry -- the contents are read straight off the card's own description text (and
    # for the Trainer boxes, off the in-game Drop Info panel).
    #
    # The "Free" cards cost nothing: CostCount 0 against the coin item, which
    # spend_cost trivially satisfies. They are still capped and still reset.
    #
    # Two cards are quoted in PAID diamonds (item 11, CurrencyType 32) rather than the
    # ordinary gem -- the footage shows the $-marked icon on Ultra Karma Deluxe Box and
    # the 5,000,000 Coin card.
    "1": [
        # ---- Super Sales ------------------------------------------------------
        # The Feel Lucky tab, and the ONLY sink for item 46 (福利代幣, "Feel Lucky" --
        # the P token): "Can be obtained in Feel Lucky quests. Take this token to
        # exchange..." Its filter id was defined here from the start but no goods ever
        # carried it, so the tab rendered empty and the token had nowhere to go.
        # Transcribed from live footage the same way the rest of this table was; the
        # cards visible were 100 gems / 10, ★5 Trainer / 3, Refined Crystal / 5 and
        # 250 Grimoire of Sin Fragment / 80, all on a monthly reset. The strip scrolls,
        # so there may be further cards off the right edge that are not here yet.
        _goods(1401, COST_DIAMOND, 100, COST_FEEL_LUCKY, 10, filt=FILTER_SALES,
               sort=1, limit=45, reset=RESET_MONTHLY),
        _goods(1402, 105, 1, COST_FEEL_LUCKY, 3, filt=FILTER_SALES,
               sort=2, limit=100, reset=RESET_MONTHLY),
        _goods(1403, 8, 1, COST_FEEL_LUCKY, 5, filt=FILTER_SALES,
               sort=3, limit=10, reset=RESET_MONTHLY),
        # 541, not 700311 -- both are named "Grimoire of Sin Fragment", but 541 is the
        # plain `_action 0` collectible whose note matches the card ("Collect 1000
        # fragments to exchange Grimoire of Sin in Soul Altar"); 700311 is an `_action 4`
        # use-item.
        _goods(1404, 541, 250, COST_FEEL_LUCKY, 80, filt=FILTER_SALES,
               sort=4, limit=2, reset=RESET_MONTHLY, once_max=2),
        # Virtue and Rider fragments on the same terms as the Sin card. Not seen on the
        # footage -- ours, so the other two factions are not stuck without a source --
        # but 542/543 are the exact counterparts of 541: same `_action 0`, same
        # "Collect 1000 fragments to exchange ... in Soul Altar" note.
        _goods(1405, 542, 250, COST_FEEL_LUCKY, 80, filt=FILTER_SALES,
               sort=5, limit=2, reset=RESET_MONTHLY, once_max=2),
        _goods(1406, 543, 250, COST_FEEL_LUCKY, 80, filt=FILTER_SALES,
               sort=6, limit=2, reset=RESET_MONTHLY, once_max=2),

        # ---- Daily Sales ------------------------------------------------------
        _goods(1101, 922, 1, COIN, 0, filt=FILTER_DAILY, sort=1,
               limit=1, reset=RESET_DAILY, once_max=1),
        _goods(1102, 901, 1, COST_DIAMOND, 190, filt=FILTER_DAILY, sort=2,
               limit=2, reset=RESET_DAILY, once_max=2),
        _goods(1103, 3648, 1, COST_DIAMOND, 130, filt=FILTER_DAILY, sort=3,
               limit=1, reset=RESET_DAILY, once_max=1),
        _goods(1104, 700322, 1, COST_DIAMOND, 130, filt=FILTER_DAILY, sort=4,
               limit=3, reset=RESET_DAILY, once_max=3),

        # ---- Weekly Sales -----------------------------------------------------
        _goods(1201, 923, 1, COIN, 0, filt=FILTER_WEEKLY, sort=1,
               limit=1, reset=RESET_WEEKLY, once_max=1),
        _goods(1202, 3581, 1, COST_DIAMOND, 590, filt=FILTER_WEEKLY, sort=2,
               limit=2, reset=RESET_WEEKLY, once_max=2),
        _goods(1203, 902, 1, COST_DIAMOND, 590, filt=FILTER_WEEKLY, sort=3,
               limit=3, reset=RESET_WEEKLY, once_max=3),
        _goods(1204, 700323, 1, COST_DIAMOND, 490, filt=FILTER_WEEKLY, sort=4,
               limit=1, reset=RESET_WEEKLY, once_max=1),
        _goods(1205, 700324, 1, COST_DIAMOND, 490, filt=FILTER_WEEKLY, sort=5,
               limit=1, reset=RESET_WEEKLY, once_max=1),
        _goods(1206, 700325, 1, COST_DIAMOND, 490, filt=FILTER_WEEKLY, sort=6,
               limit=3, reset=RESET_WEEKLY, once_max=3),
        _goods(1207, COIN, 5000000, COST_PAID_DIAMOND, 590, filt=FILTER_WEEKLY,
               sort=7, limit=1, reset=RESET_WEEKLY, once_max=1),

        # ---- Monthly Sales ----------------------------------------------------
        _goods(1301, 924, 1, COIN, 0, filt=FILTER_MONTHLY, sort=1,
               limit=1, reset=RESET_MONTHLY, once_max=1),
        _goods(1302, 3650, 1, COST_DIAMOND, 1890, filt=FILTER_MONTHLY, sort=2,
               limit=2, reset=RESET_MONTHLY, once_max=2),
        _goods(1303, 700014, 1, COST_PAID_DIAMOND, 790, filt=FILTER_MONTHLY, sort=3,
               limit=2, reset=RESET_MONTHLY, once_max=2),
        _goods(1304, 906, 1, COST_DIAMOND, 590, filt=FILTER_MONTHLY, sort=4,
               limit=1, reset=RESET_MONTHLY, once_max=1),
        _goods(1305, STAMINA, 2000, COST_DIAMOND, 490, filt=FILTER_MONTHLY, sort=5,
               limit=1, reset=RESET_MONTHLY, once_max=1),
    ],
    # 3 = Asmodeus's Soul Altar, all four tabs.
    #
    # The Grimoires and Inherit Gems are the game's SKILL-UP fodder, and the Grimoires
    # are `_action 1` CASTS (531 -> char 90401 and so on) -- you feed them to another
    # cast, exactly like the Transcender Gremlins -- so they are granted into the
    # roster, not the bag. That looks wrong at a glance and is right.
    #
    # Skill Up is the fragment exchange, and the rule is uniform: an item costs its OWN
    # fragment, item N <- fragment N+10 for the Grimoires (531..536 <- 541..546) and
    # 12/21/13/14/23 <- 300011..300015 for the Inherit Gems. The Holy Blood tab's own
    # card text confirms the rate ("Collect 1000 fragments to exchange Grimoire of Sin
    # in Soul Altar").
    #
    # Every card here is bought with the currency for exactly the thing it sells, which
    # is the pattern the cost icons show: an orb costs that orb's FRAGMENT, a Gremlin
    # costs that Gremlin's PIECES, a shard costs that shard's TICKET.
    "3": [
        # ---- Holy Blood (bought with Holy Blood of Saint, item 3) --------------
        # The tab's first two cards are the Bunrei SELECTORS, bought with Prime Mana
        # Crystal (the rare grade you get from unsummoning). Their prices are cropped
        # in the footage -- the ids, the currency and the contents are not.
        _goods(3107, 1432, 1, COST_PRIME_MANA_CRYSTAL, 1, filt=FILTER_HOLY_BLOOD,
               sort=1, limit=1, reset=RESET_MONTHLY, once_max=1),
        _goods(3108, 1500002, 1, COST_PRIME_MANA_CRYSTAL, 1, filt=FILTER_HOLY_BLOOD,
               sort=2, limit=1, reset=RESET_MONTHLY, once_max=1),
        _goods(3101, 533, 1, COST_HOLY_BLOOD, 12000, filt=FILTER_HOLY_BLOOD, sort=3,
               limit=1, reset=RESET_MONTHLY, once_max=1),
        _goods(3102, 534, 1, COST_HOLY_BLOOD, 7500, filt=FILTER_HOLY_BLOOD, sort=4,
               limit=1, reset=RESET_MONTHLY, once_max=1),
        _goods(3103, 541, 500, COST_HOLY_BLOOD, 20000, filt=FILTER_HOLY_BLOOD, sort=5,
               limit=1, reset=RESET_MONTHLY, once_max=1),
        _goods(3104, 542, 500, COST_HOLY_BLOOD, 20000, filt=FILTER_HOLY_BLOOD, sort=6,
               limit=1, reset=RESET_MONTHLY, once_max=1),
        _goods(3105, 12, 1, COST_HOLY_BLOOD, 15000, filt=FILTER_HOLY_BLOOD, sort=7,
               limit=1, reset=RESET_MONTHLY, once_max=1),
        _goods(3106, 21, 1, COST_HOLY_BLOOD, 15000, filt=FILTER_HOLY_BLOOD, sort=8,
               limit=1, reset=RESET_MONTHLY, once_max=1),

        # ---- Skill Up (each item bought with its OWN fragment) -----------------
        # The rule is uniform and exact: Grimoire N is bought with fragment N+10, so
        # 531..536 <- 541..546. The tab LEADS with the two Awaker Grimoires (★4 at 625,
        # ★5 at 750) -- both were missing entirely until 2026-08-13, and a quest step
        # sends the player here for the ★4 one.
        #
        # **These two goods ids are NOT ours to choose.** Quests 31017 and 31034 carry
        # `_case_v1 = 3305` and quest 31041 carries `3304` -- "Go to Shop-Soul Altar and
        # exchange Grimoire of ★4/★5 Awaker" -- and the GO! button on those steps sends
        # that id straight to the server as SendGoodsIDToShopIDCmd (cmd 261), which we
        # answer with the shop and tab to jump to. Number these cards anything else and
        # the button lands in the wrong tab, or in a shop that does not sell the thing
        # the quest just asked for. That is why the Summoning Orbs tab below had to
        # move off 3301..3309: it was squatting on the real ids.
        _goods(3305, 535, 1, 545, 625, filt=FILTER_SKILL_UP, sort=1),
        _goods(3304, 534, 1, 544, 750, filt=FILTER_SKILL_UP, sort=2),
        # The Grimoire of Sin card is cropped in the footage; its price is taken from
        # the 1000-fragment rate the other Grimoires and the Holy Blood card both
        # state. Reset strips are cropped on this whole tab, so these are uncapped.
        _goods(3201, 531, 1, 541, 1000, filt=FILTER_SKILL_UP, sort=3),
        _goods(3202, 532, 1, 542, 1000, filt=FILTER_SKILL_UP, sort=4),
        _goods(3203, 533, 1, 543, 1000, filt=FILTER_SKILL_UP, sort=5),
        _goods(3204, 536, 1, 546, 180, filt=FILTER_SKILL_UP, sort=6),
        # The Inherit Gems are a SEPARATE product line from the Grimoires -- their own
        # items (12/13/14/21/23) bought with their own Inherit Gem Fragments. They sit
        # further right in the tab and are cropped in every shot we have, so their
        # prices are inferred from the same footage and are the least certain here.
        _goods(3205, 14, 1, 300014, 750, filt=FILTER_SKILL_UP, sort=7),
        _goods(3206, 23, 1, 300015, 625, filt=FILTER_SKILL_UP, sort=8),
        _goods(3207, 12, 1, 300011, 900, filt=FILTER_SKILL_UP, sort=9),
        _goods(3208, 13, 1, 300013, 900, filt=FILTER_SKILL_UP, sort=10),
        _goods(3209, 21, 1, 300012, 900, filt=FILTER_SKILL_UP, sort=11),

        # ---- Summoning Orbs ---------------------------------------------------
        # Orbs are `_action 2` and summon "a random cast of ★N or better", so they
        # resolve to a real cast through CHAR_ORB_BUNDLES.
        # The tab's first card is a second ★5 Awaker Orb bought with Mana Crystal (the
        # common grade) rather than with Orb Fragments -- 70 of them. The cost ICON was
        # unreadable in the footage and is now identified.
        _goods(3601, 212, 1, COST_MANA_CRYSTAL, 70, filt=FILTER_ORBS, sort=1,
               limit=5, reset=RESET_WEEKLY, once_max=5),
        _goods(3602, 212, 1, 300003, 100, filt=FILTER_ORBS, sort=2,
               limit=5, reset=RESET_WEEKLY, once_max=5),
        _goods(3603, 211, 1, 300002, 100, filt=FILTER_ORBS, sort=3,
               limit=5, reset=RESET_WEEKLY, once_max=5),
        _goods(3604, 210, 1, 300001, 100, filt=FILTER_ORBS, sort=4,
               limit=20, reset=RESET_DAILY, once_max=20),
        # Transcender Gremlins are `_action 1` CASTS bought with their own Pieces, and
        # they carry NO purchase cap -- buy as many as you have Pieces for. That is
        # GOODS_NO_LIMIT (a NEGATIVE Limit), which is what hides the cap strip; limit=0
        # is a cap of zero and renders "Purchase Cap 0/0" over an unbuyable card.
        _goods(3605, 115, 1, 120, 1, filt=FILTER_ORBS, sort=5, limit=GOODS_NO_LIMIT),
        _goods(3606, 114, 1, 119, 1, filt=FILTER_ORBS, sort=6,
               limit=GOODS_NO_LIMIT),
        _goods(3607, 113, 1, 118, 1, filt=FILTER_ORBS, sort=7,
               limit=GOODS_NO_LIMIT),
        _goods(3608, 112, 1, 117, 1, filt=FILTER_ORBS, sort=8,
               limit=GOODS_NO_LIMIT),
        _goods(3609, 111, 1, 116, 1, filt=FILTER_ORBS, sort=9,
               limit=GOODS_NO_LIMIT),

        # ---- Summon Star Shards -----------------------------------------------
        # Reset strip is cropped here too, so these are uncapped for now.
        _goods(3401, 314, 1, 304, 1, filt=FILTER_STAR_SHARDS, sort=1),
        _goods(3402, 313, 1, 303, 1, filt=FILTER_STAR_SHARDS, sort=2),
        _goods(3403, 312, 1, 302, 1, filt=FILTER_STAR_SHARDS, sort=3),
        _goods(3404, 311, 1, 301, 1, filt=FILTER_STAR_SHARDS, sort=4),
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


# ---- selectors (`_action 7`) ----------------------------------------------
# A SELECTOR is a choose-your-reward box: "Choose 1 Bunrei of Limited Event Casts as
# you prefer." Two facts out of the binary explain everything about them:
#
#  1. `PanelItemInfo.OnPanelDirty` (0x15AA038) branches on `_action` when the Drop Info
#     popup opens. `_action 2` asks the BACKPACK (`RequesQueryBoxList`, cmd 129); but
#     `_action 7` asks the SHOP -- `PlayerShop.RequesQueryCouponList` (0x18058E8),
#     **ShopRpcServerCmd 0x111 = 273**, intargs [itemID]. We answered 129 and not 273,
#     so tapping a selector's magnifier sent a request nothing replied to and left the
#     modal overlay up with no way to dismiss it. That was the "I clicked display on
#     the awaker soulmirror+ selector box and the screen went dark" bug.
#     The reply is `HandleQueryCouponRply`, **ShopRpcClientCmd 0x211 = 529**, whose
#     strargs[0] is the same `List<List<uint>>` the box list uses.
#  2. `PlayerBackpack.GetItemSpace` (0x18EE554) has NO case for `_action 7`. A selector
#     can never occupy an inventory list, so it is never HELD -- which is why every one
#     of their descriptions ends "Remember to claim your reward from your mailbox".
#     There is therefore no in-client "use it now and pick" panel to drive: the choice
#     is resolved off-client. We resolve it at grant time instead (see grant_goods).
SELECTOR_ACTION = 7

# The seven Sins and the seven Virtues, in design order. `_action 1` character items
# whose char row is alignment 100 / 101 -- these are the ORIGINALS, not the costume
# variants (520xxx), which is what the live Drop Info for 1432 lists.
SIN_BUNREI = [510000, 510010, 510020, 510030, 510040, 510050, 510060]
VIRTUE_BUNREI = [510100, 510110, 510120, 510130, 510140, 510150, 510160]

# item id -> what the box lets you choose between. Only the selectors we actually sell
# need an entry; anything else answers with an empty list, which still RETURNS and so
# still dismisses the popup cleanly.
SELECTOR_POOLS = {
    1432: SIN_BUNREI,                       # ★5 Sin Bunrei Selector Box
    1500002: VIRTUE_BUNREI,                 # Virtue Bunrei Selector Box
    1500013: SIN_BUNREI + VIRTUE_BUNREI,    # Sin/Virtue Bunrei Selector Box
}


def selector_pool(item_id):
    """The item ids a selector offers, or [] if we do not model that one."""
    known = SELECTOR_POOLS.get(int(item_id))
    if known:
        return list(known)
    # Starshard selectors name their own contents ("★3 LR Starshard"), so they need no
    # hand-written pool -- without this, goal step 68 handed over nothing at all.
    any_suit = starshard_any_suit_box(item_id)
    if any_suit:
        return rune_any_suit_pool(*any_suit)
    return []


def is_selector(item_id):
    return (bt.dd.row("item", int(item_id)) or {}).get("_action") == SELECTOR_ACTION


def is_sellable(item_id):
    """A selector may only be listed once we know what it offers -- selling one with an
    empty pool would take the currency and hand back nothing."""
    return not is_selector(item_id) or bool(selector_pool(item_id))


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
    # Roll expired windows over BEFORE testing the cap, or a card bought out last month
    # still refuses today.
    expire_shop_bought(state, shop_id)
    rec = bought.get(str(goods_id)) or {"Count": 0, "Reset": 0}
    # Limit < 0 is the UNCAPPED encoding (see GOODS_NO_LIMIT); only a positive Limit
    # is a real cap. `if limit` alone would have treated -1 as a cap of -1 and refused
    # every purchase, which is the server half of the same bug.
    if limit > 0 and rec["Count"] + count > limit:
        return False, shop_id, f"over purchase cap {limit}", []

    if not spend_cost(state, cost_id, cost_cnt * count):
        return False, shop_id, f"cannot pay {cost_cnt * count}x item {cost_id}", []
    state.pop("_last_payout", None)
    new_chars, out_id, out_cnt = grant_goods(state, item_id, item_cnt * count)
    rec["Count"] += count
    # Stamp the window this purchase belongs to, so expire_shop_bought knows when to
    # clear it and the card's countdown strip has a time to show.
    if _cycle and not rec.get("Reset"):
        rec["Reset"] = next_reset_time(_cycle)
    bought[str(goods_id)] = rec
    state.setdefault("_last_purchase", {})[str(goods_id)] = [out_id, out_cnt]
    # A quest may be watching for exactly this purchase ("Go to Shop-Soul Altar and
    # exchange Grimoire of ★4 Awaker"). Its `_case_v1` is the GOODS id, so credit it
    # narrowly -- and only bump, never complete: the client rebuilds its claimable list
    # from the counter and the player claims through Quest cmd 257. The caller must push
    # a quest sync afterwards or the client never sees the counter move.
    bump_quest_counter(state, QUEST_CASE_BUY_GOODS, count, case_v1=int(goods_id))
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

    # A SELECTOR. It can never be held (`GetItemSpace` files no `_action 7`), and the
    # client has no panel that offers the choice -- in the live game the pick happens
    # off-client and the prize arrives by mail. Resolve it here instead: draw one of
    # the offered items and hand that over, reporting the real prize so the reveal
    # popup plays. Random rather than chosen, which is the honest limit of what the
    # client will let us do today; see SELECTOR_POOLS.
    if action == SELECTOR_ACTION:
        pool = selector_pool(item_id)
        if pool:
            uids, got = [], None
            for _ in range(max(1, amount)):
                got = rng.choice(pool)
                uids.extend(grant_goods(state, got, 1, rng)[0])
            return uids, got, max(1, amount)

    # A "Random ★N <suit>" box (1001..1030): roll a shard of that suit and star.
    star_suit = random_rune_box(item_id)
    if star_suit:
        pool = rune_star_suit_pool(*star_suit)
        if pool:
            from .gear import grant_rune       # local: gear imports core, not us
            got = None
            for _ in range(max(1, amount)):
                got = rng.choice(pool)
                grant_rune(state, got, rune_slot(got) or 1)
            return [], got, max(1, amount)

    # An ANY-SUIT box: "Random ★3 LR Starshard" and friends.
    any_suit = starshard_any_suit_box(item_id)
    if any_suit:
        pool = rune_any_suit_pool(*any_suit)
        if pool:
            from .gear import grant_rune       # local: gear imports core, not us
            got = None
            for _ in range(max(1, amount)):
                got = rng.choice(pool)
                grant_rune(state, got, rune_slot(got) or 1)
            return [], got, max(1, amount)

    # A random STARSHARD box: roll a real shard so the player gets something they can
    # actually equip, and report the one they got.
    bundle = RUNE_BUNDLES.get(int(item_id))
    if bundle:
        pool = rune_bundle_pool(*bundle)
        if pool:
            from .gear import grant_rune       # local: gear imports core, not us
            import collections as _c
            rolls = _c.Counter()
            for _ in range(max(1, amount)):
                got = rng.choice(pool)
                rolls[got] += 1
                grant_rune(state, got, rune_slot(got) or 1)
            # A MULTI-ROLL is itemized through _last_payout, so the client gets one
            # DropItemRply listing every shard actually rolled. Reply 513's own popup
            # can only carry a single (id, count) pair, and reporting the LAST roll x
            # amount there told a player their 10x buy paid ten copies of one shard --
            # while the server had granted ten different ones. Owner-reported.
            if sum(rolls.values()) > 1:
                state.setdefault("_last_payout", {})[str(item_id)] = \
                    sorted(rolls.items())
            return [], got, max(1, amount)

    # A random ★5+ cast orb.
    orb = CHAR_ORB_BUNDLES.get(int(item_id))
    if orb:
        star, alignments = orb
        pool = awaker_pool(star, alignments)
        if pool:
            got_item = None
            uids = []
            for _ in range(max(1, amount)):
                cid, got_item = rng.choice(pool)
                uids.append(add_char(state, cid, star=star))
            return uids, got_item, max(1, amount)

    # A fixed bundle: pay the amount printed on the card.
    payout = BUNDLE_PAYOUT.get(int(item_id))
    if payout:
        lines = []
        uids = []
        for real_id, per in payout:
            # **Recurse rather than grant_reward.** A bundle line can itself be a CAST
            # (the Evolution TUT Bundle contains ★4 Jacqueline), and grant_reward would
            # file an `_action 1` item into the bag, where GetItemSpace cannot place it
            # and the player never sees it. Going back through grant_goods routes each
            # line by its own `_action`, so casts reach the roster.
            sub_uids, _pid, _pcnt = grant_goods(state, real_id, per * amount, rng)
            uids.extend(sub_uids)
            lines.append((real_id, per * amount))
        state.setdefault("_last_payout", {})[str(item_id)] = lines
        head_id, head_per = payout[0]
        return uids, head_id, head_per * amount

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
        item_id, per = payout[0][0], payout[0][1] * per
    elif int(item_id) in CHAR_ORB_BUNDLES:
        pool = awaker_pool(*CHAR_ORB_BUNDLES[int(item_id)])
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
def next_reset_time(cycle, now=None):
    """When the current daily/weekly/monthly window ends, as an epoch second.

    `PlayerShop.GetShopGoodResetTime` hands `GoodsBuyData.Reset` straight back to the UI
    (and returns 0 for a row with no ResetCycle), so this field is a TIME, not a period
    counter -- it is what the card's "Daily/Weekly/Monthly Reset" strip counts down to.

    All three windows hinge on the same 4AM local boundary the daily passes and the free
    gacha pull already use (DAILY_RESET_HOUR), so a player's whole day rolls over at one
    moment: weekly on Monday 4AM, monthly on the 1st at 4AM.
    """
    now = int(now if now is not None else time.time())
    if not cycle:
        return 0
    t = time.localtime(now)
    # Start of today's window, then walk forward one window.
    start = time.struct_time((t.tm_year, t.tm_mon, t.tm_mday, DAILY_RESET_HOUR, 0, 0,
                              t.tm_wday, t.tm_yday, -1))
    day_start = int(time.mktime(start))
    if now < day_start:                       # before 4AM we are still in yesterday's
        day_start -= 86400
    if cycle == RESET_DAILY:
        return day_start + 86400
    if cycle == RESET_WEEKLY:
        wday = time.localtime(day_start).tm_wday          # 0 = Monday
        return day_start + (7 - wday) * 86400
    # Monthly: 4AM on the 1st of next month.
    d = time.localtime(day_start)
    year, mon = (d.tm_year + 1, 1) if d.tm_mon == 12 else (d.tm_year, d.tm_mon + 1)
    return int(time.mktime(time.struct_time(
        (year, mon, 1, DAILY_RESET_HOUR, 0, 0, 0, 1, -1))))


def _goods_cycles(state, shop_id):
    """{goods id: reset cycle} for one shop, so a bought-record can find its window."""
    goods = ((state.get("shop_goods") or {}).get(str(shop_id))
             or DEFAULT_SHOP_GOODS.get(str(shop_id)) or [])
    return {str(row[0]): row[3] for row in goods}


def expire_shop_bought(state, shop_id, now=None):
    """Clear the purchase counts of any card whose reset time has passed.

    Nothing did this before: `buy_shop_goods` bumped Count and never stamped Reset, so
    every "Daily/Weekly/Monthly Reset 0/N" cap in the game was permanent -- spend it once
    and that card was dead for good. -> True if anything was cleared.
    """
    now = int(now if now is not None else time.time())
    bought = (state.get("shop_bought") or {}).get(str(shop_id)) or {}
    if not bought:
        return False
    cycles = _goods_cycles(state, shop_id)
    changed = False
    for gid, rec in bought.items():
        cycle = cycles.get(str(gid), RESET_NONE)
        if not cycle:
            continue
        stamp = int(rec.get("Reset") or 0)
        if stamp and now < stamp:
            continue
        # No stamp at all means the record predates this machinery -- treat it as
        # expired rather than stranding the player on an old count forever.
        rec["Count"] = 0
        rec["Reset"] = next_reset_time(cycle, now)
        changed = True
    return changed


def _bought_wire(bought):
    return {gid: {"cnt": rec.get("Count", 0), "reset": rec.get("Reset", 0)}
            for gid, rec in (bought or {}).items()}


def shop_bought_json(state, shop_id):
    expire_shop_bought(state, shop_id)
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
    expire_shop_bought(state, shop_id)
    bought = _bought_wire((state.get("shop_bought") or {}).get(str(shop_id)) or {})
    goods = ((state.get("shop_goods") or {}).get(str(shop_id))
             or DEFAULT_SHOP_GOODS.get(str(shop_id)) or [])
    return [json.dumps(bought, separators=(",", ":")),
            json.dumps(goods, separators=(",", ":")),
            "{}"]


def goods_bundle_lines(state, goods_id):
    """As goods_payout_lines, but keyed by the GOODS id the client bought."""
    _sid, row = find_shop_goods(state, goods_id)
    return goods_payout_lines(state, row[5]) if row else []


def goods_payout_lines(state, item_id):
    """Every (item id, count) the last purchase of this card handed over.

    **A bundle should show ONE popup listing everything it dropped**, which reply 513
    cannot do -- `HandleShopBuy` reads exactly intargs[2]/[3], a single pair, with no
    loop. `PlayerBackpack.HandleDropItemRply` (BackpackRpcCmd.DropItemRply = 24) is the
    message built for it: strargs[0] is a `Dictionary<int,int>` of item id -> count and
    it builds one ItemStruct per entry into a single ItemPopupInfo. Returns [] for a
    plain single-item card, where 513's own popup is the right one.
    """
    return list((state.get("_last_payout") or {}).get(str(item_id)) or [])


def box_contents(item_id):
    """strargs[0] of the Drop Info reply (Backpack 129 -> 130): [[itemId, count], ...].

    `PanelItemInfo.GetIconDataInBox` (0x15AAC90) reads each inner list positionally --
    [0] ItemID, [1] ItemCount, and an optional [2] DropWeight (0 when the entry has
    fewer than three elements) -- so two-element rows are a complete answer.

    Contents come from the same tables the purchase pays out of, which is what keeps the
    preview honest: whatever Drop Info shows is exactly what buying hands over. A random
    box lists its whole pool, one entry per possibility, which is what the popup's
    scroll view is for.
    """
    item_id = int(item_id)
    if is_selector(item_id):
        return [[int(i), 1] for i in selector_pool(item_id)]
    payout = BUNDLE_PAYOUT.get(item_id)
    if payout:
        return [[int(i), int(c)] for i, c in payout]
    bundle = RUNE_BUNDLES.get(item_id)
    if bundle:
        element, star = bundle[0], bundle[1]
        if element:
            # One element, random slot -> its UR and LR set icons.
            ranks = starshard_set_items(element, star)
            return [[ranks[r], 1] for r in (RANK_UR, RANK_LR) if r < len(ranks)]
        # One slot, random element -> one set icon per element.
        out = []
        for el in STARSHARD_ELEMENTS:
            ranks = starshard_set_items(el, star)
            if len(ranks) > RANK_UR:
                out.append([ranks[RANK_UR], 1])
        return out
    orb = CHAR_ORB_BUNDLES.get(item_id)
    if orb:
        return [[int(iid), 1] for _cid, iid in awaker_pool(*orb)]
    return []

"""Gacha.

Split out of the former monolithic core.py; depends only on .core.
"""


import json, os
import battle as bt
import design_data as dd

from .core import (
    QUEST_CASE_GACHA,
    SOULFRAG_ACTION_RANGE,
    _daily_period,
    add_char,
    bump_quest_counter,
    grant_soulmirror,
    spend_cost,
)


# ---- gacha ----------------------------------------------------------------
# PlayerGacha.ReceiveSyncGacha (cmd 257) deserializes strargs[0] into
# List<UnlimitGachaBox> and ends with PanelLoadingWaiting.Close() -- so if it throws,
# the gacha screen sits on "Data Loading..." forever. An empty "[]" leaves the panel
# indexing an empty list.
#
# UnlimitGachaBox WIRE NAMES (from the [JsonProperty] thunks -- take the string on the
# ADRL, the BL that follows yields a bogus "f"):
#   BoxID->id  SortOrder->sort  ImgID->img  BannerID->banner  Type->type
#   SpLock->sp_lock  Newbie->newbie  Redraw->redraw  DrawSum->drawsum
#   LeftCount->left_cnt  MaxCount->max  DeltaTurn->dturn  IsAlreadyCheck->nflag
#   LeftCnts->data  CostV2Tbl->cost_tbl  DiscountTbl->dc_tbl  Daily->daily
#   DrawStepTbl->step_tbl  StepCostType->step_cost  EyeballTbl->eyeball_tbl
#   StepHint->step_hint  IsCharOnly->is_char_only  EndLeftTime->dendtime
#   PickUpCharDic->pick_up_tbl  CycleBonusTbl->cycle_bonus_tbl
#   missTACnt->missTA_cnt  goalTACnt->goalTA_cnt  WishCount->wish_cnt
#
# There is NO gacha design form in the pack (box definitions were live-ops server
# data), so this box is SYNTHESISED. Field VALUES below are best-effort, not
# recovered -- in particular `cost_tbl` / `step_tbl` layouts are unverified.
# The sync loop only dereferences box[i+1].cost_tbl when i+1 < len(boxes), so a single
# box avoids that path entirely.
# NOT 101 or 102 -- RouletteBoxId reserves those (BaseBox=101, ItemCostBox=102) and
# the client renders them as the roulette honeycomb board, not a summon banner.
GACHA_BOX_ID = 1001

# DesignSpriteForm rows for the banner art (icon/banner/atlas_banner_gachaNN) and the
# sidebar tabs (atlas_banner_gacha_tab01, sprites 1/2/3). Identified on screen:
#   761 = 魔界傳說召喚 (Sins -- Lucifer/Belphegor)   771 = 大罪魔王限定 tab
#   763 = 初デビューガチャ (the TUTORIAL banner)      773 = 初デビュー tab
#   762 =  ?                                          772 = 魔星召喚 (Awaker) tab
# 764-769 are further banners (gacha04..08), 774-779 more tab sprites.
# NOTE ids 761/771 (Sins) and 766/776 (Soul Mirror) are NOT in the design `sprite` table
# -- there is no row at all -- so boxes using them rendered as duplicate fallback art and
# their tabs did nothing. Every id below is verified present in `sprite`; check any new
# one with dd.row("sprite", id) before using it.
# Identified in game 2026-08-05 via SEVENSINS_BANNER_PREVIEW (see BANNER_SPRITE_IDS):
#   762/772 all-★5 Sins+Virtues+Riders ("Limbo")   767/777 Awaker
#   764/774 Sin Soulmirrors                        765/775 Virtue Soulmirrors
#   763/773 Debut (tutorial)   768/778 unidentified   871/873/875/877 revival art
# There is NO Sins-only / Virtues-only / Riders-only banner art in the pack, so those
# banners were evidently retired before EoS. Ids 761/771 and 766/776 have no sprite row
# at all -- using them is what produced dead duplicate tabs.
SPR_BANNER_SINS, SPR_TAB_SINS = 762, 772               # the combined ★5 banner
SPR_BANNER_ENCHANTED, SPR_TAB_ENCHANTED = 767, 777    # "In the Enchanted Stars"
SPR_BANNER_AWAKER, SPR_TAB_AWAKER = 767, 777           # same art, older label
SPR_BANNER_DEBUT, SPR_TAB_DEBUT = 763, 773
SPR_BANNER_ANGEL, SPR_TAB_ANGEL = 765, 775             # Virtue Soulmirrors
SPR_BANNER_SOULMIRROR, SPR_TAB_SOULMIRROR = 764, 774   # Sin Soulmirrors
SPR_BANNER_DEBUT_ALT = 768                             # unidentified art
# Reused for the Rider Soulmirror banner. There is no Riders-only art in the pack (see
# above), so this is the only unclaimed pair that has real `sprite` rows -- better a
# generic banner than a missing one, which is what 761/771 and 766/776 produced.
SPR_BANNER_RIDER, SPR_TAB_RIDER = 768, 778
SPR_BANNER_BIGTHREE = 769                              # 御三家 100-pull limited

# All banner sprites identified (via SEVENSINS_BANNER_PREVIEW=1, which publishes one
# box per candidate so they can be flipped through in a single run):
#   761 大罪召喚 Grand Sins        762 魔星召喚 Awaker Summon
#   763 初デビューガチャ tutorial   764 美德天使限定 Virtue Angels
#   765 天使召喚 Angel Summon      766 魂鏡召喚 Soul Mirror (Sins)
#   767 魂鏡召喚 Soul Mirror (Angels)                769 御三家 Big Three, 100-pull
# Every sprite row that actually exists on a gacha banner/tab atlas, enumerated from the
# design `sprite` form rather than guessed. The old candidate list included 769/779,
# which have NO row -- the same mistake that left 761/771 and 766/776 rendering as dead
# duplicate tabs. Run the server with SEVENSINS_BANNER_PREVIEW=1 to publish one box per
# pair and see them all in game.
BANNER_SPRITE_IDS = [762, 763, 764, 765, 767, 768, 871, 873, 875, 877]
TAB_SPRITE_IDS = [772, 773, 774, 775, 777, 778, 780, 872, 874, 876, 878]
BANNER_CANDIDATES = [
    (BANNER_SPRITE_IDS[i % len(BANNER_SPRITE_IDS)], TAB_SPRITE_IDS[i])
    for i in range(len(TAB_SPRITE_IDS))
]
# The gacha panel prefab has this many draw-slot widgets; LeftCnts must match exactly.
GACHA_UI_SLOTS = 10
GACHA_COST_ITEM = 205      # Awaker Scroll
GACHA_COST_AMOUNT = 10


def _gacha_box(box_id, img, banner, costs, *, redraw=0, newbie=0, sort=1,
               char_only=True, daily=0):
    """One UnlimitGachaBox. Every array field is non-null on purpose (the panel indexes
    them), except `data` (LeftCnts) which MUST stay null for a summon banner."""
    return {
        "id": box_id, "sort": sort, "img": img, "banner": banner,
        "type": 1, "sp_lock": 0, "newbie": newbie, "redraw": redraw,
        # **LeftCount (`left_cnt`) > 0 is what marks a box DRAW-LIMITED** -- not MaxCount.
        # ReceiveSyncGacha (RVA 0x18FE5B0) walks the boxes and, for any box with
        # LeftCount > 0, requires that it is NOT last and that the next box's Redraw and
        # CostV2Tbl length are compatible; otherwise it logs
        #   "有抽數限制的轉蛋箱(boxID=N)不能是最後一箱!"
        # from inside TitanStack.NetCore.poll, aborting the rest of the message batch --
        # which is how a bad gacha sync took the STORE down with it. A box with
        # LeftCount <= 0 skips every one of those checks.
        #
        # **LeftCount must be -1, the UNLIMITED sentinel** -- not 0. The draw button's
        # enable test is `LeftCount == -1 || LeftCount >= needed` (UpdateGachaInformation
        # around 0x1572690: `CMN W8,#1` then `CSET GE`), so 0 greys every button; while
        # ReceiveSyncGacha treats anything <= 0 as unlimited, so 0 and -1 both dodge the
        # crash. Only -1 satisfies both. 999 dodges the greying but IS draw-limited and
        # crashes when such a box is last.
        "drawsum": 0, "left_cnt": -1, "max": 999,
        "dturn": [], "nflag": 1,
        "data": None,
        "cost_tbl": costs, "dc_tbl": [],
        "daily": daily, "step_tbl": [], "step_cost": 0, "eyeball_tbl": [],
        "step_hint": 0, "is_char_only": char_only, "dendtime": 0,
        "pick_up_tbl": {}, "cycle_bonus_tbl": [],
        "missTA_cnt": 0, "goalTA_cnt": 0, "wish_cnt": 0,
    }


# Cost rows are [drawType, costCat, itemId, price]; drawType 1 = a single pull, anything
# else = a 10-pull, and the button widget index is row[0] + 2*row[1] - 3.
def _cost(single, ten, item=None, price_single=1, price_ten=10, cat=1):
    item = GACHA_COST_ITEM if item is None else item
    rows = []
    if single:
        rows.append([1, cat, item, price_single])
    if ten:
        rows.append([2, cat, item, price_ten])
    return rows


# The banner has FOUR buttons, index = drawType + 2*costCat - 3:
#   (1,1)->0 single for diamonds   (2,1)->1 ten for diamonds
#   (1,2)->2 single for a ticket   (2,2)->3 ten for tickets
# left-to-right on screen, confirmed against a live "Love, Around the Globe" banner
# showing 120 / 1000 diamonds and 1 / 10 tickets.
#
# CAUTION: a first attempt at four rows hung the panel on "Waiting" (2026-08-05). That
# attempt also priced the single at **0** (an Awaker-style free daily pull) and set
# Daily=1. Those are the suspects, not the row count -- a zero price and the daily-free
# flag both imply server state the panel then waits on. Keep prices non-zero.
GACHA_DIAMOND_ITEM = 1          # CurrencyType.Cash
GACHA_SINGLE_DIAMOND_PRICE = 120
GACHA_TEN_DIAMOND_PRICE = 1000

# "In the Enchanted Stars" is bought with ONE currency and nothing else. Item 1400009
# 邊境之遺 "Limbo Legacy" says so itself: "Can be obtained in Main Story Normal stages,
# Hard 5-10, 6-10, 7-10, etc. Used for summoning In the Enchanted Stars gacha box." So
# this box gets neither the gem rows nor a scroll -- two rows, 80 and 800, which is what
# the live banner charged.
GACHA_LIMBO_LEGACY_ITEM = 1400009
LIMBO_LEGACY_SINGLE_PRICE = 80
LIMBO_LEGACY_TEN_PRICE = 800

# Published pull rates. The remaining 88.5% is 3★ fodder.
GACHA_RATE_5 = 0.025
GACHA_RATE_4 = 0.09

# ---- per-banner cast pools -------------------------------------------------
#
# `_alignment` separates the cast: 100 Sins / 101 Virtues / 102 Riders (the ★5 story
# cast), 103 ★5 Awakers, 104 ★4 Awakers, 9001 ★3 fodder. The generic banner rolls
# everything; a NAMED banner rolls its own slice with its featured casts rate-upped.
#
# **All of this is invented.** No DesignGacha* form ships in the pack, so pools, rates
# and pickups are ours -- but the FEATURED CASTS are not guesses: they are read off the
# banner artwork. `atlas_banner_gacha06_en` ("In the Enchanted Stars") pictures
# ★5 SEERE "Foxy Scryer" (10501) and ★5 NONNA "Lightning Rider" (10581), both
# alignment 103, and captions itself "Chance to get ★5 Cast / 10-Summon & Get 1 ★4".
# That is an Awaker banner, so its pool is 103/104 rather than the whole cast.
GACHA_PICKUP_SHARE = 0.5      # share of the ★5 slot reserved for the featured casts
# `filler` is the tier the remaining ~88.5% of pulls draws from, as (alignments, star).
# It is per-banner because a banner bought with a SCARCE currency should not be paying
# out ★3 minions: 3101 costs Limbo Legacy, which only drops from a handful of stages, so
# its filler is the ★4 Awakers instead. That makes every non-★5 pull on that banner a
# ★4 -- deliberate, and the reason the tier is data rather than the hardcoded (9001,).
CAST_POOL_DEFAULT = {"star5": (100, 101, 102, 103), "star4": (104,), "pickup": (),
                     "filler": ((9001,), 3)}
CAST_POOLS = {
    3101: {"star5": (103,), "star4": (104,), "pickup": (10501, 10581),
           "filler": ((104,), 4)},
}


def gacha_free_available(state):
    """Is today's free daily pull still unused? Shares the 4AM period with the passes."""
    return state.get("gacha_free_day") != _daily_period()


def use_gacha_free(state):
    state["gacha_free_day"] = _daily_period()


def _cost_standard(scroll_item, free_single=False):
    """The standard four: gems single/ten, then this banner's own scroll single/ten.

    The Awaker banner's first slot renders as "Free / Daily Limited" while a daily pull
    is unused and reverts to the plain gem price once spent -- that is the box's `Daily`
    flag, NOT a zero-priced row, so the gem price belongs here either way.
    """
    rows = (_cost(True, True, GACHA_DIAMOND_ITEM,
                  price_single=GACHA_SINGLE_DIAMOND_PRICE,
                  price_ten=GACHA_TEN_DIAMOND_PRICE, cat=1)
            + _cost(True, True, scroll_item, price_single=1, price_ten=10, cat=2))
    if free_single:
        # The free pull is a 100% DISCOUNT, not a zero price. GetDiscountOFFV reads the
        # full price from row[3] and the CURRENT price from row[DiscountTbl[i] - 1], or
        # from the row's LAST element when DiscountTbl does not cover that row. So
        # appending a 0 makes current=0, base=120 -> floor((120-0)*100/120) = 100% off.
        # GachaButtonHandler.SetData only shows _goFree/_goDaily when discount >= 1, so
        # a bare price of 0 (what we tried first, and what hung the panel) can never
        # work -- it leaves discount at 0 and skips the whole branch.
        rows[0] = rows[0] + [0]
    return rows


def _cost_limbo_legacy():
    """The Enchanted Stars pair: single and ten, Limbo Legacy only.

    costCat **2**, the bag-item category, because 1400009 is `_action 0` -- an ordinary
    backpack item like the 202/203 scrolls, not a currency (item 1/2 carry `_action 5`
    ITEM_ACTION_CURRENCY and live in `currency`). Category 1 would point the client's
    own affordability test at the wrong wallet and grey both buttons out.

    Cost: this puts them at button indices 2 and 3 (`drawType + 2*costCat - 3`) rather
    than 0 and 1, so they sit in the right-hand pair with the gem slots empty. Cosmetic;
    revisit if the gap looks wrong in game.
    """
    return _cost(True, True, GACHA_LIMBO_LEGACY_ITEM,
                 price_single=LIMBO_LEGACY_SINGLE_PRICE,
                 price_ten=LIMBO_LEGACY_TEN_PRICE, cat=2)


# Boxes that do NOT take the standard gems+scroll set. Keyed by box id so the
# REGULAR_BOXES rows stay uniform.
COST_OVERRIDES = {3101: _cost_limbo_legacy}


# After the tutorial pull the newbie box is replaced by the standing banners, which is
# what the live game shows: Awaker Summon / Grand Sins Pick Up / Soulmirrors Limited.
# (box_id, banner sprite, tab sprite, name, char_only). Soul Mirror is the ITEM gacha
# -- 魂鏡 are items -- so it is the one banner that must NOT be char_only, since that
# flag is what makes ShowGachaAnim skip to the result screen instead of playing the
# character summon animation.
# (box_id, banner sprite, tab sprite, name, char_only, scroll item). Each banner is
# bought with ITS OWN scroll. Only three permanent scrolls exist -- 202 Awaker
# (魔星召喚卷軸), 203 Sin (大罪魔王召喚卷軸), 205 Premium Awaker -- there is no item 204 and
# no standing Virtue scroll at all; every Virtue/Angel one is an event or revival item
# (1400412 "Virtue Scroll (Revisited)" is the closest), and Soulmirror scrolls are all
# per-character. Those two are best-guess picks, not confirmed from footage.
#
# The trailing flag is UnlimitGachaBox.Daily: the Awaker banner alone grants one free
# pull a day, which the client renders over the FIRST (gem, single) slot as
# "Free / Daily Limited" and which falls back to the normal 120-gem price once spent.
# It is a flag on the box, never a zero-priced cost row -- pricing that row 0 is what
# hung the panel on "Waiting".
REGULAR_BOXES = [
    # One cast banner covering every unit, bought with Awaker Scrolls, carrying the
    # daily free pull. Uses the combined ★5 art (all three factions) rather than the
    # Awaker-specific art, since the pool is not Awaker-only.
    # **Box id 3101 is not arbitrary: it is what quest 32001 asks for.** Netherworld
    # Note step 35 is "Summon 1 Cast in [In the Enchanted Stars] gacha box", case 13
    # with `_case_v1` 3101. No DesignGacha* form ships in the design pack at all -- box
    # definitions were live-ops data -- so every banner here is ours to number, and
    # giving the standing cast banner the id the goal names makes the step reachable as
    # designed instead of fudging the counter. (3444/3450/3471, "Rise of Solar Prime",
    # are event banners we do not run.)
    #
    # **The ART for it DID ship**: `icon/banner/atlas_banner_gacha06_en`, sprite 767,
    # captioned "In the Enchanted Stars" and featuring 10501 SEERE "Foxy Scryer" and
    # 10581 NONNA "Lightning Rider". It was sitting unused under the name
    # SPR_BANNER_AWAKER -- the box was live data, the atlas was not.
    (3101, SPR_BANNER_ENCHANTED, SPR_TAB_ENCHANTED,
     "In the Enchanted Stars", True, 202, 0),
    # The all-cast banner stays: it is the ONLY way to roll Sins, Virtues and Riders,
    # since 3101 is Awaker-only (see CAST_POOLS). It keeps the daily free pull.
    (1002, SPR_BANNER_SINS, SPR_TAB_SINS, "Summon", True, 202, 1),
    # Soulmirrors are the ITEM gacha (魂鏡 are items), so char_only must be False or
    # ShowGachaAnim skips straight to the results screen.
    (1004, SPR_BANNER_SOULMIRROR, SPR_TAB_SOULMIRROR,
     "Sin Soulmirrors", False, 1400430, 0),
    (1005, SPR_BANNER_ANGEL, SPR_TAB_ANGEL,
     "Virtue Soulmirrors", False, 1400414, 0),
    # No Rider banner: gacha09_en covers "Virtues & Riders" (see SOULMIRROR_GACHA_BOXES).
    # That the Riders have no scroll of their own -- the 1400400 block has Sin and Virtue
    # entries and nothing for 102 -- was the clue, read the wrong way round at the time.
]


def gacha_json(state):
    """The box list for the gacha sync (cmd 257).

    The standing banners are ALWAYS present; the tutorial box is prepended (sort 1, so
    it is the one selected) only until it has been used. The live screen shows Debute
    Summon alongside Awaker Summon / Grand Sins during the tutorial, and just the
    standing set afterwards.
    """
    if os.environ.get("SEVENSINS_BANNER_PREVIEW"):
        return json.dumps(
            [_gacha_box(2000 + i, img, tab, _cost_standard(202), sort=i + 1)
             for i, (img, tab) in enumerate(BANNER_CANDIDATES)],
            separators=(",", ":"))

    boxes = []
    if not state.get("gacha_count"):
        boxes.append(_gacha_box(GACHA_BOX_ID, SPR_BANNER_DEBUT, SPR_TAB_DEBUT,
                                _cost(False, True, price_ten=GACHA_COST_AMOUNT),
                                redraw=1, newbie=1, sort=1))
    free = gacha_free_available(state)
    n = len(boxes)
    for i, (bid, img, ban, _name, co, scroll, dly) in enumerate(REGULAR_BOXES):
        override = COST_OVERRIDES.get(bid)
        costs = (override() if override
                 else _cost_standard(scroll, free_single=bool(dly) and free))
        boxes.append(_gacha_box(bid, img, ban, costs,
                                sort=n + i + 1, char_only=co, daily=dly))
    return json.dumps(boxes, separators=(",", ":"))


def gacha_box_json(state, box_id=None):
    """The single UnlimitGachaBox object for the box being drawn (cmd 258 strargs[0]
    wants one box, not a list).

    **It MUST be the box actually drawn, not just the first in the list.**
    `PlayerGacha.ReceiveDraw` (0x18FECE4) deserializes this into `curGachaBox` and
    derives `isItemBox = !IsCharOnly` from it, which `PanelGacha.OnGachaDraw` hands to
    `ShowGachaAnim`. When that flag is false the client runs the CHARACTER draw
    animation -- and `GachaDrawingUI.InitGachaAnim` (0x17149CC) dereferences every
    `DrawData.charData` with no null check, which is null for a GachaObjectType.Item
    row. So describing a Soulmirror pull with the cast banner's `is_char_only: True`
    threw a NullReferenceException and left the "Waiting" overlay up forever.
    """
    import json as _j
    boxes = _j.loads(gacha_json(state))
    box = boxes[0]
    if box_id is not None:
        box = next((b for b in boxes if b.get("id") == int(box_id)), box)
    return _j.dumps(box, separators=(",", ":"))


# GachaObjectType: Char=1, Item=2, StepBonus=3, CycleBonus=4.
#
# Row shapes, read off `PanelGacha.OnGachaDraw` (0x1576530) -- the two types are bounds
# checked DIFFERENTLY:
#   type 1 Char : needs Count > 3; reads row[1] = char id and row[3] = star, then
#                 `CharData(id, star, 1, 0, ...)`.
#   type 2 Item : needs Count > 2; reads row[1] = item id and row[2] = amount, then
#                 `ItemIconData(item, amount, ...)` -> DrawData.
#   types 3/4   : same 3-element shape, collected into the step/cycle bonus list.
# A 4-element row satisfies both, so every row here stays [type, id, amount_or_1, star].
GACHA_OBJ_CHAR = 1
GACHA_OBJ_ITEM = 2

# The two Soulmirror banners are ITEM gachas (魂鏡 are items, see
# [[sevensins-soulmirrors]]), split by the OWNING character's `_alignment`:
# 100 Sins / 101 Virtues / 102 Riders. A mirror's `_param3` names its character.
#
# **TWO banners, not three -- the artwork says so.** `atlas_banner_gacha09_en` is
# captioned "SOUL MIRRORS SUMMON / Virtues & Riders" and pictures Esmira, Rider of
# Death, alongside Gabriel, Metatron and Uriel. So the Riders (alignment 102) ride on
# the Virtue banner; they never had one of their own. An invented 1006 was the wrong
# reading of "alignment 102 has mirrors but no box".
SOULMIRROR_GACHA_BOXES = {1004: (100,), 1005: (101, 102)}
# Mirror grade (`_param2`): 1 普通 N / 2 優良 R / 3 稀有 SR / 4 史詩 UR / 5 傳說 LR.
# Weighted to match the published char rates so the banner's advertised odds stay
# coherent; the real per-banner table was live-ops data we do not have.
SOULMIRROR_GACHA_GRADES = ((5, GACHA_RATE_5), (4, GACHA_RATE_4))
SOULMIRROR_GACHA_FALLBACK_GRADE = 3


def _mirror_owning_chars():
    """-> {charId: alignment} for every character that owns a Soulmirror.

    **Only characters with a real `char` row.** Six `_param3` values own mirrors but have
    no row at all -- 0 (60 mirrors that are not character-bound) plus 20851/20861/20911/
    20931/20951 -- and granting one would hand the client a character it cannot look up:
    the Soulmirror panel filters every list by the cast being viewed, and
    GetTransmutePredictText does DesignCharForm.GetRow(charId) outright. They were
    excluded before only as a side effect of matching on the char form; that is now the
    stated rule.
    """
    owners = {}
    for _iid, row in (bt.dd.rows("item") or {}).items():
        if row.get("_action") not in SOULFRAG_ACTION_RANGE:
            continue
        cid = int(row.get("_param3") or 0)
        if cid in owners:
            continue
        char = bt.dd.row("char", cid) or {}
        if char:
            owners[cid] = char.get("_alignment")
    return owners


_shared_mirror_chars_cache = None


def soulmirror_shared_chars():
    """Characters whose mirrors ride on EVERY banner.

    The three boxes are keyed to the three ★5 casts, which leaves the ★4 (alignment 103,
    34 characters) and ★3 (alignment 104, 15) casts owning mirrors with no banner of
    their own -- 49 characters whose Soulmirrors were unobtainable by any route. Rather
    than invent three more banners for casts that never had one, their mirrors are added
    to all three pools, so any banner can produce them.
    """
    global _shared_mirror_chars_cache
    if _shared_mirror_chars_cache is None:
        # values are TUPLES of alignments (1005 carries Virtues AND Riders), so flatten
        # -- comparing an int against a set of tuples matches nothing and made every
        # cast "shared", which collapsed both banners into the same pool.
        banner_casts = {a for v in SOULMIRROR_GACHA_BOXES.values() for a in v}
        _shared_mirror_chars_cache = {
            cid for cid, alignment in _mirror_owning_chars().items()
            if alignment not in banner_casts}
    return _shared_mirror_chars_cache


def _soulmirror_gacha_pool(alignment):
    """{grade: [item id, ...]} drawable from the banner for `alignment`.

    That cast's own mirrors plus the shared ones (see soulmirror_shared_chars).
    """
    wanted = (alignment,) if isinstance(alignment, int) else tuple(alignment)
    chars = {cid for cid, a in _mirror_owning_chars().items() if a in wanted}
    chars |= soulmirror_shared_chars()
    pool = {}
    for iid, row in (bt.dd.rows("item") or {}).items():
        if row.get("_action") in SOULFRAG_ACTION_RANGE and row.get("_param3") in chars:
            pool.setdefault(int(row.get("_param2") or 0), []).append(int(iid))
    return pool


def gacha_is_soulmirror_box(box_id):
    return int(box_id) in SOULMIRROR_GACHA_BOXES


def gacha_is_redraw_box(box_id):
    """Only the tutorial box re-rolls; every other banner commits on the draw itself.

    There is no separate commit command for a regular `DrawV2` -- `RedrawBoxDoGetDraw`
    (20) exists only for the redraw box -- so treating every draw as pending meant
    normal pulls were rolled, displayed and then silently dropped.
    """
    return int(box_id) == GACHA_BOX_ID


def _draw_soulmirrors(state, count, alignment):
    """Roll `count` soulmirror items as GachaObjectType.Item rows."""
    import random
    pool = _soulmirror_gacha_pool(alignment)
    if not pool:
        return []
    results = []
    for _ in range(count):
        r = random.random()
        grade = SOULMIRROR_GACHA_FALLBACK_GRADE
        acc = 0.0
        for g, rate in SOULMIRROR_GACHA_GRADES:
            acc += rate
            if r < acc:
                grade = g
                break
        bucket = pool.get(grade) or pool.get(SOULMIRROR_GACHA_FALLBACK_GRADE)
        if not bucket:
            bucket = next(iter(pool.values()))
        results.append([GACHA_OBJ_ITEM, random.choice(bucket), 1, grade])
    return results


def gacha_commit(state):
    """Commit the pending redraw results: grant the characters and charge the cost.

    A `redraw` box is the tutorial's "play this round till you feel good" gacha -- the
    player re-rolls for free and only the roll they keep is committed, via RedrawSave
    (server cmd 21). So the draw itself must NOT grant or charge.
    """
    pending = state.get("gacha_pending") or []
    if not pending:
        return []
    # Charge FIRST and bail if the player cannot afford it -- otherwise running out of
    # scrolls silently yields free pulls. Use the price of the button actually pressed,
    # recorded by gacha_draw; the old code always charged the same scroll regardless of
    # whether the player paid in gems or picked a single pull.
    item_id, price = state.get("gacha_pending_cost") or (GACHA_COST_ITEM,
                                                         GACHA_COST_AMOUNT)
    if price and not spend_cost(state, item_id, price):
        return []
    state["gacha_pending_cost"] = None
    for row in pending:
        if row[0] == GACHA_OBJ_ITEM:
            # Soulmirrors are equipment, not stackable items: one storage-3 entry per
            # copy, rolled with the same attr scheme make_soulmirror uses.
            for _ in range(max(1, int(row[2]))):
                grant_soulmirror(state, row[1])
        else:
            add_char(state, row[1], star=row[3])
    state["gacha_count"] = state.get("gacha_count", 0) + 1
    # **`_case_v1` on case 13 is the BOX id, a discriminator** -- 32001 names 3101 and
    # the "Rise of Solar Prime" rows name 3444/3450/3471. Bumping on the case id alone
    # credited every gacha quest in the game off a single pull on any banner (the same
    # trap case 2003 documents). Bump the generic family (v1 0) plus this box.
    #
    # Count SUMMONS, not button presses: "[Daily] Summon 10 times" (10035, case 13
    # `_case_cnt` 10) is satisfied by one ten-pull, which is plainly how it reads. One
    # pending row IS one summon on both paths -- casts and Soulmirrors alike -- so the
    # row count is the pull count.
    pulls = len(pending)
    bump_quest_counter(state, QUEST_CASE_GACHA, pulls, case_v1=0)
    box = state.pop("gacha_pending_box", None)
    if box:
        bump_quest_counter(state, QUEST_CASE_GACHA, pulls, case_v1=int(box))
    state["gacha_pending"] = []
    return pending


def gacha_draw(state, count=10, cost=None, box_id=None):
    """Roll `count` results for `box_id` and return the rows.

    A Soulmirror banner rolls ITEMS instead of characters -- same reply shape, different
    GachaObjectType (see the notes on SOULMIRROR_GACHA_BOXES).

    The drop table was live-ops data we do not have, so this picks real DesignCharForm
    rows by rarity -- the tutorial banner advertises a guaranteed 4* in a 10-pull.
    Result row = **[objectType, id, count, star]** -- PanelGacha.OnGachaDraw bounds
    checks `_size > 1` then `> 3` and builds
    `CharData(id = row[1], star = row[3], level = 1, plus = 0, ...)`. Two-element rows
    threw "Index was outside the bounds of the array".
    """
    import random
    if box_id is not None and gacha_is_soulmirror_box(box_id):
        results = _draw_soulmirrors(state, count,
                                    SOULMIRROR_GACHA_BOXES[int(box_id)])
        state["gacha_pending"] = results
        state["gacha_pending_cost"] = list(cost) if cost else None
        state["gacha_pending_box"] = int(box_id)
        return results
    rows = bt.dd.rows("char")
    # Playable cast lives in the 10000..19999 id band (Lucifer 10001, Leviathan
    # 10011); outside it are mobs/skins/etc, which must not be rolled.
    # The client does CharData.ctor -> _growStar[star - 1] on the RAW array, so a row
    # whose entries are 0 yields "DesignCharGrowForm row ID 0 not found". Some ids in
    # the band (e.g. 10032) are placeholders with an all-zero ladder -- the list is
    # non-empty, so a plain truthiness check lets them through. Require a real entry.
    # `_alignment` separates the cast; this banner rolls AWAKERS plus fodder:
    #   100 Sins / 101 Virtues / 102 Riders  -- 5* story cast, NOT in this pool
    #   103 = 5* Awakers (28)   104 = 4* Awakers (15)   9001 = 3* fodder (168)
    # (The pack's `_rarity` column reads 4/3/2 for those three; the in-game display
    # rarity is one higher, which is why the wiki lists align 103 as 5*.)
    #
    # `_order == 0` rows in the 1xxxx band are "分靈" spirit duplicates of the main
    # cast (10000 = 路西法分靈), not cast members -- rolling them produced cards that
    # rendered greyed out and never appeared in the formation list.
    #
    # Observed tutorial spread: 1x 5*, 1x 4*, 8x 3* fodder, duplicates normal.
    # Buckets are by DISPLAY star, which is CharRareMinStar(_rarity), not _rarity:
    #   align 100/101/102 Sins+Virtues+Riders  _rarity 5 (LE)  -> ★5
    #   align 103         Awakers              _rarity 4 (SSR) -> ★5
    #   align 104                              _rarity 3 (SR)  -> ★4
    #   align 9001        fodder               _rarity 2 (R)   -> ★3
    # The single cast banner covers EVERY unit, so ★5 spans all four of the first group
    # -- it used to be Awakers only, which silently excluded the 73 real ★5 casts.
    pool = CAST_POOLS.get(int(box_id or 0), CAST_POOL_DEFAULT)
    fill_aligns, fill_star = pool.get("filler", CAST_POOL_DEFAULT["filler"])

    def _bucket(alignments, listed=True):
        return [r for r in rows.values()
                if r.get("_alignment") in alignments
                and (bool(r.get("_order")) or not listed)
                and any(r.get("_growStar") or [])]

    # 9001 fodder is unlisted (_order 0); the cast alignments are not, so a filler tier
    # made of real casts still has to pass the listed check.
    five, four = _bucket(pool["star5"]), _bucket(pool["star4"])
    fod = _bucket(fill_aligns, listed=fill_aligns != (9001,))
    pickup = [r for r in (rows.get(str(cid)) or rows.get(cid)
                          for cid in pool.get("pickup", ()))
              if r and any(r.get("_growStar") or [])]
    if not (five and four and fod):
        return []
    # The TUTORIAL roll is scripted -- 1x 5★, 1x 4★, 8x fodder -- and must stay that way;
    # it is the spread the scripted first-pull sequence is written around. The gate is
    # the same one gacha_json uses to decide whether to show the newbie box at all.
    if not state.get("gacha_count"):
        plan = [(five, 5), (four, 4)] + [(fod, fill_star)] * (count - 2)
    else:
        # Published rates: 5★ 2.5%, 4★ 9%, 3★ 88.5%, with 4★-or-better GUARANTEED in a
        # 10-pull. Rolled per pull -- reusing the tutorial's fixed spread made every
        # later 10-pull identical.
        plan = []
        for _ in range(count):
            r = random.random()
            if r < GACHA_RATE_5:
                # Rate-up: half of the ★5 slot goes to the banner's featured casts,
                # split evenly between them (see GACHA_PICKUP_SHARE).
                plan.append((pickup if (pickup and random.random()
                                        < GACHA_PICKUP_SHARE) else five, 5))
            elif r < GACHA_RATE_5 + GACHA_RATE_4:
                plan.append((four, 4))
            else:
                plan.append((fod, fill_star))
        if count >= 10 and not any(star >= 4 for _b, star in plan):
            plan[random.randrange(len(plan))] = (four, 4)
    results = []
    for bucket, star in plan:
        row = random.choice(bucket)
        raw = row.get("_growStar") or []
        # never point at an empty rung -- the client indexes _growStar[star-1] raw
        use = star if (len(raw) >= star and raw[star - 1]) else _gacha_star(row)
        results.append([GACHA_OBJ_CHAR, row["_id"], 1, use])
    random.shuffle(results)
    # held until the player keeps this roll (see gacha_commit)
    state["gacha_pending"] = results
    state["gacha_pending_cost"] = list(cost) if cost else None
    state["gacha_pending_box"] = int(box_id) if box_id is not None else None
    return results


def _gacha_star(char_row):
    """Star tier whose RAW _growStar slot is non-zero. battle._default_star now reads
    the raw ladder too (and caps at MAX_STAR, so it can never return a super rung), so
    this is that same rule."""
    return bt._default_star(char_row)


def gacha_cost_row(state, box_id, draw_index):
    """-> (item id, price, pull count) for a draw request.

    `curDrawIndex` (PlayerGacha 0x6C) arrives as a **1-based index into that box's
    cost_tbl** -- confirmed live: pressing the 1000-gem ten-pull sent [1002, 2] and row
    2 is [2, 1, 1, 1000]. Row = [drawType, costCat, itemId, price], drawType 1 = single.
    """
    for box in json.loads(gacha_json(state)):
        if box["id"] != int(box_id):
            continue
        rows = box.get("cost_tbl") or []
        i = int(draw_index) - 1
        if 0 <= i < len(rows):
            row = rows[i]
            draw_type, _cat, item_id, price = row[:4]
            # A row with a trailing extra element is discounted -- its CURRENT price is
            # that last element, which is 0 for the free daily pull. Charge what the
            # player is actually shown, not the struck-through base price.
            if len(row) > 4:
                price = row[-1]
            return item_id, price, (1 if draw_type == 1 else 10)
        break
    return None, 0, 10

"""Character progression: level training, battle XP, karma gifts, rank up, transcend, ultra transcend, skill up, skill inherit.

Split out of the former monolithic core.py; depends only on .core.
"""


import json, math
import battle as bt
import design_data as dd
import settings

from .core import (
    CURRENCY_COIN,
    MAX_STAR,
    SUPER_LIMIT_DEFINE,
    _char_data_json,
    char_rarity,
    grant_karma,
    has_item,
    spend_diamonds,
    spend_item,
    uid,
)



# ---- Skill Inherit (CharRpc char_limit_impart, 288 -> 544) -----------------
# `impartLimit` is a [JsonProperty] List<int> on GeneralSyncData, indexed by
# `alignment - 100`, and it is what PanelCharacterUpgrade.InitSkillInheritTip renders as
# "Number of Inherit Remaining (<group>)". We were sending [] , so the panel read 0 and
# the Inherit buttons were dead. There are exactly 5 alignment groups (100..104), which
# is also the length of CharDefine's two inherit arrays.
ALIGNMENT_BASE = 100
ALIGNMENT_COUNT = 5

# Both extracted from the CharDefine .cctor InitializeArray blobs in global-metadata.dat
# (see tools/metadata_blob.py). The same run recovered SellMoneyStar as
# [100, 250, 500, 1500, 3000, 15000], whose 3-star entry of 500 matches what we measured
# in-game -- that is the check that says the extraction is right.
INHERIT_MATERIAL_IDS = (12, 21, 13, 14, 23)        # Inherit Gem (Sin/Virtue/Rider/Awaker)
INHERIT_DIAMOND_COSTS = (2000, 1500, 1500, 1000, 500)
INHERIT_COST_ITEM, INHERIT_COST_DIAMOND = 1, 2     # RequestLimitImpart costType
INHERIT_ITEM_COUNT = 1                             # the panel always charges exactly 1
CURRENCY_CASH = 1                                  # CurrencyType.Cash -- free diamonds
MIN_INHERIT_RARITY = 4                             # "Only casts of ★4 rarity or better"

# How many inherits we grant per group. The real value was server-authoritative and is
# not recoverable from the client, so this is OUR choice, not a recovered constant.
DEFAULT_IMPART_LIMIT = 99


def char_alignment(char_id):
    row = dd.row("char", char_id) or {}
    return int(row.get("_alignment") or 0)


def impart_limit(state):
    lst = state.get("impartLimit")
    if not isinstance(lst, list) or len(lst) != ALIGNMENT_COUNT:
        lst = [DEFAULT_IMPART_LIMIT] * ALIGNMENT_COUNT
        state["impartLimit"] = lst
    return lst


def inherit_char(state, in_uid, out_uid, cost_type):
    """Transfer one skill level from mentor (out_uid) to apprentice (in_uid).

    -> (ok, apprentice_book, mentor_book, mentor_char, reason). The three ints are
    exactly what reply 544 carries, because receivedLimitImpart (RVA 0x169BC44) writes
    charDic[strargs[0]].limit_book = intargs[0],
    charDic[strargs[1]].limit_book = intargs[1] and .limit_char = intargs[2].
    Note it never writes the apprentice's limit_char, so the gain HAS to land on the
    apprentice's book counter or the client would not show it.
    """
    app = state["roster"].get(in_uid)
    men = state["roster"].get(out_uid)
    if not app or not men or in_uid == out_uid:
        return False, 0, 0, 0, "missing cast"
    # Help screen: "you will need two casts with Same Rarity, Skill Lv. +2 or above,
    # and Diamonds / Inherit Gems", plus "Only casts of ★4 rarity or better can transfer
    # skill levels" on the panel itself. Mentor and apprentice do NOT have to be the same
    # character -- the text says "passed on to ANOTHER cast", and the client happily
    # sends cross-character requests. The "same name" wording in the confirm popup is
    # just loose localisation of "the mentor is consumed".
    a_rare, m_rare = char_rarity(app["id"]), char_rarity(men["id"])
    if a_rare < MIN_INHERIT_RARITY or m_rare < MIN_INHERIT_RARITY:
        return False, 0, 0, 0, "rarity below 4"
    if a_rare != m_rare:
        return False, 0, 0, 0, f"rarity mismatch ({a_rare} vs {m_rare})"

    in_party = {u for f in state.get("formations", []) for u in f.get("array", []) if u}
    if out_uid in in_party:
        return False, 0, 0, 0, "mentor is in a formation"

    idx = char_alignment(app["id"]) - ALIGNMENT_BASE
    if not 0 <= idx < ALIGNMENT_COUNT:
        return False, 0, 0, 0, "cast has no inherit group"

    remaining = impart_limit(state)
    if remaining[idx] <= 0:
        return False, 0, 0, 0, "no inherits remaining for this group"

    a_book, a_char = int(app.get("limit_book", 0)), int(app.get("limit_char", 0))
    m_book, m_char = int(men.get("limit_book", 0)), int(men.get("limit_char", 0))
    if m_book + m_char <= 0:
        return False, 0, 0, 0, "mentor has no skill levels to give"
    if a_book >= MAX_LIMIT_BOOK or a_book + a_char >= MAX_TOTAL_LIMIT:
        return False, 0, 0, 0, "apprentice is at the cap"

    if cost_type == INHERIT_COST_DIAMOND:
        cost = INHERIT_DIAMOND_COSTS[idx]
        # spend_diamonds, not a plain key-1 debit: the client prices this against
        # free + paid (PlayerCurrency.Balance), so must we.
        if not spend_diamonds(state, cost):
            return False, 0, 0, 0, "not enough diamonds"
    else:
        item = INHERIT_MATERIAL_IDS[idx]
        if not spend_item(state, item, INHERIT_ITEM_COUNT):
            return False, 0, 0, 0, f"no Inherit Gem (item {item})"

    # Mentor pays out of the cast-derived counter first, keeping its Grimoire count.
    if m_char > 0:
        m_char -= 1
    else:
        m_book -= 1
    a_book += 1

    app["limit_book"], app["limit_char"] = a_book, a_char
    men["limit_book"], men["limit_char"] = m_book, m_char
    remaining[idx] -= 1
    return True, a_book, m_book, m_char, ""


def general_json(state):
    """PlayerGeneral.GeneralSyncData. Keys here happen to match the field names
    (checked in the [JsonProperty] thunks), which is the exception, not the rule."""
    return json.dumps({
        "acc": state["name"], "uid": uid(state), "ptype": 1,
        "pid": uid(state), "name": state["name"], "context": "", "callnum": "",
        "login": 0, "lastLogin": 0, "firstDiff": 0,
        "designTimeStamps": {}, "impartLimit": impart_limit(state), "bpSets": {},
        "helperUseLimit": 0, "monthScore": 0, "runeRerollMode": 0,
    }, separators=(",", ":"))


# ---- Level Training (PanelCharacterUpgrade, CharRpcServerCmd.char_level_up 278) -----
# `RequestLevelUp(target_uid, material_numbers)` sends intargs = how many of each
# trainer tier to feed, strargs = [uid]. Trainers are plain backpack items 101..105
# (★1..★5 Trainer); the tier index in intargs is the item id minus 101.
TRAINER_ITEM_IDS = (101, 102, 103, 104, 105)

# UpgradeDefine.LevelUpMaterialOfferXp, indexed [tier - 1]:
# Formula.GetCharLVupFoodXP(tier, n) = LevelUpMaterialOfferXp[tier - 1] * n.
# The values live in global-metadata.dat rather than libil2cpp.so (an InitializeArray
# blob), recovered by locating the array in the metadata file. Index 1 = 3600 is
# confirmed in game: one ★2 Trainer previews exactly 3600 EXP. The 6th slot exists in
# the array but the panel only exposes five tiers.
LEVEL_UP_MATERIAL_XP = (1725, 3600, 9000, 22500, 60000, 150000)

# Flat per-trainer coin cost, confirmed in game at 1000. Matches the panel's own hint:
# "Each tier's trainer costs equally; using trainers of higher rarity first will cost
# you less Coins" -- i.e. the cost is per trainer consumed, not per EXP gained.
TRAINER_COIN_COST = 1000

# Formula.GetCharLVupNeedXP(rare, nowLv, maxLv): XP to go from nowLv to nowLv+1.
#   floor( (c*(lv+1)^e + 4*c^2) * (1 + 0.15*(lv+1)/30) )
# with (c, e) selected by the char's design `_rarity`. Verified end to end: summing
# levels 1..39 for a rarity-3 cast gives 561845, exactly the "Requires 561845 EXP to
# reach Max Lv.40" the panel shows for Jacqueline.
_LVUP_COEF = {1: (1, 2.3), 2: (3, 2.3), 3: (4, 2.5), 4: (5, 2.5), 5: (5, 2.7)}


def char_lvup_need_xp(rarity, lv):
    """XP required to advance a cast of this design rarity from `lv` to `lv` + 1."""
    coef, exp = _LVUP_COEF.get(rarity, (0, 2.3))
    return math.floor((coef * ((lv + 1) ** exp) + 4 * coef * coef)
                      * (1 + 0.15 * (lv + 1) / 30))


# ---- battle XP ("Exp Up!" on the results screen) ---------------------------
# BattleReward.barValues (our `bar_list`) is a List<BarData>
# {uid, id, olv, lv, oxp, xp, omax, max} -- the SHORT wire keys, not the C# field
# names (see grant_battle_xp). Sending [] is why clearing
# a stage never levelled anyone.
#
# How much XP a stage pays is NOT in the client data -- there is no exp column on the
# stage row and no Formula.GetStageExp -- so, like the drop table, it was live-ops data
# and the number is ours. Scale it off the stage's own level and wave count so later
# chapters pay more while the ~lv^2.3 level curve still slows progression down.
STAGE_XP_PER_LEVEL_PER_WAVE = 40


def stage_battle_xp(stage_row, waves):
    # NOTE the battle_xp rate is the one whose 1.0 is NOT a reconstruction: the real
    # per-stage XP is not in the pack, so 1.0 means "our best guess", not "what retail
    # paid". See settings.RATES.
    return settings.scale(
        STAGE_XP_PER_LEVEL_PER_WAVE * max(int(stage_row.get("_stagelv") or 1), 1)
        * max(int(waves), 1), "battle_xp")


def apply_char_xp(entry, add):
    """Add XP to one roster entry, mirroring Formula.GetCharTrialCalculAddXp exactly
    (RVA 0x18F5AA4): level up while the cast is not at MaxLv and the banked XP covers
    the next level, then keep the remainder. -> (old_lv, old_xp, old_max, new_max).
    """
    rarity = char_rarity(entry["id"])
    max_lv = char_max_lv(entry)
    old_lv = int(entry.get("lv") or 1)      # present-but-None guards, as char_max_lv
    old_xp = int(entry.get("xp") or 0)
    old_max = char_lvup_need_xp(rarity, old_lv)

    lv, xp = old_lv, old_xp + int(add)
    if old_lv <= max_lv:
        while lv != max_lv:
            need = char_lvup_need_xp(rarity, lv)
            if need <= 0 or xp < need:
                break
            xp -= need
            lv += 1
    else:
        xp = old_xp                      # already over cap: bank nothing
    entry["lv"], entry["xp"] = lv, xp
    return old_lv, old_xp, old_max, char_lvup_need_xp(rarity, lv)


def grant_battle_xp(state, party, add):
    """Award `add` XP to each party member. -> [BarData, ...] for bar_list."""
    bars = []
    for member in party:
        uid = member.get("uid") if isinstance(member, dict) else None
        entry = state["roster"].get(uid) if uid else None
        if not entry:
            continue
        old_lv, old_xp, old_max, new_max = apply_char_xp(entry, add)
        # **The wire keys are SHORT and do not match the C# field names.**
        # `tools/json_keys.py --class BarData` (2.2.4): oldLV->olv, newLV->lv,
        # oldXP->oxp, newXP->xp, oldMAXXP->omax, newMAXXP->max. Sending the field
        # names bound nothing, so every bar arrived as zeros -- which reads on the
        # results panel as "no XP gained" with every party member pinned at max.
        bars.append({
            "uid": uid, "id": entry["id"],
            "olv": old_lv, "lv": int(entry["lv"]),
            "oxp": float(old_xp), "xp": float(entry["xp"]),
            "omax": float(old_max), "max": float(new_max),
        })
    return bars


def char_max_lv(entry):
    """CharData.get_MaxLv = plus + 10*star + 5*super_star.

    `star` is stored as None to mean "use the character's rarity default" -- _seed_roster
    seeds it that way and _char_data_json resolves it via bt._default_star before the
    client ever sees it. So this MUST resolve it the same way: `.get("star", 0)` returned
    None and crashed int() (the tutorial's post-battle XP grant on a fresh account), while
    a naive `or 0` would give a cap of 0 and silently stop a new character ever leveling.
    """
    row = bt.dd.row("char", entry["id"]) or {}
    star = entry.get("star")
    star = bt._default_star(row) if star is None else star
    return (int(entry.get("plus") or 0)
            + 10 * int(star or 0)
            + 5 * int(entry.get("super_star") or 0))


def level_up_char(state, uid, counts):
    """Feed trainers to one cast. `counts` is the per-tier list straight off the wire.

    Returns (ok, spent_coins, consumed) -- consumed is [(item id, n), ...]. Refuses the
    whole operation if the player cannot pay, rather than partially applying it."""
    entry = state["roster"].get(uid)
    if not entry:
        return False, 0, []
    consumed = [(TRAINER_ITEM_IDS[i], int(n))
                for i, n in enumerate(counts[:len(TRAINER_ITEM_IDS)]) if n]
    if not consumed:
        return False, 0, []
    total = sum(n for _i, n in consumed)
    cost = total * TRAINER_COIN_COST
    if int(state["currency"].get(str(CURRENCY_COIN), 0)) < cost:
        return False, 0, []
    for item_id, n in consumed:
        if not has_item(state, item_id, n):
            return False, 0, []

    gained = sum(LEVEL_UP_MATERIAL_XP[TRAINER_ITEM_IDS.index(i)] * n
                 for i, n in consumed)
    rarity = (dd.row("char", entry["id"]) or {}).get("_rarity")
    max_lv = char_max_lv(entry)
    lv, xp = int(entry.get("lv", 1)), int(entry.get("xp", 0)) + gained
    while lv < max_lv:
        need = char_lvup_need_xp(rarity, lv)
        if need <= 0 or xp < need:
            break
        xp -= need
        lv += 1
    if lv >= max_lv:
        xp = 0                      # capped: no XP banked past the ceiling
    entry["lv"], entry["xp"] = lv, xp

    for item_id, n in consumed:
        spend_item(state, item_id, n)
    state["currency"][str(CURRENCY_COIN)] = \
        int(state["currency"].get(str(CURRENCY_COIN), 0)) - cost
    return True, cost, consumed


# ---- Karma gifts (Consonance, CharRpcServerCmd.gift 307) -------------------
# `RequestGift(charID, sendGiftAmountList)` sends intargs =
# [charID, itemId, amount, itemId, amount, ...] and no strargs. Note it identifies the
# cast by CHAR ID, not by roster uid, unlike level up / unsummon.
#
# A gift item is any `item` row with `_action == 3`; `_param2` is its base karma XP
# (10 for nearly all of them, 20 for Popular Manga, 100 for Popular Poster) and
# `_param1` is its gift group, which equals the item's own id for all 148 of them.
#
# The multipliers are plain `const`s on CharDefine, so they are inlined in dump.cs and
# needed no metadata digging: FirstFavoriteGiftExpRate = 10 applies when the gift is
# the cast's `_favoriteGift`, SecondFavoriteGiftExpRate = 2 when it merely matches its
# `_favoriteGiftGroup`. In this build every cast has `_favoriteGift ==
# _favoriteGiftGroup` and every gift is its own group, so the x2 tier is currently
# unreachable -- it is implemented anyway because the data could change.
GIFT_ITEM_ACTION = 3
FIRST_FAVORITE_GIFT_EXP_RATE = 10
SECOND_FAVORITE_GIFT_EXP_RATE = 2
MAX_RECIEVE_GIFT_TYPE_NUM = 15      # CharDefine.MaxRecieveGiftTypeNum
MAX_RECIEVE_GIFT_NUM = 200          # CharDefine.MaxRecieveGiftNum


def gift_karma_xp(char_id, item_id, amount=1):
    """Karma XP a cast gains from `amount` of one gift item."""
    item = dd.row("item", item_id) or {}
    if item.get("_action") != GIFT_ITEM_ACTION:
        return 0
    base = int(item.get("_param2") or 0)
    char = dd.row("char", char_id) or {}
    if item_id == char.get("_favoriteGift"):
        rate = FIRST_FAVORITE_GIFT_EXP_RATE
    elif item.get("_param1") and item.get("_param1") == char.get("_favoriteGiftGroup"):
        rate = SECOND_FAVORITE_GIFT_EXP_RATE
    else:
        rate = 1
    return base * rate * int(amount)


def give_gifts(state, char_id, pairs):
    """Feed gifts to one cast. `pairs` is [(item id, amount), ...] off the wire.

    Returns (ok, total karma xp, consumed, rank rewards paid). Refuses outright if the
    player does not hold the items, rather than partially applying.

    One feed can cross SEVERAL Karma ranks at once, and every rank crossed pays -- see
    core.karma_rank_rewards, which collects the whole open range rather than just the
    rank landed on."""
    wanted = [(int(i), int(n)) for i, n in pairs if n > 0]
    if not wanted:
        return False, 0, [], []
    if len(wanted) > MAX_RECIEVE_GIFT_TYPE_NUM:
        return False, 0, [], []
    if sum(n for _i, n in wanted) > MAX_RECIEVE_GIFT_NUM:
        return False, 0, [], []
    for item_id, n in wanted:
        if not has_item(state, item_id, n):
            return False, 0, [], []
    total = sum(gift_karma_xp(char_id, i, n) for i, n in wanted)
    if total <= 0:
        return False, 0, [], []
    for item_id, n in wanted:
        spend_item(state, item_id, n)
    # A gift can cross a Karma rank, and those ranks PAY (see core.karma_rank_rewards).
    # The paid lines ride back so the caller can push the syncs they landed in.
    karma = grant_karma(state, char_id, total)
    return True, total, wanted, list(karma.get("_paid") or [])


# ---- Rank Up (PanelCharacterUpgrade, CharRpcServerCmd.char_rank_up 279) -----
# `RequestRankUp(target_uid)` sends strargs = [uid] and NO intargs. Reply is cmd 535,
# which shares receivedOneCharAndRemove with level up: strargs[0] = updated CharData.
#
# Everything below is keyed by the cast's STAR, not its rarity -- read straight out of
# the client, so no rarity/star ambiguity this time:
#   Formula.GetCharRankupCost(star) is a jump table: 3 -> 0xC350, 4 -> 0x249F0,
#     5 -> 0x7A120, else 0.
#   InitRankUpMaterialInfo computes idx = (star == 6 ? 5 : star - 1) and reads
#     UpgradeDefine.<static int[]>[idx] for the count, with the item id hardcoded as
#     #0x22C = 556 (Evolution Gem). That array is a metadata blob:
#     [0, 0, 525, 1200, 3000, 0].
# Both tables confirmed in game: star 3 = 525 gems / 50000 coins, star 4 = 1200 /
# 150000, star 5 = 3000 / 500000.
#
# The two leading zeros are not gaps -- CharDefine.MinRankUpStar = 3, so a cast below
# star 3 cannot rank up at all. star 6 is max and routes to SUPER rank up instead
# (cmd 289), which needs GeneralSyncGameRuleData.superRankUpMaterial/superRankUpCost --
# tables we do not currently send, so that path is not implemented.
RANKUP_GEM_ITEM_ID = 556                        # Evolution Gem
RANKUP_GEM_BY_STAR = (0, 0, 525, 1200, 3000, 0)   # indexed [star - 1]
RANKUP_COIN_BY_STAR = {3: 50000, 4: 150000, 5: 500000}
MIN_RANKUP_STAR = 3                             # CharDefine.MinRankUpStar


def rank_up_cost(star):
    """-> (gem count, coin cost) to take a cast from `star` to `star` + 1."""
    try:
        gems = RANKUP_GEM_BY_STAR[int(star) - 1]
    except (IndexError, TypeError, ValueError):
        gems = 0
    return gems, RANKUP_COIN_BY_STAR.get(int(star or 0), 0)


def rank_up_char(state, uid):
    """Spend Evolution Gems + coins to raise one cast's star by 1.

    Returns (ok, gems, coins). Refuses outright rather than partially applying."""
    entry = state["roster"].get(uid)
    if not entry:
        return False, 0, 0
    star = int(entry.get("star") or 0)
    if star < MIN_RANKUP_STAR or star >= MAX_STAR:
        return False, 0, 0
    gems, coins = rank_up_cost(star)
    if gems <= 0:
        return False, 0, 0
    if not has_item(state, RANKUP_GEM_ITEM_ID, gems):
        return False, 0, 0
    if int(state["currency"].get(str(CURRENCY_COIN), 0)) < coins:
        return False, 0, 0
    spend_item(state, RANKUP_GEM_ITEM_ID, gems)
    state["currency"][str(CURRENCY_COIN)] = \
        int(state["currency"].get(str(CURRENCY_COIN), 0)) - coins
    entry["star"] = star + 1
    # MaxLv = plus + 10*star + 5*super_star, so the cap rises with the star. The level
    # itself is untouched -- the panel advertises "Max Lv. 40 > 50", not a level gain.
    return True, gems, coins


# ---- Transcend (PanelCharacterUpgrade, CharRpcServerCmd.char_plus_up 280) ----
# "Transcend" in EN is PLUS UP internally (the panel calls it Surmount). It feeds whole
# CASTS in as material to earn plus-XP; each full bar raises `plus`, and since
# MaxLv = plus + 10*star + 5*super_star with CharDefine.MaxPlus = 30, that is what turns
# "Max Lv. 60" into "Max Lv. 60 /90" on the panel.
#
# Request: cmd 280, strargs = [target uid, ...material uids], no intargs. Reply 536 is
# receivedOneCharAndRemove again -- strargs[0] = updated CharData, strargs[1..] = the
# consumed material uids to delete from charDic.
#
# All three formulas are client-side and verified against the panel:
#   Formula.GetCharPlusupFoodXP(star) = ((star <= 2) ? star : star + 1) ** 2
#       -> star 3 = 16, star 4 = 25, matching the +16/+25 badges on the material cards.
#   Formula.GetCharPlusupCost(star)   = that same square * 100  (coins per material)
#   Formula.GetCharPlusupNeedXP(plus) = floor(32 + 2 * (plus + 1) ** 1.64), 0 at plus 30
#       -> plus 0 = 34, exactly the "Req. 34pt" the panel shows.
#
# GetPlusUpFoodOfferPXP / GetPlusUpFoodNeedMoney first consult a per-char-id override
# dictionary on PlayerGeneral and only fall back to the formulas above when it has no
# entry. We never send that table, so the fallback is always taken -- which is correct
# for us, but note a live account could see different numbers for specific material ids.
MAX_PLUS = 30                    # CharDefine.MaxPlus


def _plusup_square(star):
    s = int(star or 0)
    return (s if s <= 2 else s + 1) ** 2


# "Consume materials of the same cast type will grant 50% bonus EXP" -- the panel's own
# hint. "Cast type" is the design row's `_job`, confirmed against every badge on the
# Transcend screen for a job-3 target: job-3 materials read 24/54/73 at star 3/5/6
# (16/36/49 x 1.5, floored) while job-2 and job-4 materials of the same star read the
# plain 16/25/49. The bonus is EXP ONLY -- the coin cost stayed at square x 100
# (1600/3600/4900) regardless of whether the badge was boosted.
SAME_JOB_PXP_BONUS = 1.5


def plusup_food_pxp(star, same_job=False):
    """Plus-XP one material cast of this star contributes.

    `same_job` applies the 50% same-cast-type bonus, floored."""
    base = _plusup_square(star)
    return math.floor(base * SAME_JOB_PXP_BONUS) if same_job else base


def char_job(char_id):
    return (dd.row("char", char_id) or {}).get("_job")


def plusup_food_cost(star):
    """Coins charged for consuming one material cast of this star."""
    return _plusup_square(star) * 100


def char_plusup_need_xp(plus):
    """Plus-XP to go from `plus` to `plus` + 1."""
    if int(plus) >= MAX_PLUS:
        return 0
    return math.floor(32 + 2 * ((int(plus) + 1) ** 1.64))


def transcend_char(state, uid, material_uids):
    """Feed material casts to raise `plus`. Returns (ok, consumed, pxp, coins)."""
    entry = state["roster"].get(uid)
    if not entry:
        return False, [], 0, 0
    mats = [m for m in material_uids
            if m and m != uid and m in state["roster"]]
    if not mats:
        return False, [], 0, 0
    # A cast committed to a formation must not be eaten -- same rule the Unsummon
    # panel enforces via CompiledCharDic.
    in_party = {u for f in state.get("formations", []) for u in f.get("array", []) if u}
    if any(m in in_party for m in mats):
        return False, [], 0, 0

    target_job = char_job(entry["id"])
    pxp = coins = 0
    for m in mats:
        mat = state["roster"][m]
        star = mat.get("star") or 0
        same = target_job is not None and char_job(mat["id"]) == target_job
        pxp += plusup_food_pxp(star, same)
        coins += plusup_food_cost(star)      # cost ignores the same-job bonus
    if int(state["currency"].get(str(CURRENCY_COIN), 0)) < coins:
        return False, [], 0, 0

    plus, banked = int(entry.get("plus", 0)), int(entry.get("pxp", 0)) + pxp
    while plus < MAX_PLUS:
        need = char_plusup_need_xp(plus)
        if need <= 0 or banked < need:
            break
        banked -= need
        plus += 1
    if plus >= MAX_PLUS:
        banked = 0
    entry["plus"], entry["pxp"] = plus, banked

    for m in mats:
        del state["roster"][m]
    state["currency"][str(CURRENCY_COIN)] = \
        int(state["currency"].get(str(CURRENCY_COIN), 0)) - coins
    return True, mats, pxp, coins


# ---- Ultra Transcend (super limit) -----------------------------------------
# Reached through the 15th subsystem, PlayerJSAgent -- the super-limit UI is implemented
# in Puerts JavaScript, not C#, so it does not use a CharRpc command at all:
#   request  index 0x8E0D82FD cmd 290
#            strings[0] = ["PlayerChar"]  (JS module name)
#            strings[1] = [target uid, ...duplicate material uids]
# Only duplicates of the SAME char id are valid material ("Consuming duplicate
# Casts/Bunreis will grant Ultra Levels"), and only rarity-5 casts can Ultra Transcend
# at all (CharData.get_isSuperLimitAvailable == `_rarity == 5`).
#
# Calibrated from the panel: each duplicate grants +5 Ultra Levels and costs 3,000,000
# coins, so four duplicates take a cast 0 -> 20 (MAX) for 12,000,000. The displayed
# effect (+5/+10/+15/+20%) is superLimitEffect = personal_effect * lv / 10.
ULTRA_LEVELS_PER_MATERIAL = 5
ULTRA_COIN_COST_PER_MATERIAL = 3_000_000


def ultra_transcend(state, uid, material_uids):
    """Consume duplicate casts to raise `super_limit`. -> (ok, consumed, levels, coins)."""
    entry = state["roster"].get(uid)
    if not entry:
        return False, [], 0, 0
    row = dd.row("char", entry["id"]) or {}
    if row.get("_rarity") != 5:                      # isSuperLimitAvailable
        return False, [], 0, 0
    cap = int(SUPER_LIMIT_DEFINE.get("max_super_limit", 0))
    cur = int(entry.get("super_limit", 0))
    if cur >= cap:
        return False, [], 0, 0

    in_party = {u for f in state.get("formations", []) for u in f.get("array", []) if u}
    mats = [m for m in material_uids
            if m and m != uid and m in state["roster"]
            and state["roster"][m]["id"] == entry["id"]   # duplicates only
            and m not in in_party]
    if not mats:
        return False, [], 0, 0

    coins = ULTRA_COIN_COST_PER_MATERIAL * len(mats)
    if int(state["currency"].get(str(CURRENCY_COIN), 0)) < coins:
        return False, [], 0, 0

    gained = ULTRA_LEVELS_PER_MATERIAL * len(mats)
    entry["super_limit"] = min(cap, cur + gained)
    for m in mats:
        del state["roster"][m]
    state["currency"][str(CURRENCY_COIN)] = \
        int(state["currency"].get(str(CURRENCY_COIN), 0)) - coins
    return True, mats, entry["super_limit"] - cur, coins


# ---- Skill Up / "Skill Awaken" (CharRpc char_limit_up, 281 -> 537) ---------
# Skill rank IS `limit` (= limit_book + limit_char); see char_limit(). Materials are
# CHAR uids, not items -- duplicates of the cast, or a Grimoire, which is itself a
# character row rather than an inventory item.
#
# All of the following is read out of PanelCharacterUpgrade.SetSkillAwakenCost and
# GetMaterialAddLimitSum (RVA 0x18AD91C / 0x18B0A60), not guessed:
#
#   per material, keyed on CharData.Type (== the design char row's `_type`):
#     type 1 or 2 -> book += mat.limit_book      ; char += mat.limit_char + 1
#     otherwise   -> book += mat.limit_book + 1  ; char += mat.limit_char
#
# `_type` 1 is a PLAYABLE cast (every roster entry we hold is type 1) and 2 is the
# 極・大罪ノ書 Ultimate Grimoire, so those two raise `limit_char`; `_type` 3 is the plain
# 大罪ノ書 Grimoire and raises `limit_book` -- hence the panel's "Grimoire x/9" counter
# reading limit_book. Do NOT go by design row 1002 "Lucifer": that is a mob variant
# (type 7). The playable Lucifer is id 10001.
#   new_total = min(limit_book + limit_char + book + char, MAX_TOTAL_LIMIT)
#   cost      = (new_total - old_total) * GetCharLimitupCost(rarity)
#
# So a material is worth +1 limit AND carries over whatever limit it had already
# accumulated -- feeding an already-awakened duplicate is not wasted.
MAX_TOTAL_LIMIT = 13          # Mathf.Min(..., 13) in SetSkillAwakenCost
MAX_LIMIT_BOOK = 9            # "Each cast can consume up to 9 Grimoire/Bunreis in total"

# Design `_type` values that take the first branch above: playable casts and the
# Ultimate Grimoire. Everything else (plain Grimoire, gremlins, mob rows) falls through.
LIMIT_MATERIAL_CHAR_SIDE = (1, 2)


def char_limitup_cost(rarity):
    """Formula.GetCharLimitupCost (RVA 0x18F6220) is literally 1200 * 5^(rare-1):
    30,000 for a 3-star, 150,000 for a 4-star, 750,000 for a 5-star -- PER limit
    level gained, not per material."""
    return 1200 * 5 ** (max(int(rarity or 1) - 1, 0))


def char_type(char_id):
    row = dd.row("char", char_id) or {}
    return int(row.get("_type") or 0)


def skill_up(state, uid, material_uids):
    """Consume casts/Grimoires to raise skill rank. -> (ok, consumed, gained, coins)."""
    entry = state["roster"].get(uid)
    if not entry:
        return False, [], 0, 0
    mats = [m for m in material_uids if m and m != uid and m in state["roster"]]
    if not mats:
        return False, [], 0, 0
    in_party = {u for f in state.get("formations", []) for u in f.get("array", []) if u}
    if any(m in in_party for m in mats):
        return False, [], 0, 0

    cur_book = int(entry.get("limit_book", 0))
    cur_char = int(entry.get("limit_char", 0))
    cur = cur_book + cur_char
    if cur >= MAX_TOTAL_LIMIT:
        return False, [], 0, 0

    add_book = add_char = 0
    for m in mats:
        mat = state["roster"][m]
        mb, mc = int(mat.get("limit_book", 0)), int(mat.get("limit_char", 0))
        if char_type(mat["id"]) in LIMIT_MATERIAL_CHAR_SIDE:
            add_book += mb
            add_char += mc + 1
        else:
            add_book += mb + 1
            add_char += mc

    new_total = min(cur + add_book + add_char, MAX_TOTAL_LIMIT)
    gained = new_total - cur
    if gained <= 0:
        return False, [], 0, 0
    coins = gained * char_limitup_cost(char_rarity(entry["id"]))
    if int(state["currency"].get(str(CURRENCY_COIN), 0)) < coins:
        return False, [], 0, 0

    # Clamp exactly the way UpdateSkillAwakenInfo (RVA 0x18B13B8) does: the book side
    # caps at 9 on its own, then the cast side gets whatever is left under 13. Keeping
    # the two counters separate matters because the panel renders limit_book by itself
    # as "Grimoire x/9".
    #
    # NOTE the displayed "Skill Lv." is NOT limit_book + limit_char -- the panel adds
    # `superStar + limit_suit` on top, which is why a cast with both counters at 0 can
    # still show SKL +6. Do not try to reconcile the two numbers.
    book = min(cur_book + add_book, MAX_LIMIT_BOOK)
    entry["limit_book"] = book
    entry["limit_char"] = max(min(cur_char + add_char, MAX_TOTAL_LIMIT - book), 0)
    for m in mats:
        del state["roster"][m]
    state["currency"][str(CURRENCY_COIN)] = \
        int(state["currency"].get(str(CURRENCY_COIN), 0)) - coins
    return True, mats, gained, coins


def char_data_json(state, uid):
    """One CharData as a JSON string -- what receivedOneCharAndRemove expects in
    strargs[0] for every single-cast update (level up, rank up, plus up, limit up)."""
    entry = state["roster"].get(uid)
    if not entry:
        return "{}"
    return json.dumps(_char_data_json(uid, entry), separators=(",", ":"))


def char_create_json(state, uids):
    """strargs[0] for Char `create` (529) -- `Dictionary<uid, CharData>`.

    `receivedCreateChar` (0x16992F0) walks the dictionary and AddChar's each entry, so
    the KEY is the roster uid and the value is the same CharData shape every other
    single-cast reply uses. Unknown uids are skipped rather than emitting a null the
    deserialiser would choke on.
    """
    return json.dumps(
        {uid: _char_data_json(uid, state["roster"][uid])
         for uid in uids if uid in state["roster"]},
        separators=(",", ":"))

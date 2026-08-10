#!/usr/bin/env python3
"""Persistent per-account player state.

Until now every sync reply was a hardcoded constant, so nothing survived a restart
and nothing could ever change -- spending currency, pulling gacha or clearing a stage
had nowhere to be recorded. This module owns the mutable facts and renders them into
the JSON payloads the client's sync handlers expect.

Wire-format notes live next to each builder; the key names are NOT the C# field names
(they come from [JsonProperty] on the client types) so don't rename them casually:
  Energy    : type / energy / energy_cap / refresh_time_bias, dict keyed by EnergyType
  Currency  : plain dict keyed by CurrencyType
  Level     : type / uid / lv / xp / xp_cap / lv_min / lv_max, keyed by LevelSetting uid
  CharData  : pro_chars / group_tbl / id_tbl / formations / acPeriod / ...
  Backpack  : "sid" (LuaTableConverter) inside BackpackItemsData
"""
import json, math, os, threading, time

import battle as bt
import design_data as dd

# Relocatable: an Android host app cannot write next to the code and sets
# SEVENSINS_ACCOUNTS, which always wins. The desktop DEFAULT is server/accounts --
# anchored on the PACKAGE's parent (this file is player_state/core.py, so two dirnames
# up), NOT on this file's own directory. This module used to live at server/player_state.py
# where one dirname was correct; after the package split, dirname(__file__) is
# server/player_state/, and anchoring there silently wrote a fresh account into
# player_state/accounts/ instead of loading the real server/accounts/1000001.json.
_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_DIR = os.environ.get("SEVENSINS_ACCOUNTS") or os.path.join(_SERVER_DIR, "accounts")

# EnergyType: Action=1 (stamina), Arena=16, ArenaSP=17, ArenaTeam=18
ENERGY_ACTION, ENERGY_ARENA, ENERGY_ARENA_SP, ENERGY_ARENA_TEAM = 1, 16, 17, 18
# CurrencyType: Cash=1, Mira=16, RealCash=32, DMMCash=48, GuildPoints=64
CUR_CASH, CUR_MIRA, CUR_REAL, CUR_DMM, CUR_GUILD = 1, 16, 32, 48, 64

PLAYER_LEVEL_UID = "player_level"      # LevelDefine..cctor, LevelType.Player = 1

# How many saved teams the client expects. PanelCharacterList.OnPreviousClick wraps
# `_teamIndex - 1 < 0` round to the literal 5, so the teams are indexed 0..5 and
# PlayerCharData.formations must hold SIX entries -- GetTeamCharListByIndex indexes
# the list directly and ThrowArgumentOutOfRangeException is what the formation
# screen was dying on.
FORMATION_COUNT = 6
# Slots per team. PanelCharacterList.InitFormationSelfTeam allocates a fixed
# `new int[5]` for the per-slot support-skill ids and walks _teamListCharIcon in
# lockstep with the CharList, so every CharList must be exactly this long.
FORMATION_SLOTS = 5
# An empty slot is the empty string, not null or a missing entry:
# GetTeamCharListByIndex compares each entry against "" (StringLiteral_73) and only
# calls GetCharData for the others.
EMPTY_SLOT = ""

# PlayerCharData..ctor turns each `sort_list` string into a List<uint> by splitting
# on '_' and uint.Parse-ing the first two parts, so an entry is "<type>_<order>".
# PanelCharacterList.InitSortOrderAndSortType then picks one entry per
# _panelActionType (0->0, 7->1, 1->2, 2->3, 3->4, 4->5, 10->8, 9/16/17->13) and reads
# [0] as the sort type and [1] as the direction -- so the list must have at least
# FOURTEEN entries or the formation screen dies in that getter before it ever draws.
CHAR_SORT_SLOTS = 14
CHAR_BUYCOUNT_MAX = 50      # CharDefine.CharBuycountMax -> capacity 100 + 5*50 = 350
DEFAULT_CHAR_SORT = "0_1"

_lock = threading.Lock()


# Items a brand-new account is born holding, {item_id: amount}. These are ordinary
# backpack items (`_action` 0) the real game gifts at creation and the newbie flow then
# depends on -- item 18 "Training Gym Pass" x3 is required to enter Trainer's Gym, which
# the Cast Power-up goal step sends the player to. Seeded into Normal storage (1) in the
# same {sid,iid,amount,uid,attr} shape grant_item builds.
NEWBIE_STARTER_ITEMS = {18: 3}


def _starter_backpack():
    """Build the Normal-storage bag for NEWBIE_STARTER_ITEMS (see grant_item's shape)."""
    bag = {}
    for sid, (iid, amount) in enumerate(NEWBIE_STARTER_ITEMS.items(), start=1):
        bag[str(sid)] = {"sid": sid, "iid": iid, "amount": amount,
                         "uid": "", "attr": {}}
    return {str(BP_STORAGE_NORMAL): bag} if bag else {}


def _default(player_id):
    return {
        "player_id": player_id,
        "name": f"guest{player_id}",
        "currency": {str(CUR_CASH): 999999, str(CUR_MIRA): 99999,
                     str(CUR_REAL): 9999, str(CUR_DMM): 0, str(CUR_GUILD): 0},
        "energy": {str(ENERGY_ACTION): {"energy": 999, "cap": 999},
                   str(ENERGY_ARENA): {"energy": 99, "cap": 99},
                   str(ENERGY_ARENA_SP): {"energy": 99, "cap": 99},
                   str(ENERGY_ARENA_TEAM): {"energy": 99, "cap": 99}},
        "level": {"lv": 1, "xp": 0, "xp_cap": 100, "lv_min": 1, "lv_max": 200},
        "showgirl": 1,          # must be a real DesignCharForm row id
        # The owned characters, keyed by the uid the client uses everywhere as a
        # dictionary key (charDic, the formation CharLists, every later request).
        # Seeded from `team` below so a fresh account owns exactly the party it
        # fields; gacha will add to it.
        "roster": {},
        # Six saved teams of five slots (see FORMATION_COUNT / FORMATION_SLOTS),
        # seeded so team 1 is the starting party. `sup` is the 1-based slot index of
        # the support character, 0 for none -- InitFormationSelfTeam compares it
        # against `i + 1` when driving the support toggles.
        "formations": [],
        # The battle team, as DesignCharForm row ids in field-slot order. The client
        # never picks this -- the server decides who is fielded and pushes them in
        # BattleDatas -- so a starter team here is what makes a stage playable at all.
        # The newbie tutorial fields exactly two: LUCIFER in the back slot and
        # LEVIATHAN in front (slot order here is field position). Leviathan is the
        # faster of the two (spd 635 vs 610), so she also acts first.
        "team": [10001, 10011],
        # Level for the fielded party. Fitted against gameplay footage of the tutorial:
        # with rank-IV skills and the real defence curve, level 10 puts Leviathan's
        # basic at 686 damage against the wave-3 boss versus the 699 seen on video,
        # and her skill 2 at 858 versus 763. The original value is not recoverable
        # from the client, but two independent data points agreeing within a few
        # percent is a real fit rather than a guess.
        "team_level": 10,
        # Party-wide overrides for the fielded team. None/0 = use each character's own
        # roster values (falling back to their rarity tier -- see battle._default_star).
        # `team_star` is a STAR (1..6), not a _growStar rung: the top six rungs are
        # reached with team_super_star, which is an offset on top of it.
        "team_star": None,
        "team_super_star": 0,
        "backpack": _starter_backpack(),   # cbpType -> items; seeds NEWBIE_STARTER_ITEMS
        # stage id -> rating bitmask (bits 0..3; PlayerStage.GetStageRating counts the
        # set bits). Leave 1101 UNRATED: it is NewbieForceDefine.STAGE_STEP1, and the
        # newbie flow that auto-starts the tutorial battle keys off it -- pre-rating
        # it sends the client straight to the home screen instead.
        #
        # The cost is that PlayerBattle latches bTutorial from
        # GetStageRating(1101) == 0 for the whole fight, and while it is set
        # AttackState.OnMouseClickUp drops any target click that does not arrive with
        # the tutorial panel open at step 1. Setting {"1101": 15} here is the escape
        # hatch for free targeting, at the price of skipping the tutorial entirely.
        "stages": {},
        # Completed quest ids -> value. Only membership matters to the client
        # (QuestHasCompleted is a ContainsKey on this dict for _type 1 quests), but
        # it is the gate the newbie tutorial walks: PlayerStage.CheckNewbieForceStep
        # will not advance past the 1-1 clear until quest 31001 is in here.
        "quests": {},
        # Quest progress counters, keyed by DesignQuestRow.CaseKey. GetQuestValue
        # resolves _case_type 1 quests through DB_Datas.db[CaseKey].total_cnt, so this
        # is how "do X N times" quests advance (e.g. 31002 "pull gacha", key "13_0").
        "quest_db": {},
        # Type-2 (QuestForver) completions live here instead; see
        # complete_stage_quests for why the two cannot share a dict.
        "sp_quests": {},
        # char id -> {flv, fxp}: karma rank and progress, paid out by story decisions
        # and rendered into CharIDData. See grant_karma.
        "karma": {},
    }


def path_for(player_id):
    return os.path.join(STATE_DIR, f"{player_id}.json")


# ---- daily dungeon passes --------------------------------------------------
# A stage with `_ap_type == 2` costs `_ap` of ITEM `_ap_v1` -- not stamina and not the
# `energy` form, whose 16/17/18 rows are the older AP system this column supersedes.
# Proven empirically: we were already sending energy 16/17/18 at 99 and the panel still
# read 0, and `_ap_v1` 38902 resolves to Master Ticket, plainly a backpack item.
#
#   16 Evolution Abyss   17 Treasure Raiders   18 Training Gym   19 Transcend Corridor
#
# In-game tooltip: "Automatically replenishes up to 3 passes every 4:00 AM." So this is a
# top-up to the floor, NOT an increment -- stockpiling above 3 by other means must not be
# clawed back, and the design row's cap of 999 is the real ceiling.
DAILY_PASS_ITEM_IDS = (16, 17, 18, 19)
DAILY_PASS_FLOOR = 3
DAILY_RESET_HOUR = 4


def _daily_period(now=None):
    """Which 4AM-to-4AM day we are in, as an ISO date string."""
    t = time.localtime(now if now is not None else time.time())
    day = time.mktime(t) - (DAILY_RESET_HOUR * 3600)
    return time.strftime("%Y-%m-%d", time.localtime(day))


def _refill_passes(state):
    """Top daily-dungeon passes up to the floor once per 4AM period. -> True if changed."""
    period = _daily_period()
    if state.get("pass_refill_day") == period:
        return False
    changed = False
    for iid in DAILY_PASS_ITEM_IDS:
        if item_count(state, iid) < DAILY_PASS_FLOOR:
            grant_item(state, iid, DAILY_PASS_FLOOR - item_count(state, iid))
            changed = True
    state["pass_refill_day"] = period
    return True


def stage_ap_cost(stage_row):
    """-> (item id, count) a stage charges to enter, or None for ordinary stamina."""
    if int(stage_row.get("_ap_type") or 0) != 2:
        return None
    iid = int(stage_row.get("_ap_v1") or 0)
    return (iid, int(stage_row.get("_ap") or 1)) if iid else None


def load(player_id):
    os.makedirs(STATE_DIR, exist_ok=True)
    p = path_for(player_id)
    with _lock:
        if os.path.isfile(p):
            try:
                with open(p) as f:
                    st = json.load(f)
                # fill in keys added after this account was first written
                for k, v in _default(player_id).items():
                    st.setdefault(k, v)
                if _seed_roster(st) | _clamp_roster_stars(st) | _refill_passes(st):
                    _save_locked(st)
                return st
            except Exception:
                pass
        st = _default(player_id)
        _seed_roster(st)
        _save_locked(st)
        return st


def save(state):
    with _lock:
        _save_locked(state)


def _save_locked(state):
    os.makedirs(STATE_DIR, exist_ok=True)
    p = path_for(state["player_id"])
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1, sort_keys=True)
    os.replace(tmp, p)          # atomic, so a crash mid-write can't truncate the file


# ---- payload builders ----------------------------------------------------

def energy_json(state):
    return json.dumps({"energies": {
        k: {"type": int(k), "energy": v["energy"], "energy_cap": v["cap"],
            "refresh_time_bias": 0}
        for k, v in state["energy"].items()}}, separators=(",", ":"))


def currency_json(state):
    return json.dumps(state["currency"], separators=(",", ":"))


def level_json(state):
    lv = state["level"]
    return json.dumps({"levels": {PLAYER_LEVEL_UID: {
        "type": 1, "uid": PLAYER_LEVEL_UID, "lv": lv["lv"], "xp": lv["xp"],
        "xp_cap": lv["xp_cap"], "lv_min": lv["lv_min"], "lv_max": lv["lv_max"]}}},
        separators=(",", ":"))


def _level_obj(state):
    """One `Level` -- the shape LevelRpc Update (513) deserializes from strargs[0]."""
    lv = state["level"]
    return {"type": 1, "uid": PLAYER_LEVEL_UID, "lv": lv["lv"], "xp": lv["xp"],
            "xp_cap": lv["xp_cap"], "lv_min": lv["lv_min"], "lv_max": lv["lv_max"]}


def level_update_json(state):
    return json.dumps(_level_obj(state), separators=(",", ":"))


# ---- account level ---------------------------------------------------------
# PlayerLevel.HandleUpdateCmd (513, RVA 0x195906C) takes a single Level in strargs[0],
# requires the uid to already exist from the sync, and dispatches LevelEvent 2 with the
# old and new Level so the client can show the level-up.
#
# Neither the XP curve nor a stage's XP payout is in the pack -- there is no `level`
# design form at all -- so both are ours, same as stage drops. Tie the payout to the
# stamina the stage charges, which is the usual shape for this genre and means harder
# stages advance the account faster without a second table to maintain.
PLAYER_XP_PER_AP = 10
PLAYER_XP_FLAT_MIN = 10


def player_level_xp_cap(lv):
    """XP needed to leave account level `lv`. Gentle quadratic so early levels are quick
    and later ones stretch out."""
    return 100 + 20 * (lv - 1) + 2 * (lv - 1) ** 2


def stage_player_xp(stage_row):
    ap = int(stage_row.get("_ap") or 0)
    if int(stage_row.get("_ap_type") or 0) == 2:
        ap = 0                       # pass-gated dungeons do not charge stamina
    return max(ap * PLAYER_XP_PER_AP, PLAYER_XP_FLAT_MIN)


def grant_player_xp(state, add):
    """-> (levelled_up, old_lv, new_lv). Mirrors the cast loop: spend the bank one level
    at a time and keep the remainder, stopping at lv_max."""
    lv = state["level"]
    old_lv = int(lv["lv"])
    lv["xp"] = int(lv["xp"]) + int(add)
    while int(lv["lv"]) < int(lv["lv_max"]):
        cap = player_level_xp_cap(int(lv["lv"]))
        if lv["xp"] < cap:
            break
        lv["xp"] -= cap
        lv["lv"] = int(lv["lv"]) + 1
    lv["xp_cap"] = player_level_xp_cap(int(lv["lv"]))
    return int(lv["lv"]) != old_lv, old_lv, int(lv["lv"])


def uid(state):
    """The account's own uid string. PlayerBattle.HandleJudge compares its strargs[0]
    against PlayerGeneral.Uid and SILENTLY does nothing when they differ -- no event,
    no AttackState, the client just keeps re-asking for the turn -- so the general
    sync and the battle handlers have to agree on this value."""
    return str(state["player_id"])


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
CURRENCY_CASH = 1                                  # CurrencyType.Cash -- diamonds
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
        if int(state["currency"].get(str(CURRENCY_CASH), 0)) < cost:
            return False, 0, 0, 0, "not enough diamonds"
        state["currency"][str(CURRENCY_CASH)] = \
            int(state["currency"].get(str(CURRENCY_CASH), 0)) - cost
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


# ---- roster --------------------------------------------------------------
# The lobby's character and formation screens read PlayerChar.Data, not the battle
# party we push in BattleDatas, so an empty charDic/formations made every one of them
# throw. Everything below renders that structure.
#
# CharData and its friends are [JsonObject(MemberSerialization.OptIn)] (the attribute
# thunk passes 1), so ONLY [JsonProperty] members cross the wire: the extra public
# fields (EquipValues, curHp, FinalSkillList, ...) cannot be populated from here and
# stay at their defaults whatever we send.

def _clamp_roster_stars(state):
    """Migration: accounts written before super_star existed hold stars up to 12,
    i.e. a raw `_growStar` rung rather than a star tier. Those crash the client's
    rank-up check on every char sync (see MAX_STAR). Split the overflow back into
    the star/super_star pair that addresses the same rung, so a character stored as
    "12" keeps the fully-awakened stats it was meant to have.

    The party-wide `team_star` override carried the same confusion, and that one also
    reaches the client as LightBattleChar.Star, so split it the same way.

    Returns True if anything changed, so load() can persist it.
    """
    changed = False
    for entry in state.get("roster", {}).values():
        star = entry.get("star") or 0
        if star > MAX_STAR:
            entry["star"] = MAX_STAR
            entry["super_star"] = min(star - MAX_STAR, MAX_SUPER_STAR)
            changed = True
    if (state.get("team_star") or 0) > MAX_STAR:
        state["team_super_star"] = min(state["team_star"] - MAX_STAR, MAX_SUPER_STAR)
        state["team_star"] = MAX_STAR
        changed = True
    return changed


def _char_uid(player_id, n):
    """Per-character key. The client only ever uses it as a dictionary key and
    echoes it back in formation requests, but keep it numeric-looking in case
    anything parses it."""
    return f"{player_id}{n:04d}"


# The two starter casts and their TUTORIAL loadout. The newbie forced battles are
# balanced around a boosted Lucifer/Leviathan (level 10, all skill slots rank 4 -- see
# battle.skill_ranks; limitWithSuit 12 -> rank 4, under MAX_TOTAL_LIMIT 13), so a fresh
# account seeds them that way and can clear the tutorial. Once the forced flow is done
# (quest 31002 claimed) they RESET to base (level 1 / rank 1) for real progression --
# maybe_reset_tutorial_casts.
TUTORIAL_CASTS = frozenset({10001, 10011})
TUTORIAL_CAST_LV = 10
TUTORIAL_CAST_LIMIT = 12
TUTORIAL_DONE_QUEST = 31002


def _seed_roster(state):
    """Give an account its starting characters and teams. Returns True if it
    changed anything, so the caller can persist the migration for accounts written
    before the roster existed."""
    changed = False
    if not state.get("roster"):
        lv = state.get("team_level", 1)
        state["roster"] = {
            _char_uid(state["player_id"], i + 1):
                {"id": cid, "xp": 0, "plus": 0, "star": None,
                 "lv": TUTORIAL_CAST_LV if cid in TUTORIAL_CASTS else lv,
                 "limit_char": TUTORIAL_CAST_LIMIT if cid in TUTORIAL_CASTS else 0,
                 "limit_book": 0}
            for i, cid in enumerate(state.get("team", []))
        }
        changed = True
    if not state.get("formations"):
        team = list(state["roster"])[:FORMATION_SLOTS]
        state["formations"] = [
            {"array": team + [EMPTY_SLOT] * (FORMATION_SLOTS - len(team)), "sup": 0}
            if i == 0 else
            {"array": [EMPTY_SLOT] * FORMATION_SLOTS, "sup": 0}
            for i in range(FORMATION_COUNT)
        ]
        changed = True
    return changed


# DBCharData.star is 1..6, NOT an index into the whole 12-entry `_growStar` ladder:
# the second six rungs (110001..110006) are the super-star ladder, addressed by the
# separate `super_star` field (see battle.grow_rung). PlayerChar.UpdateCheckRankUpList
# -- which runs on EVERY char sync, right after receivedHelperData -- does
#     if (star != 6) { ... UpgradeDefine.RankUpMaterialCount[star - 1] ... }
# against a static int[6], with the bounds check hoisted above the branch. So any star
# outside 1..6 throws IndexOutOfRangeException out of receivedSync, which StackCore.poll
# swallows as "event execution error, message=Index was outside the bounds of the array".
# That is the error that had been misattributed to the shop sync payload.
MAX_STAR = bt.MAX_STAR
MAX_SUPER_STAR = bt.MAX_SUPER_STAR


def _char_data_json(uid, entry):
    """One CharData. Stats are computed the same way the battle engine builds a
    unit (DesignCharGrowForm indexed by the char's _growStar rung) so the lobby and
    the fight agree on the numbers.

    `star` defaults to the character's rarity rung -- see battle._default_star for
    why building a 5-rarity character at 1* is wrong -- and star/super_star are
    clamped to the ranges the client accepts (see MAX_STAR).
    """
    row = bt.dd.row("char", entry["id"]) or {}
    star = min(entry.get("star") or bt._default_star(row), MAX_STAR)
    super_star = max(0, min(entry.get("super_star") or 0, MAX_SUPER_STAR))
    stats = bt._grow(row, star, entry.get("lv", 1), super_star)
    raw_grow = row.get("_growStar") or []
    grow = (bt.dd.row("char_grow", raw_grow[bt.grow_rung(row, star, super_star)])
            if raw_grow else {}) or {}
    return {
        # DBCharData. `equips_list` may be empty: CharData.get_equips falls back to
        # PlayerChar.equips_template when the array is null or zero-length, but
        # dbdata itself must exist or the getter dereferences null.
        "dbdata": {
            "uid": uid, "time": 0, "id": entry["id"], "mod": entry["id"],
            "lv": entry.get("lv", 1), "xp": entry.get("xp", 0),
            # pxp is DBCharData.plusXP -- the Transcend gauge. It was hardcoded to 0,
            # so the bar snapped back to 0% after every Transcend animation even though
            # the server had banked the points correctly.
            "plus": entry.get("plus", 0), "pxp": entry.get("pxp", 0),
            # `super_limit` is new in 2.2.7 (DBCharData 0x38, thunk 0x1152850).
            # limit_book + limit_char IS the cast's skill rank: DBCharData carries no
            # skill field at all, and CharData.GetCharSkillLimitLvs walks the design
            # row's `_skillUp` sequence up to `limitWithSuit`
            # (= limit_char + limit_book + limit_suit + super_star) to derive each
            # slot's level. So skills are computed by the client, never stored, and
            # raising the limit is what "levels" them.
            "limit_book": entry.get("limit_book", 0),
            "limit_char": entry.get("limit_char", 0),
            "super_limit": entry.get("super_limit", 0),
            "star": star, "super_star": super_star,
            # DBCharData.ilock (wire key `lock`); set by cmd 276, and the
            # padlock is what keeps a cast out of decompose/unsummon.
            "lock": int(entry.get("lock", 0)),
            # DBCharData.skin is the CharSoulType currently DISPLAYED for this copy
            # (0 = base art). Per-copy, unlike the unlock flags, which are per char id.
            "equips_list": char_equips(entry), "skin": int(entry.get("skin", 0)),
        },
        "hp": stats["hp"], "atk": stats["atk"], "def": stats["def"],
        "spd": stats["spd"],
        # char_grow carries crt/cdi columns; the remaining CharData stats have no
        # column in this pack, so they stay 0 rather than being invented.
        "cri": grow.get("crt", 0), "cdi": grow.get("cdi", 0),
        "tgn": 0, "cdr": 0, "prc": 0, "ehit": 0, "eanti": 0,
    }


def maybe_reset_tutorial_casts(state):
    """Keep the starter casts right for the tutorial, then hand them to the player -- a
    ONE-TIME transition. Idempotent; returns True if it changed anything (caller
    persists + re-syncs).

    The forced newbie battles are BALANCED around a boosted Lucifer/Leviathan. While the
    tutorial is unfinished (quest 31002 not done) the two casts are held at level 10 /
    rank 4 (this also repairs accounts seeded before the boost existed). The moment it
    finishes, casts STILL at the exact tutorial boost revert to base, and the account is
    stamped `tutorial_casts_done` -- after which this NEVER touches them again, so any
    real leveling the player does post-tutorial is preserved.

    The `tutorial_casts_done` stamp is also why an already-developed account (e.g. 1000001,
    Lucifer at level 100) is safe: it is `done` but its casts are NOT at limit_char 12, so
    nothing is reverted -- it is just stamped and left alone.
    """
    if state.get("tutorial_casts_done"):
        return False
    done = quest_completed(state, TUTORIAL_DONE_QUEST)
    changed = False
    for entry in state.get("roster", {}).values():
        if entry.get("id") not in TUTORIAL_CASTS:
            continue
        boosted = int(entry.get("limit_char", 0) or 0) >= TUTORIAL_CAST_LIMIT
        if done:
            if boosted:                        # still the untouched tutorial loadout
                entry.update({"lv": 1, "xp": 0, "plus": 0,
                              "limit_char": 0, "limit_book": 0, "super_star": 0})
                changed = True
        elif not boosted:
            entry.update({"lv": TUTORIAL_CAST_LV, "limit_char": TUTORIAL_CAST_LIMIT,
                          "limit_book": 0})
            changed = True
    if done:
        # One-time: stamp so post-tutorial leveling is never clobbered.
        state["tutorial_casts_done"] = True
        changed = True
    return changed


def char_limit(entry):
    """A roster entry's limit level = CharData.get_limit = limit_char + limit_book.

    This is the cast's skill rank: skills are learned per limit level out of the
    design row's `_skillUp`, so there is no separate stored skill value."""
    return int(entry.get("limit_book", 0)) + int(entry.get("limit_char", 0))


def _char_id_json(entry, star, super_star=0, karma=None, skins=()):
    """One CharIDData -- the per-character-ID (not per-copy) book/affinity record.

    `flv`/`fxp` are the karma rank and its progress, fed by story decisions.
    `skins` is the set of unlocked CharSoulTypes for this character id."""
    karma = karma or {"flv": 1, "fxp": 0}
    return {
        "flv": karma.get("flv", 1), "fxp": karma.get("fxp", 0),
        # skin1/2/3 are the UNLOCK flags for CharSoulType Limit/Super/Ultra
        # ("Break" / "EX Break" / "Apoc."). receivedUnlockSkin writes 1 to exactly
        # these three fields at CharIDData +0x18/+0x1C/+0x20.
        "skin1": int(1 in skins), "skin2": int(2 in skins), "skin3": int(3 in skins),
        "lv": entry.get("lv", 1), "star": star, "super_star": super_star,
        "plus": entry.get("plus", 0),
        # CharIDData.skill -- the pedia's record of the best skill rank ever reached
        # for this character id. Per copy that rank is limit_book + limit_char
        # (CharData.get_limit); nothing stores it separately.
        "skill": char_limit(entry), "score": 0,
        "kset": [], "klv": {},
    }


# Unsummon / "Mana Extract" (PanelSell action type 0, CharRpcServerCmd.char_sell 291).
# Selling a cast pays the mana crystals named on its own design row -- `_sellId` x
# `_sellNumber`, e.g. Lucifer = 6x item 502 "Prime Mana Crystal" -- plus coins. Low
# rarity casts (the gremlins) carry `_sellId` 0 and pay coins only.
#
# The coin figure comes from CharDefine.SellMoneyStar, a 6-entry int[] whose values
# live in global-metadata.dat (an il2cpp RuntimeFieldHandle blob) rather than in
# libil2cpp.so, and which has NO C# reader at all (only its cctor and a Puerts
# get/set), so it could not be read out the way every other constant here was. It is
# calibrated instead against the panel's own pre-confirm preview -- the same number the
# live server paid.
#
# Calibrated 2026-08-04 from four previews. Keyed by the cast's RANK (the upgradeable
# star), NOT by design `_rarity`: rarities 3/4/5 give three different payouts while
# ranks 4/5/6 line up one-to-one, and a 6-entry array indexed [rank - 1] covers ranks
# 1..6 exactly.
#
#   rank 3 -> 500     (rarity-2 gremlin, lv 1)
#   rank 4 -> 1500    (Jacqueline / Evelina, rarity 3, lv 1)
#   rank 5 -> 3000    (Dixie, rarity 4, lv 1)
#   rank 6 -> 15000   (Lucifer / Leviathan, rarity 5, lv 100)
#
# LEVEL DOES NOT MATTER, tested directly: a rank-3 gremlin levelled to 30 server-side
# still previewed 500, identical to its lv-1 siblings. That was worth checking because
# every sample below rank 6 was level 1 while the only rank-6 sample was level 100, so
# the 15000 could have been a level effect. It is not -- the payout is purely
# SellMoneyStar[rank - 1]. Ranks 1-2 are 0 because no cast is ever ranked that low: the
# lowest grade in the game is the rank-3 fodder, so those slots are unreachable rather
# than uncalibrated. This table is COMPLETE for every rank that exists.
SELL_COIN_BY_RANK = (0, 0, 500, 1500, 3000, 15000)


def sell_coins(rank):
    """Coins paid for unsummoning a cast of this rank. CharDefine.SellMoneyStar is a
    6-entry array indexed [rank - 1], so rank 1 is the first slot."""
    try:
        return SELL_COIN_BY_RANK[int(rank) - 1]
    except (IndexError, TypeError, ValueError):
        return 0

# Coin, as an ITEM id (grant_reward routes it to currency via the item row's _action).
# Same id the login bonus pays: mail row 1001 is _itemID 2 x 50000 = "coin 50000".
COIN_ITEM_ID = 2
# ...and as a CURRENCY key, which is what state["currency"] is indexed by.
CURRENCY_COIN = 16


def item_count(state, item_id, cbp_type=None):
    """How many of an item the player is holding."""
    bag = state["backpack"].get(str(cbp_type if cbp_type is not None
                                    else BP_STORAGE_NORMAL), {})
    return sum(e.get("amount", 0) for e in bag.values() if e.get("iid") == item_id)


def has_item(state, item_id, amount):
    return item_count(state, item_id) >= amount


def char_sell_gain(char_id, rank):
    """-> [(item id, amount), ...] paid for unsummoning one cast.

    Mana crystals are exactly the cast's own `_sellId` x `_sellNumber` with no level or
    rank scaling -- verified against the panel preview (Lucifer +6 x 502, Dixie +2 x
    502, Jacqueline +3 x 501, all matching their design rows). Coins scale with rank."""
    row = dd.row("char", char_id) or {}
    gain = []
    sell_id, sell_n = row.get("_sellId") or 0, row.get("_sellNumber") or 0
    if sell_id and sell_n:
        gain.append((sell_id, sell_n))
    coins = sell_coins(rank)
    if coins:
        gain.append((COIN_ITEM_ID, coins))
    return gain


def sell_chars(state, uids):
    """Unsummon casts: drop them from the roster and pay out.

    Returns [(item id, amount), ...] merged across every cast sold. The charIDDic
    record is deliberately NOT pruned -- Soul Link help rule 3 says the pedia keeps a
    cast's record even after it is unsummoned or lost."""
    gain = {}
    sold = []
    for uid in uids:
        entry = state["roster"].get(uid)
        if not entry:
            continue
        row = dd.row("char", entry["id"]) or {}
        star = entry.get("star") or char_star(row.get("_rarity"))
        for item_id, amount in char_sell_gain(entry["id"], star):
            gain[item_id] = gain.get(item_id, 0) + amount
        del state["roster"][uid]
        sold.append(uid)
    # A sold cast must not linger in a formation, or the client keeps showing it in the
    # party and CompiledCharDic would mark a uid that no longer exists.
    for form in state.get("formations", []):
        form["array"] = ["" if u in sold else u for u in form.get("array", [])]
    for item_id, amount in gain.items():
        grant_reward(state, item_id, amount)
    return sold, sorted(gain.items())


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
    return STAGE_XP_PER_LEVEL_PER_WAVE * max(int(stage_row.get("_stagelv") or 1), 1) \
        * max(int(waves), 1)


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

    Returns (ok, total karma xp, consumed). Refuses outright if the player does not
    hold the items, rather than partially applying."""
    wanted = [(int(i), int(n)) for i, n in pairs if n > 0]
    if not wanted:
        return False, 0, []
    if len(wanted) > MAX_RECIEVE_GIFT_TYPE_NUM:
        return False, 0, []
    if sum(n for _i, n in wanted) > MAX_RECIEVE_GIFT_NUM:
        return False, 0, []
    for item_id, n in wanted:
        if not has_item(state, item_id, n):
            return False, 0, []
    total = sum(gift_karma_xp(char_id, i, n) for i, n in wanted)
    if total <= 0:
        return False, 0, []
    for item_id, n in wanted:
        spend_item(state, item_id, n)
    grant_karma(state, char_id, total)
    return True, total, wanted


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


def char_rarity(char_id):
    row = dd.row("char", char_id) or {}
    return int(row.get("_rarity") or 1)


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


# ---- Break artwork / skins (Char 308 unlock_skin, 309 set_skin) -------------
#
# `CharSoulType`: Normal=0 (base art), **Limit=1 (大破 "Break")**, **Super=2 (超大破
# "EX Break")**, **Ultra=3 (天啓 "Apoc.")** -- the three tabs on the character gallery,
# unlocked by filling the BREAK gauge with Soulmirrors (see [[sevensins-soulmirrors]]).
#
# Two DIFFERENT scopes, which is the thing to keep straight:
#   * the UNLOCK is per CHARACTER ID   -> CharIDData.skin1/skin2/skin3 (cmd 567)
#   * the SELECTION is per COPY (uid)  -> DBCharData.skin              (cmd 528)
#
# 308 `RequestUnlockSkin(char_uid, skinType)` (0x16A0F50) sends
# `intargs=[skinType], strargs=[char_uid]`, but the reply is NOT an echo:
# `receivedUnlockSkin` (0x169BEA4) reads `intargs=[**charId**, skinType]` -- the design
# id, not the uid -- indexes `charIDDic[charId.ToString()]` with `get_Item` (throws on a
# character we never synced) and sets skin1/2/3 for skinType 1/2/3. It needs at least
# TWO intargs and no strargs at all. A charId of 0 makes it skip the flag write and only
# raise the event, so always send the real id.
#
# 309 `RequestSetSkin(char_uid, skinType)` (0x16A1030) has the same request shape and
# its reply IS an echo: `receivedSetSkin` (0x169C118) reads `strargs[0]` as the uid,
# `intargs[0]` as the type, and writes `charDic[uid].dbChar.skin` (DBCharData +0x58).
#
# `PanelCharacterGallery.OnGalleryMoldingUnlockClick` (0x16D1734) calls RequestUnlockSkin
# straight through -- no currency check, no item cost, no confirm dialog -- so unlocking
# is free and the eligibility gate is entirely the client's BREAK gauge.
CHAR_SOUL_NORMAL, CHAR_SOUL_LIMIT, CHAR_SOUL_SUPER, CHAR_SOUL_ULTRA = 0, 1, 2, 3
CHAR_SOUL_TYPES = (CHAR_SOUL_LIMIT, CHAR_SOUL_SUPER, CHAR_SOUL_ULTRA)


def unlocked_skins(state, char_id):
    """The CharSoulTypes unlocked for a character id."""
    return {int(t) for t in state.get("skins", {}).get(str(char_id), ())}


def unlock_skin(state, char_uid, skin_type):
    """Mark a CharSoulType unlocked for the cast's character id. -> that char id."""
    entry = state["roster"][char_uid]
    if int(skin_type) not in CHAR_SOUL_TYPES:
        raise ValueError(f"{skin_type} is not an unlockable CharSoulType")
    cid = str(entry["id"])
    have = state.setdefault("skins", {}).setdefault(cid, [])
    if int(skin_type) not in have:
        have.append(int(skin_type))
        have.sort()
    return entry["id"]


def set_skin(state, char_uid, skin_type):
    """Choose which artwork a COPY displays. -> the stored value."""
    entry = state["roster"][char_uid]
    skin_type = int(skin_type)
    if skin_type != CHAR_SOUL_NORMAL and skin_type not in CHAR_SOUL_TYPES:
        raise ValueError(f"{skin_type} is not a CharSoulType")
    if skin_type != CHAR_SOUL_NORMAL and \
            skin_type not in unlocked_skins(state, entry["id"]):
        raise ValueError(f"CharSoulType {skin_type} is not unlocked for {entry['id']}")
    entry["skin"] = skin_type
    return skin_type


def char_id_table(state):
    """`charIDDic` -- {char id: CharIDData}, keyed by character ID rather than copy.

    Shared by the login sync (`char_json`, as `id_tbl`) and the Soulpedia request
    (PlayerChar cmd 310 -> 567), which must agree or the pedia's completion count
    disagrees with the roster."""
    id_tbl = {}
    for uid, entry in state["roster"].items():
        db = _char_data_json(uid, entry)["dbdata"]
        cid = str(entry["id"])
        rec = _char_id_json(entry, db["star"], db["super_star"],
                            karma_of(state, entry["id"]),
                            unlocked_skins(state, entry["id"]))
        # Rule 2: keep the best copy, not the last one seen.
        prev = id_tbl.get(cid)
        if prev is None or char_soulbook_score(entry["id"], rec) > \
                char_soulbook_score(entry["id"], prev):
            id_tbl[cid] = rec
    for cid, rec in id_tbl.items():
        rec["score"] = char_soulbook_score(int(cid), rec)
        # `kset`/`klv` are CONFIRMED (2026-08-08, `tools/json_keys.py --class
        # CharIDData` on the 2.2.4 binary: BookKizunaSetData -> 'kset',
        # BookKizunaLvData -> 'klv'); they are no longer the guesses this comment used
        # to describe. The C# field names are kept alongside purely as belt-and-braces:
        # Json.NET ignores whichever does not match, and an unbound list/dict here would
        # leave BookKizunaLvData null, which receivedBookKizunaSet dereferences without
        # a guard. The same run confirmed skin1/skin2/skin3 and DBCharData's `skin`.
        rec["kset"] = rec["BookKizunaSetData"] = kizuna_set(state, cid)
        rec["klv"] = rec["BookKizunaLvData"] = kizuna_levels(state, cid)
    return id_tbl


# ---- Soul Book Kizuna (CharRpc book_kizuna_set 313 -> 569,
#                                 book_kizuna_lvup 320 -> 576) ---------------
# The "Active Effects 0/5" / "Master" panel on a cast. Two pieces of per-CHARACTER (not
# per-copy) state live on CharIDData and must be in every charIDDic we send:
#   List<uint>            BookKizunaSetData  -- the equipped support rows, max 5
#                                              (CharDefine.SoulBookKizunaMaxEquipCount)
#   Dictionary<uint,uint> BookKizunaLvData   -- row id -> level
#
# receivedBookKizunaSet (RVA 0x169C9D0) rebuilds the equipped list from
# `intargs = [charID, ...rowIds]` and looks each row's level up in BookKizunaLvData, so
# the LEVELS have to already be on the client from the char sync -- reply 569 does not
# carry them. receivedBookKizunaLvup (RVA 0x169CD30) is just
# `intargs = [charID, rowID, newLevel]`.
#
# NOT VALIDATED: level-up costs and unlock conditions live in the `soulbook_kizuna`
# design form, which our type-tree generator cannot parse (`read_str out of bounds` --
# the row has nested UnlockCondition[]/LevelVariable[] arrays under a generic base).
# The client owns the pack and gates the UI itself, so the server is permissive here.
MAX_KIZUNA_EQUIP = 5        # CharDefine.SoulBookKizunaMaxEquipCount


def _kizuna(state, char_id):
    return state.setdefault("kizuna", {}).setdefault(str(char_id),
                                                     {"set": [], "lv": {}})


def kizuna_set(state, char_id):
    return list(_kizuna(state, char_id)["set"])


def kizuna_levels(state, char_id):
    return dict(_kizuna(state, char_id)["lv"])


def set_kizuna(state, char_id, row_ids):
    """Equip support rows. -> (ok, stored rows)."""
    rows, seen = [], set()
    for r in row_ids:
        r = int(r)
        if r and r not in seen:
            seen.add(r)
            rows.append(r)
    if len(rows) > MAX_KIZUNA_EQUIP:
        return False, kizuna_set(state, char_id)
    k = _kizuna(state, char_id)
    k["set"] = rows
    # A row has to have a level entry or receivedBookKizunaSet's dictionary lookup
    # throws -- it indexes BookKizunaLvData[rowId] with no ContainsKey guard.
    for r in rows:
        k["lv"].setdefault(str(r), 0)
    return True, rows


def kizuna_level_up(state, char_id, row_id, now_lv):
    """Raise one support row's level. -> (ok, new level)."""
    k = _kizuna(state, char_id)
    cur = int(k["lv"].get(str(row_id), 0))
    if int(now_lv) != cur:
        return False, cur          # client and server disagree; refuse rather than guess
    k["lv"][str(row_id)] = cur + 1
    return True, cur + 1


# ---- Soul Link (the soulbook) ---------------------------------------------
# `bookRank` / `bookSumXp` / `bookLeftXp` on PlayerCharData, drawn as the RANK + score
# bar along the bottom of the Soulpedia. Nothing client-side computes this -- the
# DesignSoulbookScoreRow getters have no code xrefs at all -- so it is server
# authoritative and had to be reconstructed. The in-game "Soul Link Point Rules" help
# screen gives the whole table, and it matches `soulbook_score` column for column:
#
#             First Summon  Base Level  Rank  Ultra Level  Skill Rank  Karma Rank
#   Sins             28000         210  8800          400        7000         280
#   Virtues          14000         210  8800          400        5250         280
#   Riders           14000         210  8800          400        5250         280
#   *5 Awakers        3500          85  8000          400        1750         225
#   *4 Awakers        1750          65  5600          100         350         165
#
#   design column:   _base     _rankup  _star   _surmount     _awaken     _kizuna
#
# Rows 103/104 match the two Awaker lines exactly. The row id is the char's
# **`_alignment`**, confirmed by who lives in each: 100 = Lucifer/Leviathan/Satan
# (Sins), 101 = Michael/Uriel/Sariel (Virtues), 102 = Esmira/Chino (Riders), 103/104 =
# the *5 / *4 Awakers, 9001 = gremlin mobs. Our EN 2.2.7 pack has 101/102 at Sins-level
# values (28000/7000) and 100 raised to 220/9000/420/7200/300, i.e. the categories were
# rebalanced after the build that help screen was written for -- so always read the
# numbers from the pack, never from the table above.
#
# Help-screen rules, all three implemented here:
#   1. eligible only for casts of initial rarity *4 or above  -> soulbook_eligible
#   2. scored from the HIGHEST trained status among copies    -> the charIDDic record
#   3. the record is kept even if the cast is unsummoned/lost -> charIDDic is keyed by
#      character id, so it already outlives any individual copy
SOULBOOK_MIN_STAR = 4

# DesignCharRow._rarity -> displayed star, read out of the 2.2.7 binary at 0x3703B10
# (PanelCharacterList.GetBrowsableIllustrationList indexes it as [_rarity - 1]).
_CHAR_STAR = (1, 3, 4, 5, 5)


def char_star(rarity):
    """Displayed star count for a design rarity (1..5)."""
    try:
        return _CHAR_STAR[int(rarity) - 1]
    except (IndexError, TypeError, ValueError):
        return 0


def soulbook_eligible(char_id):
    """Rule 1: Soul Link only counts casts whose INITIAL rarity is *4 or above."""
    row = dd.row("char", char_id)
    return bool(row) and char_star(row.get("_rarity")) >= SOULBOOK_MIN_STAR


def char_soulbook_score(char_id, record):
    """Soul Link points for one cast, from its best-ever record (a CharIDData).

    `record` is the charIDDic entry, which is the high-water mark across every copy
    ever owned -- that is what help rules 2 and 3 describe."""
    row = dd.row("char", char_id)
    if not row or not soulbook_eligible(char_id):
        return 0
    w = dd.row("soulbook_score", row.get("_alignment"))
    if not w:
        return 0
    # First Summon is granted once for having ever obtained the cast; every other
    # column is a per-level rate applied to the trained value.
    return (w.get("_base", 0)
            + w.get("_rankup", 0) * record.get("lv", 0)
            + w.get("_star", 0) * record.get("star", 0)
            + w.get("_surmount", 0) * record.get("super_star", 0)
            + w.get("_awaken", 0) * record.get("skill", 0)
            + w.get("_kizuna", 0) * record.get("flv", 0))


def book_rank_for(sum_xp):
    """Highest `soulbook_reward` rank whose cumulative _exp_sum the score has reached.

    This is the rank the player is ELIGIBLE for, not the one they hold: the bar shows
    `RANK UP!` and only advances on CharRpcServerCmd.book_rank_up (311), which is why
    footage of a live account sits at RANK 97 with a score already past rank 100."""
    rank = 0
    for rid in sorted(dd.rows("soulbook_reward")):
        if sum_xp < dd.row("soulbook_reward", rid).get("_exp_sum", 0):
            break
        rank = rid
    return rank


def book_sum_xp(state):
    """Total Soul Link points across every cast ever recorded."""
    return sum(char_soulbook_score(int(cid), rec)
               for cid, rec in char_id_table(state).items())


def book_progress(state):
    """-> (bookRank, bookSumXp, bookLeftXp) for the Soul Link bar."""
    sum_xp = book_sum_xp(state)
    rank = int(state.get("book_rank", 0))
    claimed = dd.row("soulbook_reward", rank) or {}
    # "left" = points not yet consumed by the ranks already claimed.
    return (rank, sum_xp, max(0, sum_xp - claimed.get("_exp_sum", 0)))


def char_json(state):
    """PlayerCharData. `acPeriod` and the three ctor params must be present or the
    client NPEs -- see docs/GAME_SERVER.md."""
    pro_chars = {uid: _char_data_json(uid, entry)
                 for uid, entry in state["roster"].items()}
    # charIDDic is keyed by the character ID, so multiple copies collapse to one entry
    # -- that is the point of it being separate from charDic. Build it with the same
    # helper the Soulpedia sync (567) uses, or the two disagree: this used to inline
    # _char_id_json, which kept the best-copy rule but left `kset`/`klv` empty, so the
    # LOGIN charIDDic carried no Kizuna state at all.
    id_tbl = char_id_table(state)
    return json.dumps({
        "pro_chars": pro_chars,
        "group_tbl": {}, "id_tbl": id_tbl,
        "formations": state["formations"], "acPeriod": [],
        # PlayerCharData.addCharCount. Roster capacity is
        # CharCapacityDefault(100) + CharCapacityPerBuycount(5) * add_char, capped at
        # CharBuycountMax(50) -> 350. Left at 0 the roster jams at 100, which the
        # Consonance partner list alone overruns.
        "add_char": int(state.get("add_char", CHAR_BUYCOUNT_MAX)),
        "showgirl": state["showgirl"],
        "state": int(state.get("showgirl_state", 0)),
        "offset": state.get("showgirl_offset", SHOWGIRL_OFFSET_DEFAULT),
        # PlayerChar.receivedHelperData (0x169B860) only accepts a uid that is IN
        # charDic; anything else -- including "" -- falls through to the
        # `helper uid error` warning that repeated once per login. The helper is the
        # cast lent to friends, so the lead of the first formation is the sane default.
        "helper": helper_uid(state),
        "book_rank": 0, "book_xp": 0,
        "orgArenaTeam": [],
        "sort_list": state.get("sort_list") or [DEFAULT_CHAR_SORT] * CHAR_SORT_SLOTS,
        "act_collection": [],
    }, separators=(",", ":"))


# ---- the two lobby icons: helper (304) and showgirl (305) -------------------
#
# **304 `set_helper` -> 560.** `RequestServerSetHelper(char_uid)` (0x16A024C) sends
# `strargs=[char_uid]` and no intargs; the reply is an echo of the same single strarg
# (`receivedHelperData(char_uid, showmsg=true)`), and the uid MUST be in charDic.
#
# **305 `set_showgirl` -> 561.** `RequestServerSetShowgirl(group, id, state, x, y, scale)`
# (0x16A00B4) sends `intargs=[char_group, char_id, char_state]` and
# `strargs=[String.Format("{0:F4}_{1:F4}_{2:F3}", x, y, scale)]`.
# **The reply is NOT a straight echo -- it takes only TWO intargs**:
# `receivedShowgirlData(intargs[0] = showgirl_id, intargs[1] = showgirl_state,
# strargs[0] = offsetStr)`, and the dispatcher bounds-checks for at least two, so the
# group is dropped. `showgirl_id` is looked up in DesignCharForm (a CHAR id, not a
# group); an unknown id degrades to 0 rather than throwing. The offset string is split
# on '_' and Convert.ToSingle'd per part.
#
# Note `receivedShowgirlData` also consults `IsGENTELMAN()`: on a stock EN build any
# state other than 0 or 3 is forced back to 0 and the offsets are discarded. Our patched
# libil2cpp returns true, so the state we send is kept -- see [[sevensins-break-skins]].
SHOWGIRL_OFFSET_DEFAULT = ""


def set_helper(state, char_uid):
    """Choose the cast lent to friends. -> the stored uid."""
    if char_uid not in state.get("roster", {}):
        raise ValueError(f"helper uid {char_uid!r} is not in the roster")
    state["helper"] = char_uid
    return char_uid


def set_showgirl(state, char_id, char_state, offset):
    """Choose the lobby showgirl. -> (id, state, offset)."""
    if not (bt.dd.row("char", int(char_id)) or {}):
        raise ValueError(f"showgirl id {char_id} is not a DesignCharForm row")
    state["showgirl"] = int(char_id)
    state["showgirl_state"] = int(char_state)
    state["showgirl_offset"] = str(offset or "")
    return state["showgirl"], state["showgirl_state"], state["showgirl_offset"]


def helper_uid(state):
    """The cast lent out as a helper. MUST be a uid present in the roster (charDic) or
    `receivedHelperData` rejects it and logs `helper uid error`; "" is not a valid
    "none" value. Prefers the player's own pick, then the lead of the first formation,
    then any owned cast."""
    chosen = state.get("helper")
    if chosen and chosen in state.get("roster", {}):
        return chosen
    for slot in (state.get("formations") or [{}])[0].get("array", []):
        if slot and slot in state.get("roster", {}):
            return slot
    return next(iter(state.get("roster", {})), "")


def battle_team(state, index=0):
    """The party to field, taken from a saved formation so that editing the team in
    the lobby actually changes who fights. Falls back to the raw `team` list for
    accounts whose roster could not be seeded.

    Yields the roster entries themselves (id + lv + star + super_star), which
    battle.Battle accepts in place of bare ids, so a fight uses the same _growStar
    rungs the lobby just displayed."""
    try:
        slots = state["formations"][index]["array"]
    except (KeyError, IndexError, TypeError):
        return list(state.get("team", []))
    party = [dict(state["roster"][u], uid=u) for u in slots
             if u and u in state["roster"]]
    return party or list(state.get("team", []))


def set_formation(state, index, uids, support):
    """Apply a client formation edit (PlayerChar cmd 274) and persist it.

    Returns the stored FormationData so the caller can echo it back on cmd 530,
    which replaces formations[index] wholesale on the client side.
    """
    slots = [u if u in state["roster"] else EMPTY_SLOT
             for u in list(uids)[:FORMATION_SLOTS]]
    slots += [EMPTY_SLOT] * (FORMATION_SLOTS - len(slots))
    data = {"array": slots, "sup": support}
    if 0 <= index < len(state["formations"]):
        state["formations"][index] = data
        save(state)
    return data


# ---- lock / decompose / capacity / sort / remove-from-all-formations --------
# Read out of PlayerChar.OnClientCmdReceived (0x1698584) and the four handlers it
# dispatches to. Reply cmds: 276->532, 277->533, 292->548 (fail 597), 312->none,
# 321->577. Every one of these is reachable from the cast list, and the sell/unsummon
# precedent says an unanswered one soft-locks its panel rather than merely doing nothing.


def toggle_char_lock(state, uids):
    """CharRpcServerCmd.lock_char (276). -> [(uid, new state), ...]

    It is a TOGGLE and the server owns the decision: `RequestServerLock(List<string>)`
    (0x16A00A0) sends the uids as strargs and **no intargs at all**, and
    `PanelCharacterInformation.OnLockClick` passes exactly one uid with no desired
    state. Echoing a fixed 1 back would make the padlock un-clearable.

    `receivedLockChar` (0x169A36C) does `charDic[strargs[0]].dbChar.ilock = intargs[0]`
    -- ONE uid per reply, and it uses Dictionary.get_Item, which THROWS on a uid it does
    not hold. So reply once per uid and never name a uid we do not have.
    `DBCharData.ilock` is wire key `lock`, which char_json already emits.
    """
    changed = []
    for uid in uids:
        entry = state["roster"].get(uid)
        if entry is None:
            continue
        entry["lock"] = 0 if entry.get("lock") else 1
        changed.append((uid, entry["lock"]))
    return changed


def decompose_gain(char_id):
    """-> [(item id, amount), ...] paid for decomposing one cast.

    The `char_decompose` design form is keyed by the cast's `char._group` (Michael,
    group 10101 -> Gust of Agility x4/x3/x2). Casts whose group has no rows -- most of
    the 3017 char rows are boss/placeholder entries -- yield nothing.
    """
    row = dd.row("char", char_id) or {}
    group = row.get("_group")
    if group is None:
        return []
    return [(r["_item_id"], r["_item_count"])
            for r in (dd.rows("char_decompose") or {}).values()
            if r.get("_group") == group and r.get("_item_id")]


def decompose_chars(state, uids):
    """CharRpcServerCmd.char_decompose (292). -> (decomposed uids, [(item, amt), ...])

    Mirrors sell_chars: `receivedCharDecompose` (0x169AB8C) drops every uid in strargs
    from charDic and reads intargs as FLAT [item id, amount, ...] pairs for the reward
    popup, exactly like char_sell. Refuses casts that are locked or fielded, the same
    rule the Unsummon path enforces -- otherwise the client keeps showing a uid that no
    longer exists.
    """
    in_party = {u for f in state.get("formations", []) for u in f.get("array", []) if u}
    gain, done = {}, []
    for uid in uids:
        entry = state["roster"].get(uid)
        if not entry or uid in in_party or entry.get("lock"):
            continue
        for item_id, amount in decompose_gain(entry["id"]):
            gain[item_id] = gain.get(item_id, 0) + amount
        del state["roster"][uid]
        done.append(uid)
    for item_id, amount in gain.items():
        grant_reward(state, item_id, amount)
    return done, sorted(gain.items())


def remove_from_all_formations(state, uid):
    """CharRpcServerCmd.remove_from_allformation (321). -> the formations dict to echo.

    `receivedRemoveFromAllFormation` (0x169986C) takes TWO strargs -- [0] normal
    formations, [1] arena-team formations -- and indexes [1] before checking the count,
    so both must be present. Each is `Dictionary<int, FormationData>` and the client does
    `formations.set_Item(key - 1, value)`, i.e. the keys are **1-based**.
    Both are also deref'd right after deserializing, so neither may be a string that
    yields null: send real JSON (`{}` at worst), never `""`.
    """
    for form in state.get("formations", []):
        form["array"] = [EMPTY_SLOT if u == uid else u for u in form.get("array", [])]
    return {str(i + 1): f for i, f in enumerate(state.get("formations", []))}


def set_char_sort(state, index, sort_type, down):
    """CharRpcServerCmd.set_sort (312). Fire-and-forget -- there is no set_sort in
    CharRpcClientCmd, so the client never waits for a reply; we only need to persist it
    so the next login sync returns the same ordering.

    `sort_list` is CHAR_SORT_SLOTS entries of "<type>_<down>"; `index` selects the slot
    (one per cast-list context).
    """
    slots = state.setdefault(
        "sort_list", [DEFAULT_CHAR_SORT] * CHAR_SORT_SLOTS)
    while len(slots) < CHAR_SORT_SLOTS:
        slots.append(DEFAULT_CHAR_SORT)
    if 0 <= index < CHAR_SORT_SLOTS:
        slots[index] = f"{int(sort_type)}_{int(down)}"
    return slots


def char_capacity(state):
    """CharRpcServerCmd.char_max (277). Despite the name this is NOT "max out a cast":
    case 533 sets `PlayerCharData.addCharCount = intargs[0]` and raises
    CharEventType.CHAR_CHAR_MAX -- it is the roster CAPACITY purchase/query.
    Capacity itself is CharCapacityDefault(100) + CharCapacityPerBuycount(5) * add_char.
    """
    return int(state.get("add_char", CHAR_BUYCOUNT_MAX))


def stage_json(state):
    """PlayerStage.StageSyncData. `stages` maps stage id -> rating bitmask and is
    what GetStageRating reads; the other three dicts use LuaTableConverter and must
    be present or they deserialize to null."""
    return json.dumps({
        "entrance": {}, "stages": state["stages"], "bestrec": {}, "auto": {},
        "weekday": 0,
    }, separators=(",", ":"))


# ---- quests ---------------------------------------------------------------
# The newbie tutorial is driven by PlayerStage.CheckNewbieForceStep, which gates on
# NewbieForceDefine's STAGE_STEP1 = stage 1101, QUEST_STEP1 = quest 31001 ("clear
# story battle 1-1") and QUEST_STEP2 = quest 31002 ("pull gacha once"):
#
#   QuestHasCompleted(31002)          -> [-1,-1]  flow finished
#   GetStageRating(1101) == 0         -> [-1,-1]  no forced step  <- needs stage sync
#   CheckQuestWillbeCompleted(31001)  -> [1, 1]   announce the 1-1 clear
#   QuestHasCompleted(31001) and not CheckQuestWillbeCompleted(31002)
#                                     -> [1, 2] / [2, 0]   the GACHA step
#   otherwise                         -> [-1,-1]
#
# So the gacha explanation needs 31001 *completed* and 31002 not yet completable --
# no gacha implementation required for the step itself to appear.

# DesignQuestRow._case_id 1002 means "clear main story stage `_case_v1`". Inferred
# from quest 31001 (_case_id 1002, _case_v1 1101, named "完成 劇情戰鬥「1-1 普通」")
# and corroborated by all 516 rows carrying it being named "攻略主線劇情 (N)".
# Other condition types are not modelled, so only stage-clear quests complete.
QUEST_CASE_CLEAR_STAGE = 1002
# Which dictionary a completion has to go in is decided by **`_case_type` (row +0xA8)**,
# NOT `_type` (+0x94). PlayerQuest.QuestHasCompleted does `LDR W8,[X0,#0xA8]` and then
#   1 -> Quest_Datas.quests.ContainsKey(id)
#   2 -> SP_Quest_Datas.sp_quests[id].status == 1
#   anything else -> always false
# The two columns disagree often (459 rows are _type 2 / _case_type 1), and quest 21001
# -- the second quest clearing stage 1-1 satisfies -- is exactly such a row: routing it
# on _type filed it under sp_quests, where the client never looked, so it could never
# read as complete.
QUEST_CASE_TYPE_MAIN = 1
QUEST_CASE_TYPE_SP = 2

# DesignQuestRow._type == QuestDefine's quest-type constants (QuestDefine..cctor,
# 0x1965a44): 1 Main, 2 Forver, 3 Daily, 4 EventForever, 5 Auto, 6 EventDaily, 7 OFA,
# 8 BP, 9 (Trainers-Gym / special-mode).
#
# We advance counters ONLY for the quest systems this server actually runs. The
# event/OFA/Battle-Pass/special-mode systems are NOT modelled -- we send no OFA
# notification dicts, no BP data, no active-event context -- so arming their quests
# creates orphan progress: a single tutorial summon (case_id 13) otherwise armed 35 OFA
# + 3 BP + 1 special quest that share that case_id, none of which the player can see or
# claim. That orphan progress is at best meaningless and at worst destabilises the
# client's PlayerQuest.AnalysisQuest, whose OFA/BP branches read the context we omit.
# The goal chain (type 1, INCLUDING the case_type-2 Cast Power-up steps) and the ordinary
# Forver/Daily/Auto quests stay in scope.
UNSUPPORTED_QUEST_TYPES = frozenset({4, 6, 7, 8, 9})


def _quest_is_sp(row):
    return row.get("_case_type") == QUEST_CASE_TYPE_SP
# SPQuestDB_Data{id, a_time, cnt, status}; QuestHasCompleted checks `status == 1`
# (LDR W8,[X0,#0x1C] in the type-2 branch).
SP_QUEST_COMPLETE = 1
# `PlayerQuest.GetQuestValue` (0x1963020) returns SPQuestDB_Data + 0x18 = **`cnt`** for a
# `_case_type` 2 quest, so `cnt` is the PROGRESS, not the requirement, and `status` is the
# claimed flag. An entry sitting at status 0 with cnt >= `_case_cnt` is what the client
# reads as "claimable" -- which is what puts it in normalWillCompletedQuestList and pops
# the "Mission Complete!" banner.
SP_QUEST_INPROGRESS = 0


def complete_quests(state, quest_ids):
    """Mark quests completed because the player CLAIMED them.

    Clearing a stage does not complete its quest -- it only makes it *claimable*
    (`CheckQuestWillbeCompleted`, driven by GetQuestValue >= CaseCnt). The player
    then taps "Collect Reward" and the client sends PlayerQuest cmd 257 with the
    ids; only that turns into a real completion. Doing it server-side at stage-clear
    time skips the claim the newbie tutorial is waiting on.

    Returns [(quest_id, item_id, item_cnt), ...] for the reward popup (cmd 513).
    """
    rewards = []
    rows = bt.dd.rows("quest")
    for qid in quest_ids:
        row = rows.get(int(qid))
        if not row:
            continue
        key = str(int(qid))
        if _quest_is_sp(row):
            # **setdefault is not enough.** Once bump_quest_counter has created the
            # entry to track progress, the key already exists, so setdefault silently
            # left status at 0 -- the client saw the quest as still claimable and kept
            # re-offering it (and re-paying the reward on every press).
            e = state["sp_quests"].setdefault(
                key, {"id": int(qid), "a_time": 0, "cnt": 0, "status": 0})
            e["cnt"] = max(int(e.get("cnt", 0)), row.get("_case_cnt") or 1)
            e["status"] = SP_QUEST_COMPLETE
        else:
            state["quests"].setdefault(key, 1)
        item_id, cnt = row.get("_item_id") or 0, row.get("_item_cnt") or 0
        if item_id and cnt:
            # routed by _action -- 21001 pays 10000x item 2, which is Mira, not a
            # 10000-deep backpack stack
            grant_reward(state, item_id, cnt)
        rewards.append((int(qid), item_id, cnt))
    return rewards


def complete_stage_quests(state, stage_id):
    """Mark every stage-clear quest satisfied by clearing `stage_id`. Returns the
    ids newly completed, so the caller can decide whether a push is worthwhile.

    Completion is stored in TWO different places depending on the quest's
    **`_case_type`** (see QUEST_CASE_TYPE_MAIN above -- NOT `_type`, which is a
    different column that frequently disagrees):
      _case_type 1 -> QuestSyncQuestData.quests.ContainsKey(id)
      _case_type 2 -> SPQuestSyncQuestData.sp_quests[id].status == 1
    Filing a completion in the wrong one is silently ignored, which is what happened
    to 21001 (the other quest that clearing stage 1-1 satisfies).
    """
    newly = []
    for qid, row in bt.dd.rows("quest").items():
        if (row.get("_case_id") != QUEST_CASE_CLEAR_STAGE
                or row.get("_case_v1") != int(stage_id)):
            continue
        # Same scope limit as bump_quest_counter: never complete event/OFA/BP/special
        # stage-clear quests (a later stage does appear in OFA quest chains).
        if row.get("_type") in UNSUPPORTED_QUEST_TYPES:
            continue
        key = str(qid)
        if _quest_is_sp(row):
            if key in state["sp_quests"]:
                continue
            state["sp_quests"][key] = {"id": qid, "a_time": 0,
                                       "cnt": row.get("_case_cnt") or 1,
                                       "status": SP_QUEST_COMPLETE}
        else:
            if key in state["quests"]:
                continue
            state["quests"][key] = 1
        newly.append(qid)
    return newly


# DesignQuestRow.get_CaseKey formats "{case_id}_{case_v1}", or
# "{case_id}_{case_v1}_{case_v2}" when _case_v2 >= 1.
def case_key(row):
    if (row.get("_case_v2") or 0) < 1:
        return f"{row.get('_case_id')}_{row.get('_case_v1') or 0}"
    return (f"{row.get('_case_id')}_{row.get('_case_v1') or 0}"
            f"_{row.get('_case_v2')}")


# `_case_id` values, read off the design `quest` rows' own English text. Every quest
# sharing a case id (and `_case_v1`/`_case_v2`) shares ONE counter -- case 20's twelve
# rows are all (v1 0, v2 0), so "Perform Cast Power-up", "Enhance any cast 30 times" and
# a mislabelled "Give presents" row all advance together by design.
QUEST_CASE_GACHA = 13
QUEST_CASE_SKILL_UP = 16        # "Complete Skill-up"     -> char_limit_up  (281)
QUEST_CASE_TRANSCEND = 18       # "Perform Transcend"     -> char_plus_up   (280)
QUEST_CASE_RANK_UP = 19         # "Perform Rank Up"       -> char_rank_up   (279)
QUEST_CASE_POWER_UP = 20        # "Perform Cast Power-up" -> char_level_up  (278)


def quest_completed(state, qid):
    """Has this quest been finished? 0/None means "no prerequisite", i.e. yes.

    Completion lives in two places depending on `_case_type` -- see the notes on
    bump_quest_counter -- so check both rather than guessing.
    """
    if not qid:
        return True
    key = str(int(qid))
    if key in state.get("quests", {}):
        return True
    e = state.get("sp_quests", {}).get(key)
    return bool(e) and e.get("status") == SP_QUEST_COMPLETE


def bump_quest_counter(state, case_id, amount=1):
    """Advance every quest counter with this _case_id, then record any completions.

    QuestDB_Data's wire key is `total` (not total_cnt), and the counter key is
    `DesignQuestRow.get_CaseKey` -- see case_key().

    **Bump the counter ONLY -- do NOT mark the quest completed here.** The client
    drives the rest: `PlayerQuest.AnalysisQuest` (0x19635A4) rebuilds
    `normalWillCompletedQuestList` from the counters, and when that list GROWS it does
    `PanelManager.LaunchPanel(...)` for the "Mission Complete!" toast; the player then
    claims via Quest cmd 257, which is what actually pays the reward
    (`complete_quests`). Marking the quest done server-side skips that entirely -- the
    quest is never *claimable*, so no banner appears and no reward is paid. That is a
    real regression I introduced and then backed out.

    (`complete_stage_quests` is different on purpose: a stage clear is a
    server-authoritative event, not a counter the client can re-derive.)
    """
    touched = []
    for qid, row in bt.dd.rows("quest").items():
        if row.get("_case_id") != case_id:
            continue
        # Skip quests belonging to systems we do not run (event/OFA/BP/special). Arming
        # them produced the orphan sp_quests that a fresh account should never have -- see
        # UNSUPPORTED_QUEST_TYPES.
        if row.get("_type") in UNSUPPORTED_QUEST_TYPES:
            continue
        if _quest_is_sp(row):
            # `_case_type` 2 progress is PER QUEST in sp_quests[id].cnt -- the shared
            # quest_db counter is not consulted for these at all.
            #
            # **Only advance a step whose predecessor is done.** These counters are
            # per-quest, so bumping every quest that shares the case id walked the whole
            # chain at once: one Power-up made steps 6, 10, 16, 24... all claimable
            # without the player touching them. The client gates DISPLAY on `_pre_quest`
            # but it does not gate the counter, so the server has to.
            if not quest_completed(state, row.get("_pre_quest") or 0):
                continue
            qkey = str(qid)
            e = state["sp_quests"].get(qkey)
            if e is None:
                e = {"id": int(qid), "a_time": 0, "cnt": 0,
                     "status": SP_QUEST_INPROGRESS}
                state["sp_quests"][qkey] = e
            if e.get("status") == SP_QUEST_COMPLETE:
                continue                       # already claimed; do not re-arm it
            e["cnt"] = int(e.get("cnt", 0)) + amount
            touched.append(qkey)
            continue
        key = case_key(row)
        if key in touched:
            continue
        state["quest_db"][key] = state["quest_db"].get(key, 0) + amount
        touched.append(key)
    return touched


def quest_json(state):
    """PlayerQuestData, the cmd-529 payload. It is an INCREMENTAL update: the
    handler merges `update_quests` into QuestSyncQuestData.quests and applies the
    remove_* lists, rather than replacing the collection, so re-sending the full set
    is harmless.

    Send EVERY field, even when empty. The handler applies each one only `if
    (field != null)`, and several PlayerQuest members it feeds have no lazy-init
    getter, so omitting a key leaves the live collection null and AnalysisQuest --
    which runs on every one of these replies -- throws a NullReference partway
    through. That abort leaves willbeCompletedQuestList half-rebuilt, which is what
    made the newbie flow read quest 31001 as "completable" rather than "completed"
    and send the player to PanelGoalQuest instead of the gacha step.
    """
    return json.dumps({
        # WIRE NAMES, from the [JsonProperty] thunks -- they are NOT the field names:
        #   update_db -> db_u    remove_db        -> db_r
        #   update_quests -> q_u remove_quests    -> q_r
        #   sp_quests -> sp_u    remove_sp_quests -> sp_r
        #   gq_db -> gq_db       gq_ow            -> gq_ow
        # Sending "update_quests" was silently ignored, so completions never reached
        # QuestSyncQuestData.quests and QuestHasCompleted stayed false forever.
        "db_u": {k: {"total": v} for k, v in state["quest_db"].items()},
        "db_r": [],
        "q_u": {k: v for k, v in state["quests"].items()},
        "q_r": [],
        "sp_u": dict(state["sp_quests"]), "sp_r": [],
        "gq_db": {}, "gq_ow": {},
    }, separators=(",", ":"))


# ClientBackpackType: StorageNormal=1, StorageEquipment=2, StorageSoulfrag=3,
# StorageBloodpact=4. BackpackItemData wire keys are the field names here:
# sid (slot id), iid (item id), amount, uid, attr.
BP_STORAGE_NORMAL = 1


# Where a granted item actually goes is decided by its DesignItemRow `_action`, with
# `_param1` naming the target. This is a SERVER-side rule: there is no use-item/grant
# path anywhere in the client binary -- it only ever displays what we push back -- so it
# is read off the data, where it lines up exactly with the CurrencyType/EnergyType enums
# we already know:
#   _action 5 -> currency `_param1`   (item 1 ダイヤ -> Cash 1, 2 魔界コイン -> Mira 16,
#                                      4 ギルドPT -> Guild 64)
#   _action 6 -> energy   `_param1`   (item 5 スタミナ -> Action 1)
#   anything else         -> an ordinary backpack item
# Inference, not decompiled: the mapping is not stated in code anywhere, but all four
# currency items and the stamina item agree with it, and nothing else uses _action 5/6.
ITEM_ACTION_CURRENCY = 5
ITEM_ACTION_ENERGY = 6


def item_bucket(item_id):
    """Where grant_reward would put this item, without granting it. Callers use it to
    decide which syncs to push -- a grant the client is never told about may as well not
    have happened."""
    row = bt.dd.row("item", item_id) or {}
    action, param = row.get("_action"), row.get("_param1") or 0
    if action == ITEM_ACTION_CURRENCY and param:
        return "currency"
    if action == ITEM_ACTION_ENERGY and param:
        return "energy"
    return "backpack"


def grant_reward(state, item_id, amount):
    """Give `amount` of `item_id`, routed the way its design row says. Returns the
    bucket it landed in so the caller knows which sync to push."""
    row = bt.dd.row("item", item_id) or {}
    action, param = row.get("_action"), row.get("_param1") or 0
    if action == ITEM_ACTION_CURRENCY and param:
        key = str(param)
        state["currency"][key] = int(state["currency"].get(key, 0)) + amount
        return "currency"
    if action == ITEM_ACTION_ENERGY and param:
        slot = state["energy"].setdefault(str(param), {"energy": 0, "cap": 0})
        slot["energy"] = slot.get("energy", 0) + amount
        return "energy"
    grant_item(state, item_id, amount)
    return "backpack"


def grant_item(state, item_id, amount, cbp_type=BP_STORAGE_NORMAL):
    """Add items to the backpack. Quest/gacha rewards only POP a popup client-side --
    nothing reaches the bag unless the server puts it there and re-syncs.

    Prefer grant_reward() unless you specifically mean a bag item: currency rewards
    (a quest paying 10000x item 2, say) must not become a backpack stack."""
    bag = state["backpack"].setdefault(str(cbp_type), {})
    for slot, entry in bag.items():
        if entry.get("iid") == item_id:
            entry["amount"] = entry.get("amount", 0) + amount
            return int(slot)
    sid = (max((int(k) for k in bag), default=0) + 1)
    bag[str(sid)] = {"sid": sid, "iid": item_id, "amount": amount,
                     "uid": "", "attr": {}}
    return sid


# ---- Starshards / runes (StorageEquipment = backpack type 2) ---------------
# "Starshards" in the EN UI are `Rune` internally -- PanelRune, char_wear_rune (295),
# SelectRune, and equipment.json IS the rune table. They live in backpack storage 2, which
# the login sync already pushes (backpack cmds 84-87 = storage 1-4); we simply never put
# anything there, which is why the Starshards screen reads `Inventory 0/999`.
#
# A rune is a plain BackpackItemData {sid, iid, amount, uid, attr}. `iid` is the
# `equipment` row id and `attr` is a Dictionary<string,int> whose keys were read off
# UIRuneInfo.SetData (0x189C2A4):
#   ppt_1 / ppv_1 / ppj_1   PRIMARY attribute type / value / job-use
#   ipt   / ipv   / ipj     the secondary "type" line drawn under the primary
#   bpt_<i> / bpv_<i>       sub-attribute i, i = 1..RuneBonusAttrNumber
# The client renders a stat's NAME as GetText(type % 100 + 10100) and its number via
# ItemStruct.GetValueText.
BP_STORAGE_EQUIPMENT = 2

# Game.Player.Char.CharAttribute type ids.
ATTR_HP, ATTR_ATK, ATTR_DEF = 1, 2, 3
ATTR_SPD, ATTR_CRI, ATTR_CDI = 5, 6, 8
ATTR_EHIT, ATTR_EANTI = 10, 11
ATTR_PHP, ATTR_PATK, ATTR_PDEF = 101, 102, 103

# CharAttribute.PercentStyleAttrs -- percentages are stored MULTIPLIED BY TEN.
# We no longer compute any of these ourselves (see below), but the mapping is worth
# keeping: it is what makes `GetValueText` render 420 as "42.0%".
PERCENT_ATTRS = {ATTR_CRI, ATTR_CDI, ATTR_EHIT, ATTR_EANTI,
                 ATTR_PHP, ATTR_PATK, ATTR_PDEF}

# EquipDefine: RunePrimaryAttrNumber 1, RuneBonusAttrNumber 4, RuneMaxLevel 15.
RUNE_PRIMARY_ATTRS = 1
RUNE_BONUS_ATTRS = 4
RUNE_MAX_LEVEL = 15

# --- what the server actually sends -----------------------------------------
# **The client CALCULATES every displayed stat; we only choose the sources.**
# `PlayerBackpack.RefreshEquipAttribute` (0x18E7FB0) runs on every storage-2 entry at
# login and calls `GetEquipGrowValue` (0x18E753C), which reads these keys out of `attr`,
# looks each id up in the `equipment_bonus` design form, and WRITES the derived values
# back into `attr`:
#     ppt_<i> = row._AttrType
#     ppv_<i> = row._AttrInitV + row._AttrUpV * lv
#     ppj_<i> = row._AttrJob
#     bpt_<j> = row._AttrType
#     bpv_<j> = (be_<j> + 1) * row._AttrInitV
# so sending ppt/ppv/bpt/bpv ourselves is pointless -- they are overwritten. Send the
# SOURCE ids instead and the numbers are automatically the game's own arithmetic.
#
# **`lv` is mandatory.** RefreshEquipAttribute does `attr["lv"]` with NO ContainsKey
# guard, so a non-empty attr without it throws KeyNotFoundException *inside the login
# sync*. The sync then never sets `synced`, and the client hangs forever on
# `Subsystem 'PlayerBackpack' still in syncing...` -- and because the entry is persisted,
# every later login hangs too. (An EMPTY attr is safe: the function returns early on
# Count == 0.)
#
# **`ts` is mandatory too, for runes specifically.** It is the acquisition timestamp:
# `UIRuneIcon.SetData(UIRuneData, BetterList<Rune>)` (0x18A3CAC) does a bare
# `attr["ts"]` -- no ContainsKey -- and compares it against
# `ClientPrefs.GAME_SETTING.RuneNewItemConfirmedTS` to decide the NEW! badge. Without it
# every icon in the Starshards grid throws KeyNotFoundException out of
# `PanelRune.UpdateCommonData`'s CreateContentList callback. The same literal is read by
# `PanelRune.InitPlayerRuneList`, both `RuneComparer.Compare`s (it is the default sort
# key), `PanelRuneCustomset.FilterRule` and `GetLastOwnGem`, so it is load-bearing well
# beyond the badge. `ihid` -- the secondary bonus row -- is by contrast genuinely
# optional: its only readers are `GetEquipGrowValue`, which guards it with ContainsKey,
# and `HandleBackpackChagne`.
RUNE_ATTR_TS = "ts"             # acquisition unix time; NEW! badge + default sort
RUNE_ATTR_LEVEL = "lv"          # rune level
RUNE_ATTR_PRIMARY = "pid_"      # + i, 1..RUNE_PRIMARY_ATTRS -- equipment_bonus row id
RUNE_ATTR_IHID = "ihid"         # the secondary bonus row id
RUNE_ATTR_SUB = "bid_"          # + j, 1..RUNE_BONUS_ATTRS  -- equipment_bonus row id
RUNE_ATTR_SUB_ENHANCE = "be_"   # + j -- enhance count; value = (be + 1) * initV

# Only `_Type == DesignEquipmentBonusDef.Attribute` (== 1) rows are treated as stats;
# type 2 is ReplaceSkill and is ignored by GetEquipGrowValue.
BONUS_TYPE_ATTRIBUTE = 1

# A starshard ITEM's `_action` IS its slot: 111..116 -> parts 1..6. That range is what
# `PlayerBackpack.GetItemSpace` (0x18EE554) routes to the starshard ("gem") list -- an
# item outside it returns null and the piece never reaches the Starshards inventory at
# all, which is why a `_action 2` box item showed 0/999 no matter what else was right.
RUNE_ACTION_BASE = 110          # slot = item._action - RUNE_ACTION_BASE
RUNE_ACTION_RANGE = range(111, 117)

# Each slot's fixed primary stat, read off the Chinese item names
# (攻擊/防御/生命/爆擊/命中/爆傷 = ATK/DEF/HP/CRI/Hit/CritDMG).
# **This refutes the "odd slots flat, even slots percent" claim** -- every slot has its
# own stat family, and the split is by stat, not by parity.
RUNE_SLOT_PRIMARY = {
    1: ATTR_ATK,    # 攻擊
    2: ATTR_DEF,    # 防御
    3: ATTR_HP,     # 生命
    4: ATTR_CRI,    # 爆擊
    5: ATTR_EHIT,   # 命中
    6: ATTR_CDI,    # 爆傷
}


def rune_slot(item_id):
    """The part a starshard item belongs to, or None if it is not a starshard."""
    action = (bt.dd.row("item", int(item_id)) or {}).get("_action")
    if action in RUNE_ACTION_RANGE:
        return action - RUNE_ACTION_BASE
    return None

# Sub-stats we are willing to roll. Values come from the design rows, so this is purely
# "which stats can appear".
RUNE_SUB_TYPES = (ATTR_HP, ATTR_ATK, ATTR_DEF, ATTR_SPD,
                  ATTR_PHP, ATTR_PATK, ATTR_PDEF,
                  ATTR_CRI, ATTR_CDI, ATTR_EHIT, ATTR_EANTI)


def _bonus_rows_by_attr():
    """{attr type: [equipment_bonus row id, ...]} for stat-bearing rows."""
    out = {}
    for rid, row in (bt.dd.rows("equipment_bonus") or {}).items():
        if row.get("_Type") != BONUS_TYPE_ATTRIBUTE:
            continue
        out.setdefault(row.get("_AttrType"), []).append(int(rid))
    return out


def rune_uid(state, n):
    """Per-rune key, same shape as the character uids (player id + counter)."""
    return f"{state['player_id']}r{n:04d}"


def make_rune(state, item_id, slot, level=0, enhance=0, rng=None):
    """Build one starshard as a BackpackItemData for storage 2.

    **`item_id` is an ITEM id, not an equipment id.** The client merges the design item
    row into the ItemStruct and resolves the suit through
    `DesignEquipmentForm.GetRow(runeData._param1)`, so the ITEM's `_param1` is what names
    the equipment row. Item 1001 is the ★1 Endearment shard (`_param1` 5211).

    `slot` (1..6) picks the primary stat; `level` scales it; `enhance` is how many extra
    rolls each sub-stat has had. Everything else the client derives -- see the notes on
    RUNE_ATTR_* above.
    """
    import random as _r
    rng = rng or _r
    # The item decides the slot; a mismatch would put the wrong primary on the piece.
    real_slot = rune_slot(item_id)
    if real_slot is None:
        raise ValueError(
            f"item {item_id} is not a starshard (_action must be 111..116); "
            "a non-starshard item never reaches the Starshards list")
    slot = real_slot
    by_attr = _bonus_rows_by_attr()

    def pick(attr_type):
        ids = by_attr.get(attr_type)
        return rng.choice(ids) if ids else None

    attr = {
        RUNE_ATTR_LEVEL: int(max(0, min(level, RUNE_MAX_LEVEL))),
        RUNE_ATTR_TS: int(time.time()),
    }
    primary = pick(RUNE_SLOT_PRIMARY.get(int(slot), ATTR_ATK))
    if primary is not None:
        attr[f"{RUNE_ATTR_PRIMARY}1"] = primary
    # Four distinct sub-stats, never repeating the primary's attribute.
    pool = [a for a in RUNE_SUB_TYPES
            if a != RUNE_SLOT_PRIMARY.get(int(slot)) and by_attr.get(a)]
    for j, attr_type in enumerate(rng.sample(pool, min(RUNE_BONUS_ATTRS, len(pool))),
                                  start=1):
        attr[f"{RUNE_ATTR_SUB}{j}"] = pick(attr_type)
        attr[f"{RUNE_ATTR_SUB_ENHANCE}{j}"] = int(enhance)
    return {"iid": int(item_id), "amount": 1, "attr": attr}


# ---- wearing starshards (Char 295 char_wear_rune -> 549 update_equip) -------
#
# `PlayerChar.equips_template` (.cctor 0x16A4648) is **18** empty strings, and
# `CharData.get_equips` falls back to it whenever `dbChar.equips` is null or empty. That
# 18 is the whole contract:
#
#   * The request (`RequestWearRune`, 0x16A0C78) sends NO intargs and
#     `strargs = [...the char's full 18-slot equips array..., char_uid]` -- char_uid
#     LAST. `UIRuneEquipment.OnClickBtnEquip` passes `_charEquips`, the complete array
#     with the player's change already applied, so the client tells us the state it
#     wants rather than which slot it touched. The server never has to know which index
#     is which; it validates and echoes.
#   * The reply (`receivedUpdateEquip`, 0x169A428) opens with
#     `if (strargs.Count != equips_template.Length + 1) return;` -- a **silent** no-op,
#     no log, no error popup. Answering 549 with anything but exactly 19 strargs looks
#     identical to not answering at all, which is what leaves the equip panel spinning.
#     It then reads `strargs[18]` as the char uid, does `charDic[uid]` via `get_Item`
#     (throws on a cast we do not hold), assigns slots 0..17 with
#     `DBCharData.UpdateEquipmentData(i, strargs[i])`, and finishes with
#     RefreshEquipTotalValues + InitWearDic + CharEvent 3.
CHAR_EQUIP_SLOTS = 18


def char_equips(entry):
    """A cast's 18-slot equips array, padded/truncated to the template length."""
    stored = list(entry.get("equips_list") or [])
    stored = [str(x or "") for x in stored[:CHAR_EQUIP_SLOTS]]
    return stored + [""] * (CHAR_EQUIP_SLOTS - len(stored))


def wear_runes(state, char_uid, equips):
    """Apply a char_wear_rune request. -> the stored 18-slot array.

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
    for i, uid in enumerate(list(equips)[:CHAR_EQUIP_SLOTS]):
        slots[i] = str(uid or "")
    owned = {e.get("uid") for e in state["backpack"]
             .get(str(BP_STORAGE_EQUIPMENT), {}).values()}
    for uid in slots:
        if uid and uid not in owned:
            raise ValueError(f"equip uid {uid!r} is not in storage {BP_STORAGE_EQUIPMENT}")
    # One piece can only be worn once: drop it from whoever else was wearing it.
    worn = {u for u in slots if u}
    for other_uid, other in state["roster"].items():
        if other_uid == char_uid:
            continue
        cur = char_equips(other)
        if any(u in worn for u in cur):
            other["equips_list"] = [("" if u in worn else u) for u in cur]
    entry["equips_list"] = slots
    return slots


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
    return entry, to_lv - before, cost


# ---- Soulmirrors (internally SOULFRAG, storage 3) ---------------------------
#
# **The EN UI calls these "Soulmirror"; the code calls them SoulFrag.** The item's
# `_itemName_2Lines_en` is "Soulmirror" while `_itemName_en` is just the character name
# ("Pride I"), which is why searching the EN names for "soul mirror" finds only shop
# blurbs. The Chinese name is 大破水晶 (Break Crystal). Search for "soulfrag".
#
# They are what fills the BREAK gauge on the character gallery, and the three tabs are
# `CharSoulType`: Normal=0 (base art), **Limit=1 (大破 "Break")**, **Super=2 (超大破
# "EX Break")**, **Ultra=3 (天啓 "Apoc.")**. Unlocking/showing a tab is Char 308
# `unlock_skin` / 309 `set_skin`, which take a CharSoulType -- NOT modelled here.
#
# `PlayerBackpack.GetItemSpace` (0x18EE554) routes by the item's `_action`:
#     111..116 -> gem list (starshards)   101..109 -> soulfrag list
#     131..133 -> bloodpact list          151..152 -> talisman
# so a soulmirror is any item with `_action` 101..109, and it lives in storage 3.
#
# **`_param3` is the CHARACTER id** the mirror belongs to (item 400001 -> 10001), and
# `_param2` is the rarity grade (1 普通 / 2 優良 / 3 稀有 / 4 史詩 / 5 傳說).
SOULFRAG_ACTION_RANGE = range(101, 110)

# Action -> index into the char's 18-slot equips array, read straight out of the jump
# table at 0x3703540 that `RequestWearSoulFrag` indexes with `itemAction - 101`.
# Note it is NOT contiguous: 101..106 map to 6..11 but 107..109 jump to 15..17, leaving
# 12..14 for bloodpacts. Three actions per CharSoulType tier, matching the three
# pentagon slots the panel shows per tab.
SOULFRAG_SLOT_INDEX = {
    101: 6, 102: 7, 103: 8,      # Limit  / "Break"
    104: 9, 105: 10, 106: 11,    # Super  / "EX Break"
    107: 15, 108: 16, 109: 17,   # Ultra  / "Apoc."
}
BP_STORAGE_SOULFRAG = 3
SOULFRAG_MAX_LEVEL = 15          # EquipDefine.SoulFragMaxLevel


def soulfrag_slot(item_id):
    """The equips index a soulmirror occupies, or None if it is not one."""
    action = (bt.dd.row("item", int(item_id)) or {}).get("_action")
    return SOULFRAG_SLOT_INDEX.get(action)


def soulfrag_owner(item_id):
    """The char id a soulmirror belongs to (item `_param3`)."""
    return (bt.dd.row("item", int(item_id)) or {}).get("_param3")


def _bonus_group_rows(item_id):
    """The stat-bearing equipment_bonus rows available to one equipment item.

    A soulmirror's own `equipment` row names a single `_bonusID` group (mirror 400001 ->
    equipment 1100011 -> group 10001, 15 rows), so unlike the starshards -- which pick
    from the global table by attribute type -- every id here is one the real item could
    actually have rolled.
    """
    row = bt.dd.row("item", int(item_id)) or {}
    equip = bt.dd.row("equipment", row.get("_param1")) or {}
    group = equip.get("_bonusID")
    return [int(rid) for rid, r in (bt.dd.rows("equipment_bonus") or {}).items()
            if r.get("_group") == group
            and r.get("_Type") == BONUS_TYPE_ATTRIBUTE
            and (r.get("_AttrInitV") or 0) > 0]


def make_soulmirror(state, item_id, level=0, enhance=0, rng=None):
    """Build one Soulmirror as a BackpackItemData for storage 3.

    Same attr scheme as a starshard -- storage 3 goes through the *same*
    `RefreshEquipAttribute`, so **`lv` is mandatory or login bricks**, and `ts` is
    mandatory for the icon (see the RUNE_ATTR_* notes).
    `UISoulFragIcon.SetData` additionally reads `ppt_1/ppv_1/ppj_1`, `ipt/ipv/ipj` and
    `bpt_1/bpv_1`. Those are DERIVED -- RefreshEquipAttribute writes them from `pid_1`,
    `ihid` and `bid_1` respectively -- so all three source ids are emitted here rather
    than only the primary, otherwise the derived key the icon reads never gets written.
    """
    import random as _r
    rng = rng or _r
    if soulfrag_slot(item_id) is None:
        raise ValueError(
            f"item {item_id} is not a Soulmirror (_action must be 101..109)")
    rows = _bonus_group_rows(item_id)
    if not rows:
        raise ValueError(f"Soulmirror {item_id} has no usable equipment_bonus rows")
    picks = rng.sample(rows, min(3, len(rows)))
    while len(picks) < 3:
        picks.append(picks[0])
    attr = {
        RUNE_ATTR_LEVEL: int(max(0, min(level, SOULFRAG_MAX_LEVEL))),
        RUNE_ATTR_TS: int(time.time()),
        f"{RUNE_ATTR_PRIMARY}1": picks[0],
        RUNE_ATTR_IHID: picks[1],
        f"{RUNE_ATTR_SUB}1": picks[2],
        f"{RUNE_ATTR_SUB_ENHANCE}1": int(enhance),
    }
    return {"iid": int(item_id), "amount": 1, "attr": attr}


def grant_soulmirror(state, item_id, level=0, enhance=0, rng=None):
    """Put a Soulmirror in storage 3. -> the stored entry."""
    bag = state["backpack"].setdefault(str(BP_STORAGE_SOULFRAG), {})
    sid = max((int(k) for k in bag), default=0) + 1
    n = len(bag) + 1
    uid = f"{state['player_id']}s{n:04d}"
    while any(e.get("uid") == uid for e in bag.values()):
        n += 1
        uid = f"{state['player_id']}s{n:04d}"
    entry = make_soulmirror(state, item_id, level, enhance, rng)
    entry["sid"] = sid
    entry["uid"] = uid
    bag[str(sid)] = entry
    return entry


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


def grant_rune(state, item_id, slot, level=0, enhance=0, rng=None):
    """Put a starshard in storage 2. -> the stored entry."""
    bag = state["backpack"].setdefault(str(BP_STORAGE_EQUIPMENT), {})
    sid = max((int(k) for k in bag), default=0) + 1
    n = len(bag) + 1
    uid = rune_uid(state, n)
    while any(e.get("uid") == uid for e in bag.values()):
        n += 1
        uid = rune_uid(state, n)
    entry = make_rune(state, item_id, slot, level, enhance, rng)
    entry["sid"] = sid
    entry["uid"] = uid
    bag[str(sid)] = entry
    return entry


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


def backpacks_all_json(state, storages=None, removed=None):
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
    """
    inner = {sid: {"sid": dict(items)} for sid, items in state["backpack"].items()
             if storages is None or int(sid) in storages}
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

    Published for BOTH boxes on purpose: 102 needs a non-null info for UpdateRoulette to
    reach its isEnabled=0 line and hide the duplicate lobby button.
    """
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
    """Spin box `box_id`. -> (ok, results, why)

    `results` is cmd 307's strargs[0]: `List<List<int>>`. `Roulette.OnRouletteDraw`
    (0x1AE6B6C) reads each inner list at **[1] = itemId and [2] = amount** (byte offsets
    +36/+40 off the int[] payload), NOT [0]/[1] like the wheel-slot lists -- there is a
    leading element it never touches, so index 0 is the box id here.

    The landing slot is not sent: the client walks its own `_iiDropItemList` and spins to
    the first icon whose ItemID **and** ItemCount both match the result. So a result must
    reproduce a wheel slot exactly, or the wheel spins to nothing.
    """
    box = str(int(box_id))
    if box not in (str(b) for b in ROULETTE_BOXES):
        return False, [], f"unknown box {box}"
    info = roulette_info(state, box)
    if info["day_drawsum"] >= info["draw_max"]:
        return False, [], "no draws left today"
    cost_id, cost_num = info["cost_item_id"], info["cost_item_num"]
    # spend_cost, not spend_item: the cost may be a currency-backed item id.
    if cost_id and cost_num and not spend_cost(state, cost_id, cost_num):
        return False, [], f"cannot pay {cost_num}x item {cost_id}"

    import random
    item_id, amount = random.choice(ROULETTE_SLOTS)
    grant_reward(state, item_id, amount)

    r = state.setdefault("roulette", {}).setdefault(box, {})
    r["day"] = int(r.get("day", 0)) + 1
    r["sum"] = int(r.get("sum", 0)) + 1
    # The bonus pays out when the spin counter reaches DrawMax, then the track resets.
    if r["sum"] >= ROULETTE_DRAW_MAX:
        for bid, bamt in ROULETTE_BONUS:
            grant_reward(state, bid, bamt)
        r["sum"] = 0
    return True, [[int(box), int(item_id), int(amount)]], ""


# ---- OFA (lobby entries and the ad-banner strip) ---------------------------
# `HandleSyncStaticOFABanner` (cmd 257) reads
#   strargs[0] -> List<OFABannerData>, sorted then indexed into StaticBannerDic by `id`
#   strargs[1] -> Dictionary<int,int> QuestToOFAMapping
# OFABannerData wire keys (from the JsonProperty thunks, none match the field names
# except type/status): **idx** (Index), **bgt** (BeginTime), **edt** (EndTime),
# **id** (OFAID), **type**, **strarg**, **status**.
#
# `id` refers to a `oneforall` (DesignOFAForm) row, which carries the art and the prefab
# variant: {_banner, _smbanner, _prefabName_v2, _group, _quest_cond, _shop_cond}.
# `PlayerOFA.RenewLobbyEntry` rebuilds groups **201** and **202** -- those are the lobby
# entry buttons (TriggerShop-*/BulletinShop-*), which is why our lobby is missing the
# NOVICE ULTRA PACK / SIGNUP REWARD / WEEKLY BEST SELLER row. `UIMainAdBanner` counts
# QuestToOFAMapping, so an empty one is the `_curBannerIndex out of range, idx=0` warning
# and the blank banner strip.
#
# Deliberately starting with a SHORT list: each entry pulls a prefab variant out of the
# bundles, and a missing prefab is exactly the kind of thing that has hung panels all day.
# Grow OFA_ENTRIES once a small set is proven to render.
# Verified on device 2026-08-03: these two produce real bulletin TABS with the correct
# artwork (リンボランド ★7日遊園体験 and 大罪×魔星 7日間パック, straight off each row's
# `_banner`), and the sync raises no exception. What is still blank is the bulletin's
# CONTENT PANE -- an OFABannerData entry is enough to create the entry and its art, but
# the body comes from the prefab variant's own data source (`strarg`, and the shop/quest
# state each variant reads), none of which we serve yet.
# Groups 201 and 202 are exactly what `RenewLobbyEntry` rebuilds, i.e. the lobby entry
# buttons the real game shows (NOVICE ULTRA PACK / SIGNUP REWARD / WEEKLY BEST SELLER /
# DEMON DESCENDED) and which ours is missing entirely. Group 1 holds the bulletin
# entries. Published wholesale rather than hand-picked, since the aim is to match the
# original lobby.
OFA_ENTRY_GROUPS = (1, 201, 202)


def _ofa_entry_ids():
    rows = bt.dd.rows("oneforall")
    return tuple(sorted(k for k, v in rows.items()
                        if v.get("_group") in OFA_ENTRY_GROUPS))


# Publishing ALL 55 rows of those groups threw `Index was out of range` out of an event
# handler (2026-08-03), so some entry in the set needs data we do not serve. Two entries
# are known good and render real bulletin tabs with correct art:
#   1     group 1   variant/BulletinQuest-Novice7Day
#   10511 group 202 common/BulletinComp-7DaySubscription_JP
# Set to None to publish every row in OFA_ENTRY_GROUPS once the bad entry is found --
# bisecting the 55 would identify it.
# 10511 was DROPPED 2026-08-05: it has no row in the EN 2.2.7 pack at all (group None,
# empty), which is the source of the recurring `集成式介面沒有row ID: 10511` warning. It
# rendered a second, Japanese-labelled roulette entry in the lobby that opened the SAME
# wheel as the real one -- live footage at EoS shows only one. It is a JP-era leftover we
# kept publishing after the EN migration.
OFA_ENTRIES = (1,)        # None = every row in OFA_ENTRY_GROUPS; () = publish nothing
OFA_QUEST_MAP = {}        # quest id -> OFA id, drives the lobby ad-banner strip

# EVENT banners (cmd 258) are a separate list from the static ones, and they are what
# gates the lobby roulette button: `HandleSyncEventOFABanner`'s RefreshGroupedDic
# (0x1960D3C) buckets each entry under its `oneforall` row's `_group`, and
# MenuBtnUpdater.UpdateRoulette hides the button whenever that group is empty or expired.
#
# Row **200019** (`common_ex+/TriggerEvent-FreeRoulette`) is the only member of group
# **213** in the EN 2.2.7 pack -- it is the roulette entry. Publishing it lights up the
# type-14 button whose prefab-authored Param1 is 213 and leaves the other one (a
# different, empty group) disabled, which is the single-entry lobby live footage shows.
OFA_EVENT_ENTRIES = (200019,)


def _ofa_content_strarg(ofa_id):
    """The banner's `strarg` -- it is NOT a free-text argument, it is the CONTENT.

    `OFABannerData..ctor(string strarg)` (0x195F004) is the only constructor, so
    Newtonsoft uses it and binds the `strarg` property to it; the ctor then does
    `DeserializeObject<OFAContentData>(strarg)` into the `ContentData` field (+0x38).
    Nothing else ever assigns ContentData -- cmd 259 does not. So an empty `strarg`
    leaves it NULL, and `PanelOneForAllMenu.OnSyncOFAContent` (0x158E360) ends with
    `CreateContent(banner.ContentData)`, which dereferences it: that was the
    `event execution error, message=Object reference not set to an instance of an object`
    and the black screen behind the roulette entry.

    `CreateContent` (0x158E534) only needs **id**: it does `DesignOFAForm.GetRow(OFAID)`,
    takes that row's prefab-name field, localizes it through
    `DesignPanelLocalizeForm.GetLocalizedPanelName`, and loads it.

    Wire keys from the 2.2.4 JsonProperty thunks (tools/read_json_keys.py):
    OFAContentData = **id / type / jstr / qstr / pstr** (OFAID, Type, JumpStr, QuestStr,
    ProgressStr). The same run reproduces OFABannerData's known idx/bgt/edt/id/type/
    strarg/status, which is what validates the method.
    """
    return json.dumps({"id": int(ofa_id), "type": 0,
                       "jstr": "", "qstr": "", "pstr": ""},
                      separators=(",", ":"))


def ofa_static_banner_json():
    """strargs[0] of cmd 257 -- the static banner list."""
    now = int(time.time())
    rows = bt.dd.rows("oneforall")
    out = []
    ids = _ofa_entry_ids() if OFA_ENTRIES is None else OFA_ENTRIES
    for i, ofa_id in enumerate(ids):
        if ofa_id not in rows:
            continue
        out.append({"idx": i, "bgt": 0, "edt": now + 365 * 24 * 3600,
                    "id": int(ofa_id), "type": 0,
                    "strarg": _ofa_content_strarg(ofa_id), "status": 0})
    return json.dumps(out, separators=(",", ":"))


def ofa_event_banner_json():
    """strargs[0] of cmd 258 -- the EVENT banner list (same OFABannerData shape).

    `status` MUST be non-zero. Unlike the static list, the cmd-258 handler folds each
    parsed banner into `EventBannerDic` by OFAID and, when `Status` (OFABannerData+0x30)
    is 0, *removes* the key instead of setting it -- so a status-0 entry is accepted,
    logged clean, and then silently dropped before RefreshGroupedDic runs. Read off the
    disassembly at 0x196079C..0x19607D4; it is why `status: 0` left group 213 empty and
    the roulette button hidden even though the reply was going out correctly.

    `edt` must be comfortably in the future: UpdateRoulette disables the button when
    GetNearestUnixEndTime(group) - now < 1 and no entry in the group is permanent.
    """
    now = int(time.time())
    rows = bt.dd.rows("oneforall")
    out = []
    for i, ofa_id in enumerate(OFA_EVENT_ENTRIES):
        if ofa_id not in rows:
            continue
        out.append({"idx": i, "bgt": 0, "edt": now + 365 * 24 * 3600,
                    "id": int(ofa_id), "type": 0,
                    "strarg": _ofa_content_strarg(ofa_id), "status": 1})
    return json.dumps(out, separators=(",", ":"))


def ofa_shop_cond(ofa_id):
    """The `oneforall` row's `_shop_cond` -- the shop id cmd 259 must name.

    0 for entries with no shop attached (the roulette, 200019). The client only uses it
    to make sure PlayerShop.ShopDic has an entry, so an unknown id is harmless.
    """
    row = bt.dd.rows("oneforall").get(int(ofa_id)) or {}
    return int(row.get("_shop_cond") or 0)


def ofa_quest_map_json():
    """strargs[1] of cmd 257 -- QuestToOFAMapping."""
    return json.dumps({str(k): int(v) for k, v in OFA_QUEST_MAP.items()},
                      separators=(",", ":"))


# ---- mail -----------------------------------------------------------------
# PlayerMail's dispatcher switch: replies are EVEN, requests ODD --
#   rply 2  SetMailList          <- req 3  (list)
#   rply 4  set_UnreadMailCount
#   rply 6  (unidentified)
#   rply 8  ReceiveAllAttachments <- req 5 (receive one) / 7 (receive all)
#   rply 10 DeleteMail            <- req 9
#
# **The list is CHUNKED**: `SetMailList(id, dataEnd, strArg)` appends strArg to a
# per-request-id buffer and only parses when `dataEnd` (intargs[1]) == 1. The old stub
# sent [0, 0], so it never finalised -- harmless only because it also sent "[]".
#
# A mail element's keys are the numeric strings "1".."10" (read off the JObject lookups
# in SetMailList), NOT names:
#   "1"  formatId  -> the DesignMailForm row, which supplies subject/content art
#   "2"  uid       -> string id, echoed back when claiming
#   "3"  read      -> bool; false puts it in UnreadMailList
#   "4"  content override
#   "5"  timestamp
#   "6"  nested object whose "7" is the attachment dict {item id: count}
#   "8"  custom    -> replaces the literal "{custom}" in the row's subject/content
#   "9"  expired   -> bool
#   "10" updatetime
# "{attachment}" in the row text is replaced by Mail.GetAttachmentString(). Nested
# values are read with ToString() then re-parsed, so plain nested JSON objects work;
# ConvertJsonTable only exists to rewrite an empty "[]" into "{}".
MAIL_SUBJECT_CUSTOM = ""


def _mail_json_one(m):
    return {
        "1": m.get("format_id", 0),
        "2": str(m.get("uid", "")),
        "3": bool(m.get("read", False)),
        "4": m.get("content", ""),
        "5": int(m.get("timestamp", 0)),
        "6": {"7": {str(k): int(v) for k, v in (m.get("items") or {}).items()}},
        "8": str(m.get("custom", MAIL_SUBJECT_CUSTOM)),
        "9": bool(m.get("expired", False)),
        "10": int(m.get("updatetime", 0)),
    }


def mail_list_json(state):
    """strargs[0] of the mail-list reply (cmd 2). Send it with dataEnd = 1."""
    return json.dumps([_mail_json_one(m) for m in state.get("mail", [])],
                      separators=(",", ":"))


def unread_mail_count(state):
    return sum(1 for m in state.get("mail", []) if not m.get("read"))


def claim_mail(state, uids=None):
    """Grant the attachments of the named mails (or every unread one when uids is None)
    and mark them read.

    The reply to a receive request is cmd 8 carrying **the list of claimed uids**;
    `ReceiveAllAttachments` deserialises it as List<string> and, for each match, marks the
    mail read, moves it to the read list and decrements the unread count. It does NOT
    grant anything -- it only pops the item display -- so the granting is ours, and the
    matching syncs have to be pushed like every other reward.

    Returns (claimed_uids, buckets_touched).
    """
    claimed, buckets = [], set()
    for m in state.get("mail", []):
        if m.get("read") or m.get("expired"):
            continue
        if uids is not None and str(m["uid"]) not in {str(u) for u in uids}:
            continue
        for item_id, count in (m.get("items") or {}).items():
            buckets.add(grant_reward(state, int(item_id), int(count)))
        m["read"] = True
        m["updatetime"] = int(time.time())
        claimed.append(str(m["uid"]))
    return claimed, buckets


def add_mail(state, format_id, items=None, custom="", timestamp=None):
    """Queue a mail. `items` is {item_id: count} and is what the player claims."""
    mails = state.setdefault("mail", [])
    uid = str(max((int(m["uid"]) for m in mails), default=0) + 1)
    mails.append({
        "uid": uid, "format_id": int(format_id), "items": dict(items or {}),
        "custom": str(custom), "read": False, "expired": False,
        "timestamp": int(timestamp if timestamp is not None else time.time()),
        "updatetime": int(time.time()),
    })
    return uid


# ---- login bonus ----------------------------------------------------------
# PlayerLoginBonus asks with server cmd **1** on index 0xFE16052C and the reply is cmd
# **17** on 0xFFB98ABA -- this subsystem does NOT follow the usual "reply = servercmd +
# 0x100" rule (its whole switch is 17/19/33/49). Build layout A (uint64_msg), and
# `HandleSyncCmd` bails unless strargs has **exactly one** entry: a LoginBonusSyncData.
#
# Wire keys are NOT the C# property names (read off the JsonProperty thunks):
#   LoginBonusSyncData: group_data, active, nextreset, total_day
#   GroupInfo:          group, dcnt (day), bcnt (book), tcnt (total),
#                       duration_s (start), duration_e (end), cin_book (checkin_book),
#                       cin_day (checkin_day), banner (bannerID), text_id (textID), close
#
# The ladders themselves ARE in the pack: `login_bonus` rows {_id,_group,_day,_book,
# _mail} and each `_mail` row carries the payout as `_itemID` + `_param`. Group 1 is the
# 28-day ladder, group 3 the 14-day OB event.
#
# UNVERIFIED semantics (first cut -- the panel is the oracle): `book` looks like which
# pass through the ladder you are on (login_bonus rows carry `_book`), `dcnt` the day
# reached and `cin_day` the day already claimed, with duration_s/e bounding the event.
LOGIN_BONUS_GROUP = 1          # 1 = 28-day ladder, 3 = the 14-day OB event
LOGIN_BONUS_BOOK = 1
# a wide window so the ladder is always live rather than expired
LOGIN_BONUS_WINDOW = 365 * 24 * 3600


def login_bonus_json(state):
    """strargs[0] of the login-bonus sync (cmd 17). Exactly one string, or the handler
    bails."""
    now = int(time.time())
    day = int(state.get("login_day", 1))
    claimed = int(state.get("login_claimed_day", 0))
    rows = bt.dd.rows("login_bonus")
    total_days = sum(1 for r in rows.values() if r.get("_group") == LOGIN_BONUS_GROUP)
    group = bt.dd.rows("login_bonus_group").get(LOGIN_BONUS_GROUP, {})
    return json.dumps({
        "group_data": [{
            "group": LOGIN_BONUS_GROUP,
            "dcnt": day,
            "bcnt": LOGIN_BONUS_BOOK,
            "tcnt": total_days,
            "duration_s": now - LOGIN_BONUS_WINDOW,
            "duration_e": now + LOGIN_BONUS_WINDOW,
            "cin_book": LOGIN_BONUS_BOOK,
            "cin_day": claimed,
            "banner": group.get("_banner_id") or 0,
            "text_id": group.get("_text_id") or 0,
            "close": 0,
        }],
        "active": LOGIN_BONUS_GROUP,
        "nextreset": now + 24 * 3600,
        "total_day": int(state.get("login_total_days", day)),
    }, separators=(",", ":"))


# ---- karma (AVG decision rewards) -----------------------------------------
# Picking an option in a story scene pays a currency amount plus "karma" (favour) with
# one character. Karma is the `flv`/`fxp` pair on CharIDData: rank and rank progress.
#
# The reply the client wants is PlayerStage cmd 21 with EXACTLY four ints,
# `[currencyType, currencyValue, charID, fexp]` -- see titan_server. What each one does,
# read out of OptionButton.SetReward:
#   currencyType -> GetCurrencyTypeSpriteID, which ONLY maps type 1 (ダイヤ) to a sprite
#                   (3001); every other type returns 0 and the icon stays blank
#   charID       -> a DesignRoleModelInfoForm row (i.e. an ordinary char id) whose
#                   portrait is shown; 0 leaves the portrait untouched
#   fexp         -> the "+N" label, and picks the banner text by MAGNITUDE alone:
#                   >=100 "Ultimate Up!" (10086), >=20 "Big Up!" (10087), else "Up!"
#                   (10088). The "Karma Rank" part of that banner is static prefab text,
#                   so it is NOT evidence that a rank threshold was actually crossed.
MAX_FLV = 30                 # CharDefine.MaxFLv
# The karma curve was assumed server-side and stubbed at a flat 10 xp per rank. It is
# NOT server-side: Formula.GetCharFLVupNeedXP computes it client-side and the panel
# renders the result, so it was recoverable after all -- see char_flv_need_xp below.


# (avg id, option index) -> (currencyType, currencyValue, charID, fexp).
# Reconstructed from footage, not from data -- the option->reward mapping was
# server-side and the `avg` design form is not even in our pack. Observed in the
# tutorial, both paying the same character (the banner portrait does not change):
#
#   decision 1, top option    -> 5 ダイヤ, +10 karma   ("Up!")
#   decision 2, second option -> 10 ダイヤ, +20 karma  ("Big Up!")
#
# Those banner labels are an independent check on the decompile: OptionButton.SetReward
# picks "Up!" under 20 and "Big Up!" from 20, which is exactly what the two clips show.
#
#   decision 3, second option -> 15 ダイヤ, +100 karma ("Super Up!" in that build; our
#                                pack's text 10086 says "Ultimate Up!" -- same >=100
#                                branch, the EN wording just differs between builds)
#
# Those banner labels are an independent check on the decompile: OptionButton.SetReward
# picks "Up!" under 20, "Big Up!" from 20 and the third string from 100, which is
# exactly what the three clips show.
#
# The payout is **hardcoded per (scene, option)** -- it is not derivable from the option
# index or the scene ordinal, so the table below has to be rebuilt from video and each
# row needs its own clip. The three observations line up with nothing in particular:
# decisions 1/2/3 paid 5/10/15 gems while the options picked were 1/2/2, so the currency
# is not a function of either on its own. Footage only ever shows the branch that was
# taken, so an option's siblings stay unknown until someone picks them.
#
# **A choice is permanent per difficulty.** Replaying a stage on the same difficulty
# re-shows the scene with the previous option already locked in; only a different
# difficulty lets you pick again. That is what `avg_choices` records, and it is fed back
# to the client as intargs[0] of the AVG sync reply (see titan_server). It also settles
# the payout question: a scene pays the FIRST time it is decided and never again.
KARMA_TUTORIAL_CHAR = 11001            # Jacqueline; the portrait matches her, unconfirmed

# Observed in the tutorial, as (decision, option picked, gems, fexp). These become
# KARMA_REWARDS entries as soon as a run tells us the avg ids -- the handler logs them.
KARMA_OBSERVED = [
    (1, 1, 5, 10),
    (2, 2, 10, 20),
    (3, 2, 15, 100),
]
KARMA_REWARDS = {
    # (avg_id, option): (currency type, amount, char id, fexp)
    #
    # The tutorial's three decisions, in story order. The avg ids come from the AVG
    # *sync* line in our own server log (10102 -> 10103 -> 10107) and the option indices
    # from a playthrough that picked 1st / 2nd / 2nd -- exactly the branches the clips
    # show, which is what lets the amounts be attached to specific ids. Option indices
    # are 0-based.
    (10102, 0): (CUR_CASH, 5, KARMA_TUTORIAL_CHAR, 10),
    (10103, 1): (CUR_CASH, 10, KARMA_TUTORIAL_CHAR, 20),
    (10107, 1): (CUR_CASH, 15, KARMA_TUTORIAL_CHAR, 100),
}
# Until an entry exists, pay the smallest observed tier rather than nothing, so an
# undocumented decision still moves the story and still reads as a reward.
KARMA_DEFAULT = (CUR_CASH, 5, KARMA_TUTORIAL_CHAR, 10)


def karma_reward(avg_id, option):
    """(currencyType, amount, charID, fexp) for a decision, or the default."""
    return KARMA_REWARDS.get((int(avg_id), int(option)), KARMA_DEFAULT)


def avg_choice(state, avg_id):
    """The option already locked in for this scene, 0 if it has never been decided."""
    return state.setdefault("avg_choices", {}).get(str(avg_id), 0)


def set_avg_choice(state, avg_id, option):
    """Record a decision. Returns False if this scene was already decided, in which case
    the reward must NOT be paid again -- the client re-shows the locked-in option."""
    seen = state.setdefault("avg_choices", {})
    if str(avg_id) in seen:
        return False
    seen[str(avg_id)] = int(option)
    return True


def karma_reward(avg_id, option):
    return KARMA_REWARDS.get((int(avg_id), int(option)), KARMA_DEFAULT)


def grant_currency(state, cur_type, amount):
    """Credit a CurrencyType directly. The AVG reply names a currency type rather than
    an item id, so this is the one grant path that does not go through a design row."""
    key = str(cur_type)
    state["currency"][key] = int(state["currency"].get(key, 0)) + amount
    return state["currency"][key]


def karma_of(state, char_id):
    return state.setdefault("karma", {}).setdefault(
        str(char_id), {"flv": 1, "fxp": 0})


# Formula.GetCharFLVupNeedXP(lv, charData): karma XP to go from rank `lv` to `lv` + 1.
#   5 * floor( (155 + 5 * rarity * (lv+1)^1.64) / 5 )
# i.e. rarity-scaled and rounded down to a multiple of 5. Verified against the panel:
# Lucifer (rarity 5) at karma rank 1 gives 2^1.64 = 3.1187, x25 = 77.97, +155 = 232.97,
# /5 = 46.59 -> 46, x5 = 230, exactly the "0/230 to next level" the client shows.
#
# This replaced a flat KARMA_XP_PER_RANK = 10, which was a placeholder and wrong at
# every rank -- karma was rolling over roughly 23x too fast for a rarity-5 cast.
def char_flv_need_xp(rarity, lv):
    """Karma XP required to advance a cast of this design rarity from `lv` to `lv`+1."""
    return 5 * math.floor((155 + 5 * int(rarity or 0) * ((lv + 1) ** 1.64)) / 5)


def grant_karma(state, char_id, fexp):
    """Add favour xp to a character, rolling over into ranks. Returns its karma."""
    k = karma_of(state, char_id)
    rarity = (dd.row("char", char_id) or {}).get("_rarity")
    k["fxp"] = k.get("fxp", 0) + fexp
    while k["flv"] < MAX_FLV:
        need = char_flv_need_xp(rarity, k["flv"])
        if need <= 0 or k["fxp"] < need:
            break
        k["fxp"] -= need
        k["flv"] += 1
    if k["flv"] >= MAX_FLV:
        k["flv"], k["fxp"] = MAX_FLV, 0
    return k


# ---- game rule ------------------------------------------------------------
# PlayerGeneral.HandleSyncGameRuleCmd (cmd 514) deserializes strargs[0] into
# GeneralSyncGameRuleData. Most of its fields are copied across null-safe, but these
# five `List<string>` ones are each iterated WITHOUT a null check to build a
# List<Decimal> (Decimal.TryParse per entry, with a "data.<Field> parse error." log on a
# miss), so a missing key is a NullReferenceException out of the handler. An empty array
# is enough -- the loop exits immediately on size 0.
#
# The keys are snake_case and do NOT match the C# field names; they come from the
# JsonProperty attribute thunks (e.g. 0x1043400 -> "soulfrag_enhance_coin_magnification").
GAME_RULE_MAGNIFICATION_KEYS = (
    "soulfrag_enhance_coin_magnification",
    "soulfrag_enhance_dust_magnification",
    "soulfrag_enhance_coin_char_rarity_magnification",
    "soulfrag_enhance_dust_char_rarity_magnification",
    "soulfrag_enhance_item_char_rarity_magnification",
)


# NEW IN 2.2.7. GeneralSyncGameRuleData gained `SuperLimitDefineData SuperLimitDefine`
# (offset 0xB8), wire key "super_limit_define" (thunk 0x11520D0 in the 2.2.7 libil2cpp).
# Its three members are max_super_limit / personal_effect / team_effect (thunks
# 0x11293EC / 0x1129424 / 0x112945C).
#
# Leaving it out lands PlayerGeneral.SuperLimitDefine as null, and 2.2.7's
# PanelCharacterInformation.InitCharInfo dereferences it for the new super-limit widgets
# (_lbPopSuperLimitTitle, _lbPopSuperLimitEffect, _tsSuperLimit, _goSwitchToSuperLimit) --
# a NullReferenceException there leaves the cast detail panel permanently hung.
#
# Both getters have now been read (2026-08-05), so the semantics are no longer guesses:
#
#   CharData.get_isSuperLimitAvailable  ->  charRow._rarity == 5
#       Super Limit / "ULTRA" is purely RARITY-GATED. Only rarity-5 casts can ever use
#       it; the ULTRA tab on the Transcend panel shows _goSuperLimitLock (a chain) for
#       everyone else, and no amount of progression unlocks it.
#
#   CharData.get_superLimitEffect       ->  personalEffect * dbChar.super_limit / 10.0
#       i.e. a percentage with one decimal, scaling linearly with the cast's own
#       super_limit level. `max_super_limit` caps that level.
#
# CALIBRATED 2026-08-05 from the in-game "Ultra Transcend" help screen, which states
# "each personal Ultra Level increases the cast's damage bonus/reduction by 1% (up to
# 20%)" and shows a cast at "Ultra LV 6 /20" displaying +6% / +6%:
#   max_super_limit = 20     -- straight off the "6 /20" readout
#   personal_effect = 10     -- solves superLimitEffect = personalEffect * lv / 10.0
#                               for 1% per level: 10 * 6 / 10.0 = 6.0% at level 6
#
# team_effect is STILL unknown. The help says "The Total Ultra Level is the sum of all
# owned casts' Ultra Levels", that in battle "the personal total Ultra Level is
# calculated as the sum of personal and total Ultra Level", and that "the total damage
# bonus/reduction is capped at 40%, and enemy Ultra Level is deducted when calculating
# damage". So personal contributes up to 20% and the team share makes up the rest of the
# 40% cap. But the field has NO C# reader anywhere in the binary (same signature as
# SellMoneyStar and the Soul Link score table), so its rate is applied server-side and
# cannot be derived here -- and setting it would change nothing client-side; it would
# only matter once OUR battle code applies it. Left 0 so nothing is silently buffed.
#
# The help also states "(Only Demon Lords, Angels and Riders can Ultra Transcend.)" --
# alignments 100/101/102. That is consistent with, not contrary to, the `_rarity == 5`
# test in the binary: those three alignments are exactly the rarity-5 casts, while the
# Awaker buckets 103/104 are rarity 4 and 3.
SUPER_LIMIT_DEFINE = {
    "max_super_limit": 20,
    "personal_effect": 10,
    "team_effect": 0,
}


# Three more GeneralSyncGameRuleData dictionaries that must be PRESENT but may be empty.
# Formula.GetPlusUpFoodNeedMoney / GetPlusUpFoodOfferPXP read
# PlayerGeneral.SpecialPlusUpMaterial and throw outright when it is null -- the null
# check jumps to the throw, NOT to the formula fallback; the fallback only runs when the
# dictionary exists and simply has no entry for that char id. Leaving it out hung the
# Transcend panel with a real stack trace:
#     NullReferenceException
#       at Formula.GetPlusUpFoodNeedMoney
#       at PanelCharacterUpgrade.SetSurmountCost
#       at PanelCharacterUpgrade.UpdateCurrency
#       at PanelCharacterUpgrade.OnUpgradeSurmountIn
# An empty dict is the correct value for us: these are per-char-id live-ops overrides
# ([0] = plus-XP offered, [1] = coin cost) and with none present every material falls
# back to the standard star formulas, which is what the panel already displays.
# superRankUpMaterial/superRankUpCost are the same shape for the star-6 ULTRA path
# (SetRankUpCost dereferences them unconditionally too), so send them empty as well.
GAME_RULE_EMPTY_DICTS = (
    "special_plus_up_material",
    "super_rank_up_material",
    "super_rank_up_cost",
)


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
    d["soulfrag_enhance_item_dic"] = soulfrag_enhance_item_dic()
    d["soulfrag_enhance_show_item_id_dic"] = soulfrag_enhance_show_item_dic()
    # PlayerGeneral.get_SoulfragTransmuteDefaultNum dereferences this dictionary with no
    # null guard -- leaving it out is a NullReferenceException out of
    # PanelSoulFrag.InitTransmuteInfo the moment the transmute tab opens (seen on device
    # 2026-08-08). One entry per rarity keyed by rarity string.
    d["soulfrag_transmute_num"] = {str(r): 1 for r in SOULFRAG_RARITIES}
    d["soulfrag_transmute_cost"] = 0
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
SPR_BANNER_AWAKER, SPR_TAB_AWAKER = 767, 777           # Awaker Summon
SPR_BANNER_DEBUT, SPR_TAB_DEBUT = 763, 773
SPR_BANNER_ANGEL, SPR_TAB_ANGEL = 765, 775             # Virtue Soulmirrors
SPR_BANNER_SOULMIRROR, SPR_TAB_SOULMIRROR = 764, 774   # Sin Soulmirrors
SPR_BANNER_DEBUT_ALT = 768                             # unidentified art
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

# Published pull rates. The remaining 88.5% is 3★ fodder.
GACHA_RATE_5 = 0.025
GACHA_RATE_4 = 0.09


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
    (1002, SPR_BANNER_SINS, SPR_TAB_SINS, "Summon", True, 202, 1),
    # Soulmirrors are the ITEM gacha (魂鏡 are items), so char_only must be False or
    # ShowGachaAnim skips straight to the results screen.
    (1004, SPR_BANNER_SOULMIRROR, SPR_TAB_SOULMIRROR,
     "Sin Soulmirrors", False, 1400430, 0),
    (1005, SPR_BANNER_ANGEL, SPR_TAB_ANGEL,
     "Virtue Soulmirrors", False, 1400414, 0),
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
    boxes += [_gacha_box(bid, img, ban,
                         _cost_standard(scroll, free_single=bool(dly) and free),
                         sort=len(boxes) + i + 1, char_only=co, daily=dly)
              for i, (bid, img, ban, _name, co, scroll, dly)
              in enumerate(REGULAR_BOXES)]
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
SOULMIRROR_GACHA_BOXES = {1004: 100, 1005: 101}
# Mirror grade (`_param2`): 1 普通 N / 2 優良 R / 3 稀有 SR / 4 史詩 UR / 5 傳說 LR.
# Weighted to match the published char rates so the banner's advertised odds stay
# coherent; the real per-banner table was live-ops data we do not have.
SOULMIRROR_GACHA_GRADES = ((5, GACHA_RATE_5), (4, GACHA_RATE_4))
SOULMIRROR_GACHA_FALLBACK_GRADE = 3


def _soulmirror_gacha_pool(alignment):
    """{grade: [item id, ...]} for every mirror belonging to that alignment's cast."""
    chars = {int(cid) for cid, row in (bt.dd.rows("char") or {}).items()
             if row.get("_alignment") == alignment}
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
    bump_quest_counter(state, QUEST_CASE_GACHA)
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
    STAR5 = (100, 101, 102, 103)
    STAR4, FODDER = (104,), (9001,)

    def _bucket(alignments, listed=True):
        return [r for r in rows.values()
                if r.get("_alignment") in alignments
                and (bool(r.get("_order")) or not listed)
                and any(r.get("_growStar") or [])]

    five, four, fod = (_bucket(STAR5), _bucket(STAR4),
                       _bucket(FODDER, listed=False))
    if not (five and four and fod):
        return []
    # The TUTORIAL roll is scripted -- 1x 5★, 1x 4★, 8x fodder -- and must stay that way;
    # it is the spread the scripted first-pull sequence is written around. The gate is
    # the same one gacha_json uses to decide whether to show the newbie box at all.
    if not state.get("gacha_count"):
        plan = [(five, 5), (four, 4)] + [(fod, 3)] * (count - 2)
    else:
        # Published rates: 5★ 2.5%, 4★ 9%, 3★ 88.5%, with 4★-or-better GUARANTEED in a
        # 10-pull. Rolled per pull -- reusing the tutorial's fixed spread made every
        # later 10-pull identical.
        plan = []
        for _ in range(count):
            r = random.random()
            if r < GACHA_RATE_5:
                plan.append((five, 5))
            elif r < GACHA_RATE_5 + GACHA_RATE_4:
                plan.append((four, 4))
            else:
                plan.append((fod, 3))
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
    return results


def _gacha_star(char_row):
    """Star tier whose RAW _growStar slot is non-zero. battle._default_star now reads
    the raw ladder too (and caps at MAX_STAR, so it can never return a super rung), so
    this is that same rule."""
    return bt._default_star(char_row)


def add_char(state, char_id, lv=1, star=None, super_star=0):
    """Put a new character in the roster (what charDic/pro_chars renders from)."""
    n = len(state["roster"]) + 1
    uid = _char_uid(state["player_id"], n)
    while uid in state["roster"]:
        n += 1
        uid = _char_uid(state["player_id"], n)
    state["roster"][uid] = {"id": char_id, "lv": lv, "xp": 0, "plus": 0,
                            "star": star, "super_star": super_star}
    return uid


def spend_cost(state, item_id, amount):
    """Deduct a cost that may be a CURRENCY rather than a bag item.

    Gacha costs are quoted as item ids, but item 1 (Diamond) and 2 (Coin) carry
    `_action 5` = ITEM_ACTION_CURRENCY with `_param1` naming the CurrencyType, so they
    live in `currency`, not the backpack. Routing them through spend_item would look at
    an empty bag and always refuse. -> True if the player could pay.
    """
    row = bt.dd.row("item", item_id) or {}
    if row.get("_action") == ITEM_ACTION_CURRENCY and row.get("_param1"):
        key = str(int(row["_param1"]))
        have = int(state["currency"].get(key, 0))
        if have < amount:
            return False
        state["currency"][key] = have - amount
        return True
    return spend_item(state, item_id, amount)


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


def spend_item(state, item_id, amount, cbp_type=BP_STORAGE_NORMAL):
    """Deduct items; returns True if the player had enough."""
    bag = state["backpack"].get(str(cbp_type), {})
    for slot, entry in list(bag.items()):
        if entry.get("iid") == item_id:
            if entry.get("amount", 0) < amount:
                return False
            entry["amount"] -= amount
            if entry["amount"] <= 0:
                del bag[slot]
            return True
    return False

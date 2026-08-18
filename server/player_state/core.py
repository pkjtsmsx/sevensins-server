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
import json, math, os, shutil, threading, time

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
#
# **FIFTEEN, not fourteen: the cast list is not the only reader.**
# `PanelSoulFrag.ResetSoulShardComparer` (0x1638D78) indexes the SAME list by its own
# _panelActionType -- case 0 -> 10, case 5 -> 11, case 1 -> 12 and **case 6 -> index
# 14**, each behind a size check that throws. Case 6 is the Soulmirror FUSE tab, so at
# 14 entries tapping FUSE threw ArgumentOutOfRangeException out of
# OnSoulFragTransmuteIn and the panel silently kept showing the upgrade view -- the tab
# highlighted, nothing else changed. Fourteen was right for every screen that had been
# exercised and wrong for the one that had not.
CHAR_SORT_SLOTS = 15
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
        # The player's guild, or None when they are in none. A guild of one -- see
        # player_state.guild for why that is the honest shape here rather than a
        # compromise. Creating one costs 200,000 Mira and needs stage 4-10 cleared
        # (the client greys the button out until then; we do not re-check it).
        "guild": None,
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
#
# **22 "Weekly Guild Pass" is not one of those four dungeons** but belongs on the same
# refill: it is the Guild Weekly entry ticket, PanelGuildWeekly reads its count
# directly for the "n / 3 challenges" label, and the 3 it prints beside it is
# `ConstantDefine.MaxChallengeTimes` -- the same floor, arrived at independently.
DAILY_PASS_ITEM_IDS = (16, 17, 18, 19, 22)
DAILY_PASS_FLOOR = 3
# Owned by battle.py so the Starshard Temple's set rotation and every daily reset
# here share one boundary; they used to disagree (the Temple flipped at midnight).
DAILY_RESET_HOUR = bt.DAILY_RESET_HOUR


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
                if (_seed_roster(st) | _clamp_roster_stars(st) | _refill_passes(st)
                        | _purge_orphan_sp_quests(st)):
                    _save_locked(st)
                return st
            except Exception:
                # **NEVER fall through to a fresh account here.** The old code did, and
                # writing that default straight back DESTROYED the save: a transient
                # failure in the normalisation helpers -- design_data raising because the
                # cache was cold and this interpreter has no TypeTreeGeneratorAPI -- was
                # enough to turn a 100 KB account into a tutorial state, silently.
                # Preserve the file, hand back what could be read, and let the caller
                # see a real error if even that failed.
                stamp = time.strftime("%Y%m%d-%H%M%S")
                keep = f"{p}.unreadable-{stamp}"
                try:
                    shutil.copy2(p, keep)
                except Exception:
                    keep = "(copy failed)"
                raise RuntimeError(
                    f"refusing to overwrite {p}: it could not be loaded cleanly "
                    f"(kept a copy at {keep}). Fix the cause, do not delete the file."
                )
        st = _default(player_id)
        _seed_roster(st)
        _save_locked(st)
        return st


def _purge_orphan_sp_quests(state):
    """Drop sp_quests entries for quests belonging to systems we do not run.

    **These are old damage that persists.** Before UNSUPPORTED_QUEST_TYPES existed,
    bump_quest_counter advanced every quest sharing a `_case_id`, event/OFA/battle-pass
    rows included -- so three gacha pulls armed forty-odd quests across twenty chains at
    once. bump_quest_counter has skipped those types for a while now, but nothing ever
    removed the entries it had already written, and the client renders them: the goal
    list comes out in the wrong order and shows steps from chains the player has not
    started. A device save carried 40 such entries against 1 real one.

    Keyed on the design row's `_type`, so a quest that no longer exists in the pack is
    also dropped -- it can never be displayed or claimed either. Returns True if
    anything changed, so load() knows to rewrite the file.
    """
    sp = state.get("sp_quests")
    if not sp:
        return False
    rows = bt.dd.rows("quest") or {}
    doomed = [k for k in sp
              if (rows.get(int(k)) or {}).get("_type") in UNSUPPORTED_QUEST_TYPES
              or int(k) not in rows]
    for k in doomed:
        del sp[k]
    return bool(doomed)


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
# ...and as a CURRENCY key, which is what state["currency"] is indexed by.
CURRENCY_COIN = 16

# **Diamonds are TWO balances and the client quietly spends both.**
# `PlayerCurrency.Balance` (0x18F8298) special-cases type 1:
#     Balance(Cash) = BalanceDetailed(1) + BalanceDetailed_RealCash()
# and `BalanceDetailed_RealCash` (0x18F83F0) reads type **32** (48 for `IsDMMUser`).
# The lobby tooltip says it in words: "Free diamond will be consumed first. Paid
# diamond will cover the insufficient part." So every affordability check the client
# makes is against free + paid, and a server that debits only key 1 refuses purchases
# the player was shown as affordable -- gacha drew, displayed the roll, then granted
# nothing once free diamonds dropped below the price.
CURRENCY_CASH = 1          # CurrencyType.Cash -- free diamonds
CURRENCY_CASH_PAID = 32    # RealCash; DMM builds use 48, which we do not serve


def diamond_balance(state):
    """What the client's `Balance(CurrencyType.Cash)` reports: free + paid."""
    cur = state["currency"]
    return (int(cur.get(str(CURRENCY_CASH), 0))
            + int(cur.get(str(CURRENCY_CASH_PAID), 0)))


def spend_diamonds(state, amount):
    """Charge diamonds free-first, paid for the remainder. -> True if affordable."""
    amount = int(amount)
    if amount <= 0:
        return True
    cur = state["currency"]
    free = int(cur.get(str(CURRENCY_CASH), 0))
    paid = int(cur.get(str(CURRENCY_CASH_PAID), 0))
    if free + paid < amount:
        return False
    take_free = min(free, amount)
    cur[str(CURRENCY_CASH)] = free - take_free
    cur[str(CURRENCY_CASH_PAID)] = paid - (amount - take_free)
    # "[Monthly] Spend over 5000 diamonds" -- quest case 2014, counted in diamonds
    # spent rather than purchases made.
    bump_quest_counter(state, QUEST_CASE_SPEND_DIAMOND, amount)
    return True


def item_count(state, item_id, cbp_type=None):
    """How many of an item the player is holding."""
    bag = state["backpack"].get(str(cbp_type if cbp_type is not None
                                    else BP_STORAGE_NORMAL), {})
    return sum(e.get("amount", 0) for e in bag.values() if e.get("iid") == item_id)


def has_item(state, item_id, amount):
    return item_count(state, item_id) >= amount


def char_rarity(char_id):
    row = dd.row("char", char_id) or {}
    return int(row.get("_rarity") or 1)

# DesignCharRow._rarity -> displayed star, read out of the 2.2.7 binary at 0x3703B10
# (PanelCharacterList.GetBrowsableIllustrationList indexes it as [_rarity - 1]).
_CHAR_STAR = (1, 3, 4, 5, 5)


def char_star(rarity):
    """Displayed star count for a design rarity (1..5)."""
    try:
        return _CHAR_STAR[int(rarity) - 1]
    except (IndexError, TypeError, ValueError):
        return 0


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


def char_sort_list(state):
    """The `sort_list` to publish, padded to CHAR_SORT_SLOTS.

    **Padding on READ is the point, not just on write.** Accounts created before a slot
    count went up carry a shorter list on disk, and the login sync would happily send
    that stale length -- so raising CHAR_SORT_SLOTS alone fixes only brand-new accounts
    and leaves every existing save crashing the panel that needed the extra slot. This
    is cheap and idempotent; call it anywhere the list goes out or gets written.
    """
    slots = list(state.get("sort_list") or [])
    if len(slots) < CHAR_SORT_SLOTS:
        slots += [DEFAULT_CHAR_SORT] * (CHAR_SORT_SLOTS - len(slots))
    return slots


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
# "Go to <shop> and exchange <item>" -- `_case_v1` is the GOODS id, not a parameter to
# a shared counter, so this case must always be bumped with case_v1= (see
# bump_quest_counter). Its rows are also the source of the original server's goods ids;
# see docs/ECONOMY_GAPS.md.
QUEST_CASE_BUY_GOODS = 2003
# "[Monthly] Spend over 5000 diamonds"; counts the diamonds, not the transactions.
QUEST_CASE_SPEND_DIAMOND = 2014


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


def bump_quest_counter(state, case_id, amount=1, case_v1=None, to=None):
    """Advance every quest counter with this _case_id, then record any completions.

    `to` switches from INCREMENT to a high-water SET: the counter becomes
    `max(current, to)` and `amount` is ignored. Some cases are not events but derived
    state -- case 26 is "the best level any one starshard has reached" and case 27 is
    "how many starshards are at level >= _case_v1" -- and those have to be recomputed
    and stored, not added to. Taking the max rather than assigning keeps a quest that
    is already armed from un-arming itself when the player dismantles a shard.

    `case_v1` narrows the match to rows whose `_case_v1` equals it, and is REQUIRED for
    any case where that column is a discriminator rather than a parameter. Case 2003
    ("buy goods N") is the clear example: its rows name eight different goods, so
    bumping on case id alone would credit buying the Monthly Pass, every Step Gift Box
    and Mammon's three free bundles the moment the player bought a Grimoire.

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
        if case_v1 is not None and (row.get("_case_v1") or 0) != int(case_v1):
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
            e["cnt"] = (max(int(e.get("cnt", 0)), int(to)) if to is not None
                        else int(e.get("cnt", 0)) + amount)
            touched.append(qkey)
            continue
        key = case_key(row)
        if key in touched:
            continue
        cur = state["quest_db"].get(key, 0)
        state["quest_db"][key] = (max(cur, int(to)) if to is not None
                                  else cur + amount)
        touched.append(key)
    return touched


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


def grant_reward(state, item_id, amount):
    """Give `amount` of `item_id`, routed the way its design row says. Returns the
    bucket it landed in so the caller knows which sync to push."""
    row = bt.dd.row("item", item_id) or {}
    action, param = row.get("_action"), row.get("_param1") or 0
    # **A starshard or soulmirror is an INSTANCE, not a stack.** Bagging one puts a
    # stackable row in Normal storage, where `GetItemSpace` cannot file it and the
    # player never sees it -- the piece has to be rolled into its own storage with a
    # uid and attributes. Found 2026-08-18: the goal step that pays a "★3 LR
    # Starshard" selector resolved a real shard id and then bagged it, so the popup
    # named a shard the player never received.
    if action in RUNE_ACTION_RANGE:
        from .gear import grant_rune              # local: gear imports core, not us
        for _ in range(max(1, int(amount))):
            grant_rune(state, item_id, rune_slot(item_id) or 1)
        return "equipment"
    if action in SOULFRAG_SLOT_INDEX:
        from .gear import grant_soulmirror        # local: gear imports core, not us
        for _ in range(max(1, int(amount))):
            grant_soulmirror(state, item_id)
        return "equipment"
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


def _bonus_rows_by_attr(item_id=None):
    """{attr type: [equipment_bonus row id, ...]} for stat-bearing rows.

    **Pass `item_id`.** Without it this pools the WHOLE 2043-row table, and that table
    is not a starshard roll pool -- it also holds boss/GM rows. Row 907 is
    `_AttrType 6` (CRI) with `_AttrInitV 9990`, and percentages are stored x10, so a
    starshard that rolled it displayed **CRI+999.0%** next to sub-stats of 1.5%. (Seen
    in game 2026-08-18; the character sheet then read CRT 200.0%, the client's own cap.)

    The real pool is the piece's own `equipment._bonusID` group, reached through the
    item's `_param1` -- exactly what `_bonus_group_rows` has always done for
    Soulmirrors. Those groups are built for this: group 2001 (★1 Chaos) is 33 rows =
    11 attributes x 3 quality tiers, every value small and sane.

    The global form is kept only for callers that genuinely want "every row of this
    attribute"; nothing rolling an item should use it.
    """
    rows = bt.dd.rows("equipment_bonus") or {}
    allowed = None if item_id is None else set(_bonus_group_rows(item_id))
    out = {}
    for rid, row in rows.items():
        if row.get("_Type") != BONUS_TYPE_ATTRIBUTE:
            continue
        if allowed is not None and int(rid) not in allowed:
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
    # Scoped to THIS piece's bonus group -- see _bonus_rows_by_attr for the 999% crit
    # that the unscoped pool produced.
    by_attr = _bonus_rows_by_attr(item_id)
    if not by_attr:
        raise ValueError(
            f"starshard {item_id} has no equipment_bonus rows in its own group "
            f"(equipment {(bt.dd.row('item', int(item_id)) or {}).get('_param1')}); "
            "refusing to roll from the global table")

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
    # **Omitting this one is a NullReferenceException, not an empty preview.**
    # `Formula.GetItemCountDecomposeSoulFrag` (0x18F78EC) dereferences
    # SoulfragDecomposeItemDic BEFORE any ContainsKey guard, so a key we never sent
    # left it null and selecting a mirror on the DISMANTLE tab threw out of
    # PanelSoulFrag.GetBrowsableDecomposeItemList -> UpdateSelectRelateInfo. Sending
    # `{}` is both safe and correct: the very next line is
    # `if (!dic.ContainsKey(rarity)) return empty`, so an empty dictionary short-
    # circuits to "no bonus items", which is exactly what our refund grants.
    "soulfrag_decompose_item_dic",
)

# Char-rarity multipliers on the DISMANTLE payout. **These are List<int>, not the
# List<string> decimals the *enhance* magnifications use** -- GetItemCountDecomposeSoulFrag
# indexes them with a 4-byte stride and then tests `< 1`, so a "1" string would be read
# as garbage. Indexed by charRarity - 1, and the function bails unless that is 0..4, so
# five entries are the minimum; six matches the enhance lists.
#
# The first is only reached once soulfrag_decompose_item_dic is non-empty (the dic's
# ContainsKey returns first today), but it is dereferenced with NO null guard the moment
# it is -- so it ships now rather than becoming the next crash. The ultrabreak one is
# the sfType-3 path, which does guard, but there is no reason to treat it differently.
GAME_RULE_DECOMPOSE_MAGNIFICATION_KEYS = (
    "soulfrag_decompose_item_char_rarity_magnification",
    "soulfrag_ultrabreak_decompose_item_char_rarity_magnification",
)


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


def unequip_everywhere(state, uids, keep=None):
    """Take `uids` off every cast wearing them. -> {char_uid: rebuilt 18-slot array}.

    Every path that DESTROYS a piece (dismantle, fuse, a forge material) has to do
    this, or a cast keeps an equips slot pointing at a uid that no longer exists. The
    returned map is not bookkeeping -- the caller must send a `char_update_equip` (549)
    for each entry, because `PlayerChar.receivedUpdateEquip` (0x169A428) only rewrites
    the one cast named in the request. `keep` is a char uid to leave out of the map
    (the requester, whose own 549 the caller sends anyway).
    """
    gone = {str(u) for u in uids if u}
    affected = {}
    for char_uid, entry in state["roster"].items():
        cur = char_equips(entry)
        if any(u in gone for u in cur):
            entry["equips_list"] = [("" if u in gone else u) for u in cur]
            if char_uid != keep:
                affected[char_uid] = entry["equips_list"]
    return affected


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
        # Diamonds (item 1) draw on the paid balance too -- see spend_diamonds.
        if key == str(CURRENCY_CASH):
            return spend_diamonds(state, amount)
        have = int(state["currency"].get(key, 0))
        if have < amount:
            return False
        state["currency"][key] = have - amount
        return True
    return spend_item(state, item_id, amount)


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

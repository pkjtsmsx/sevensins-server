#!/usr/bin/env python3
"""Battle server: builds the payloads PlayerBattle consumes and tracks a fight.

The client is a pure renderer here -- it owns no combat logic. Everything (who is
in the field, their HP, whose turn it is, what a skill did) is decided by the
server and pushed as BattleRpcClientCmd messages, so this module is the actual
game engine for combat.

Message map (Game.Player.Battle.PlayerBattle.OnClientCmdReceived):
  1100 WaveBegin   strargs[0] = BattleCmd json
  1101 StartTurn   intargs[0] = 1 -> perform 8 else 4, strargs[0] = BattleCmd json
  1200 Judge       1201 Attack     1500 Retreat    1501 AutoSet
  1503 NextWave    1504 ChangeChar 1505 ServerStart(BattleDatas) 1506 WaveEnd
  1507 RuneList    1508 SelectRune 1601 CharCurInfo 1801 Sync

Every JSON key below is the [JsonProperty] wire name pulled from the attribute
thunks in libil2cpp.so -- they are nothing like the C# field names ("stage_id",
"l_units", "mob_group_ids", ...), so do not "fix" them to match the class.
"""
import json
from typing import NamedTuple
import os
import re

import design_data as dd
import battle_effects as fx

# CALIBRATION HOOK (temporary, pairs with patch_design.py's lattice): the served
# formation table is a 10x10 lattice of candidate positions rather than 5 real slots,
# so a unit's field position is whichever lattice index we hand it. Sweep positions
# with e.g. SEVENSINS_SLOTS="44,45,46,47,48" / SEVENSINS_ESLOTS="54,55,56" and a
# server restart -- no bundle re-patch, no 4 MB re-download on the device.
PLAYER_SLOTS = [int(s) for s in os.environ.get("SEVENSINS_SLOTS", "").split(",") if s]
ENEMY_SLOTS = [int(s) for s in os.environ.get("SEVENSINS_ESLOTS", "").split(",") if s]

# EnTeam
TEAM_PLAYER, TEAM_ENEMY = 1, 2
# BattleType.Stage
BATTLE_TYPE_STAGE = 0

# server -> client (PlayerBattleClientCmd) and client -> server (PlayerBattleServerCmd)
BATTLE_CLIENT_INDEX = 0xFBC2FA08
BATTLE_SERVER_INDEX = 0xFA6D759E

CMD_WAVE_BEGIN, CMD_START_TURN = 1100, 1101
CMD_JUDGE, CMD_ATTACK = 1200, 1201
CMD_RETREAT, CMD_AUTO_SET, CMD_NEXT_WAVE = 1500, 1501, 1503
CMD_CHANGE_CHAR, CMD_SERVER_START, CMD_WAVE_END = 1504, 1505, 1506
# BattleType.Stage -- campaign/story fights (Arena=1, Challenge=2, ...).
BATTLE_TYPE_STAGE = 0
CMD_RUNE_LIST, CMD_SELECT_RUNE = 1507, 1508
CMD_CHAR_INFO = 1601
CMD_REPLAY_RECORD = 1903

# TurnEndState.OnWaveEnd branches on the first WaveEnd intarg: 1 means "wave won,
# carry on" (DoWaveClear + AfterWaveWin, which either runs to the next wave or ends
# in GameWin); anything else drops a normal stage into GameLose.
WAVE_RESULT_WIN, WAVE_RESULT_LOSE = 1, 2

# client -> server, from PlayerBattle.ServerRPC* (Ready sends 0x64 with [auto])
REQ_READY, REQ_START_TURN = 100, 101
REQ_JUDGE, REQ_ATTACK = 200, 201
REQ_NEXT_WAVE, REQ_BATTLE_END = 502, 505
# PlayerBattle's login-sync request -- SHARES BATTLE_SERVER_INDEX with every real
# in-fight cmd above (attack, judge, retreat, ...), so the dispatch loop must route
# this one to build_sync_replies()/battle_sync_reply(), never to battle_replies(): once
# a resumed Battle exists at login time (see restore_battle), the two would otherwise
# collide on the very first request the client sends, and HandleSyncCmd -- the ONLY
# thing that can ever set reconnectCase and let the "rejoin?" prompt fire -- gets
# silently swallowed by battle_replies' "no handler for this cmd" catch-all instead of
# answered. Caught by an actual device test: the client hung at a fixed % on load,
# waiting forever on a sync reply that was being routed to the wrong handler.
REQ_BATTLE_SYNC = 801
# Menu -> Retreat. `PlayerBattle.Retreat` sends this with NO args, but only after
# `BattleData.BattleResultType >= 2` -- which passes normally because BattleDatas..ctor
# seeds that field with 0xFF ("no result yet"); HandleWaveEnd only overwrites it on the
# FINAL wave. The reply is CMD_RETREAT (1500), and until it arrives the battle cannot be
# left, which is why Retreat appeared to do nothing.
REQ_RETREAT = 500
# The rest of PlayerBattleServerCmd. Contracts read out of PlayerBattle's Handle*
# methods in the 2.2.7 dump (see docs/rpc_coverage.md).
REQ_AUTO = 501          # ServerRPCSetAuto(set)          -> 1501
REQ_CHANGE_CHAR = 504   # ServerRPCChangeChar(order)     -> 1504
REQ_SELECT_RUNE = 508   # ServerRPCRuneSelect(idx)       -> 1508
REQ_CHAR_INFO = 601     # ServerRPCCharInfo(order)       -> 1601
REQ_RECONNECT = 701     # CB_Reconnect()                 -> 1505 (resend BattleDatas)
REQ_REPLAY = 802        # ServerRPCReplayBattle(...)     -> 1903
REQ_REPLAY_LAST = 803   # ServerRPCReplayLastBattle()    -> 1903
REQ_REPORT_ERROR = 804  # ServerRPCReportError(skillID)  -> no reply cmd exists

# HandleReplayBattleRecord accumulates strargs[0] across intargs=[chunk, total] and,
# when the assembled string is exactly this sentinel, clears it and shows ConfirmMsg 33
# ("no replay data") instead of entering ReplayMode. Read from the binary rather than
# guessed: the comparison at 0x168B444 points at 0x494B5C8.
REPLAY_NONE = "NIL"

MAX_SLOTS = 5          # design mob lists are always 5 CSV slots

# Unit "order" keys are plain sequential numbers starting at 101 -- players first,
# then the enemy wave. This is not cosmetic: AttackState.CheckForceTutorial compares
# the tapped target's order against the literal "103" and rejects the tap otherwise,
# which with a 2-character tutorial party (101, 102) makes 103 the first mob.
ORDER_BASE = 101


_ratio_cache = {}
_level_cache = {}

# Skills are levelled: each rank is its OWN row, sharing a `_group` and differing in
# `_lv` and coefficient (Leviathan's basic runs 180% at I to 280% at VI). The id in
# DesignCharForm._skills is the rank-I row, so publishing those makes the client show
# "I" and hit for rank-I damage. Gameplay footage of the tutorial shows every skill
# suffixed IV, so the party starts at rank 4.
SKILL_RANK = 4

# The blue bar under each portrait is the MOVE GAUGE, not an ultimate charge.
# `UICharStatus.SyncBar` (0x2000A74) draws it as `LightBattleChar.Scv / 100`, and three
# other things name it for what it is: skill prose says "boosts the caster's Move Gauge
# by 30%", BattleDatas carries a `CharListByScv` queue next to `ActionOrderList`, and
# DamageInfo mode 4 is `DamageMode.SCV` (AttackBehavior.ShowScvBar). So the fight is an
# ATB: every unit's gauge fills at its own SPD and whoever fills first takes the turn.
# Treating it as a per-turn +25 charge is what made the bar read ~0 on your own turn.
SCV_FULL, ULTIMATE_SLOT = 100, 2
# The ultimate's own gate is DesignSkillRow._charge (design column "charge", 0..6 and
# only ever set on _type 3 SP skills): the number of the unit's OWN turns it must bank
# before the button lights up. 0 means "ready from the first turn".
DEFAULT_ULTIMATE_CHARGE = 0
# Gauges are floats, so "full" is a hair below SCV_FULL -- an exact compare can leave
# the ATB step with nobody ready.
FULL_EPS = SCV_FULL - 1e-6
# DesignSkillRow._type (SkillType enum): 4 = PASSIVE. A passive's effects are event-
# driven (battle_start / on_counter), not fired by an active use -- see battle_effects.
SKILLTYPE_PASSIVE = 4

# Test probe for the bloodpact aura. SEVENSINS_BLOOD_EFFECT accepts either
#   "series,rank"   -- force one pair on every unit, or
#   "spread[,rank]" -- give each unit a DIFFERENT series, so one battle shows many at
#                      once instead of a restart per candidate.
# Unset means the shipped behaviour (no aura). See Unit.blood_effect.
BE_SERIES_MAX = 15                      # fx_state_1..15 in art_fx_prefab_states

# **The class aura.** Series 101 is not a bloodpact family -- it is the per-CLASS aura,
# and the only two ranks that exist are 001 and 005, matching exactly the two special
# jobs. `fx_state_101_001` is byte-for-byte the parts of the artist's `fx_state_dark_004`
# (butterfly / groundDark / groundring / smokeDark_ground) and `fx_state_101_005` those
# of `fx_state_sacred_004` (groundRing / light_center / smallStar) -- but only the 101_*
# pair carries an FxBloodPact component, so only they are loadable. That is why the aura
# shows on Abyssal-Prime-class (job 1) and Solar-Prime-class (job 5) casts and nothing
# else. DesignCharRow._job is 2/3/4 for the ordinary STR/AGI/TEC classes.
CLASS_AURA_SERIES = 101
CLASS_AURA_JOBS = (1, 5)

# Series 1..15 are the 15 BLOODPACT families, in item-id order: 721001 Frenzy ...
# 722401 Unlaws, so family = (iid // 100) % 100 - 9. Confirmed by matching the numeric
# prefabs to the artist-named ones by child hierarchy (fx_state_6_004 ==
# fx_state_eclipse_004, 13 == solar). Ranks 001..004 grow denser with each step
# (series 6 goes 3 -> 5 -> 12 -> 17 particles).
BLOODPACT_ID_BASE = 9                   # (iid//100)%100 of the first family, minus 1
BLOODPACT_AURA_RANKS = 4
BLOODPACT_LV_MAX = 15                   # player_state.gear.BLOODPACT_MAX_LV


def bloodpact_aura(item_id, level):
    """(series, rank) for a worn bloodpact, or None if it is not one we can render.

    Rank is banded off the pact's LEVEL, so the aura grows as the pact is enhanced --
    which is what "they grow with each limit break" describes. It is the one part of
    this not proven against the live game: pact `_rarity` is also 1..4 and would fit
    the four ranks exactly. Series 101 shows rank is not always a size, so neither
    reading is safe to assume; change BLOODPACT_RANK_FROM_LEVEL to flip it.
    """
    family = (int(item_id) // 100) % 100 - BLOODPACT_ID_BASE
    if not 1 <= family <= BE_SERIES_MAX:
        return None
    if BLOODPACT_RANK_FROM_LEVEL:
        band = BLOODPACT_LV_MAX / BLOODPACT_AURA_RANKS
        rank = min(BLOODPACT_AURA_RANKS, int(max(0, level) // band) + 1)
    else:
        rank = _clamp(int((dd.row("item", int(item_id)) or {}).get("_rarity") or 1),
                      1, BLOODPACT_AURA_RANKS)
    return (family, rank)


BLOODPACT_RANK_FROM_LEVEL = True


def _parse_be(raw):
    raw = (raw or "").strip().lower()
    if not raw:
        return None
    parts = raw.split(",")
    if parts[0] == "spread":
        # spread[,rank[,start]] -- `start` walks the window, since a field of 8-9 units
        # cannot show all 15 series at once.
        rank = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 3
        start = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
        return ("spread", max(1, rank), max(1, start))
    try:
        a, b = (int(x) for x in parts)
        return (a, b) if a >= 1 and b >= 1 else None
    except (ValueError, TypeError):
        return None


_BE_OVERRIDE = _parse_be(os.environ.get("SEVENSINS_BLOOD_EFFECT"))


def skill_ranks(char_row, limit_with_suit):
    """Per-SLOT skill rank, mirroring CharData.GetCharSkillLimitLvs.

    Every slot starts at rank 1; the char row's `_skillUp` is a sequence of 1-based slot
    numbers, and the first `limit_with_suit` of them each bump that slot's rank by one.
    `limit_with_suit` = limit_char + limit_book + limit_suit + super_star (the cast's
    total limit). So a fresh cast (limit 0) has every skill at rank 1 -- which is why
    hardcoding rank 4 showed rank-IV skills the player had never upgraded to.
    """
    seq = dd.csv_ints(char_row.get("_skillUp") or "")
    ranks = [1, 1, 1, 1]
    for i in range(min(int(limit_with_suit or 0), len(seq))):
        slot = seq[i] - 1
        if 0 <= slot < len(ranks):
            ranks[slot] += 1
    return ranks


def skill_at_rank(skill_id, rank=SKILL_RANK):
    """The row for the same skill at the given rank, clamped to what exists."""
    key = (skill_id, rank)
    if key in _level_cache:
        return _level_cache[key]
    row = dd.row("skill", skill_id) or {}
    group = row.get("_group")
    resolved = skill_id
    if group:
        ranks = sorted((r for r in dd.rows("skill").values()
                        if r.get("_group") == group),
                       key=lambda r: r.get("_lv") or 0)
        if ranks:
            pick = min(ranks, key=lambda r: abs((r.get("_lv") or 0) - rank))
            resolved = pick["_id"]
    _level_cache[key] = resolved
    return resolved


def defend_ratio(defense):
    """Fraction of damage the target's DEF removes.

    This is `Game.Player.Char.CharFunction.GetDefendRatio` read straight off the
    client, not a guess: below 500 DEF it is a very shallow `def/5000`, so a stage-1
    boss at 166 DEF only shaves 3.3% -- an invented `def/(def+300)` curve took off
    36% and made the story bosses unbeatable.
    """
    if defense <= 500:
        return defense * 0.01 / 50.0
    if defense <= 3000:
        return ((defense - 500) / 100.0 + 10.0) * 0.01
    # the >3000 branch uses another constant; clamp at the 3000 value (35%)
    return 0.35


# `_growStar` is TWO ladders stacked in one 12-entry array: rungs 0..5 are the normal
# star tiers (10001..10006 for Lucifer), rungs 6..11 the super-star ones (110001..110006).
# DesignCharRow.GetCharGrowRow picks between them with `star - 1 + super_star`, so
# super_star is an OFFSET, not a flag -- `isSuperStarOpen` is merely `_growStar[6] != 0`.
# Star tops out at 6 (UpgradeDefine.RankUpMaterialCount is int[6] and PlayerChar's
# rank-up check throws on anything outside 1..6) and super_star at UpgradeDefine's
# MaxSuperStar, also 6 -- which is exactly the 12 rungs.
MAX_STAR = 6
MAX_SUPER_STAR = 6

# Rating condition types, from the switch in DesignStageRow.PaserRatingData (the label
# each one pulls out of DesignTextForm says what it means):
#   1 -> 11121 "Stage cleared!"
#   2 -> 11122 "Ally cast(s) defeated within {0} times"
#   3 -> 11123 "Clear in at most {0} turns"
#   4 -> 11124 "Clear with Kizuna quest's leading cast"; 5..22 are other modes
RATING_CLEARED, RATING_MAX_DEATHS, RATING_MAX_TURNS = 1, 2, 3
# **Kinds 11..15 are "clear with at least [3] ally casts of a JOB", and the job is the
# kind itself minus 10.** DesignChar `_job` is 2 STR / 3 AGI / 4 TEC (text 12013/12014/
# 12015 in that order), and the Hell Express bears it out: B01 is kind 14 and the panel
# reads "at least 1 TEC ally cast(s)", B02 is kind 12 and reads STR. Label text is
# 11216, "Clear with at least {1} {0} ally cast(s)".
#
# This is what pays the GRIMOIRE FRAGMENTS: 60 of the Hell Express's rating rows are
# kinds 12/13/14, carrying items 541/542/544 and the selector boxes. Reporting 0 for
# them (the old catch-all) is why clearing the line paid nothing but coin and diamonds.
RATING_JOB_BASE = 10
RATING_JOB_MIN = (11, 12, 13, 14, 15)
# **Kind 20 is "be affected by <status> at most N times".** The row is
# [20, item, count, SKILL id, times] -- 6002 is skill "Fracture" (ATK down), 6004 is
# "Slow" (SPD down), and the panel renders text 11222 for it: "Fracture 5 time(s) at
# most". The EN string names no subject, but the CN pairs it with 11225
# (「{0}」至少{1}次通關) whose EN spells the subject out as "**Receive** {0} {1} time(s)
# at least" -- same construction, so 20 is the "at most" half of that receive pair,
# i.e. it counts what lands on OUR casts, not what we inflict.
RATING_STATUS_MAX = 20
# Kind 5 is "Level Up" (text 11125): did a party member gain a level off this clear?
# The battle cannot know -- XP is granted by the caller after the fight -- so the
# caller sets `levelled` before reading the flags. It stays False for a loss, which is
# right: no XP is paid for one.
RATING_LEVEL_UP = 5
# `_rating_datas1..4` -- four is the whole set the stage table carries.
RATING_SLOTS = 4

# Per-wave value fed to BattleDatas.i_plays, which PaserInterludeData grades into a box
# rank. It must be NON-ZERO (0 is the "no box, hide the slot" sentinel) and is compared
# against the stage's ItemsRank pair, so 1 lands in the lowest grade for every stage. The
# real per-wave value is server-side -- it looks like a score the box tier is bought with
# -- so this is a placeholder that simply guarantees a visible box per wave.
INTERLUDE_BOX_VALUE = 1

# 魔界コイン, DesignItemRow _action 5 / _param1 16 -> CurrencyType.Mira.
COIN_ITEM_ID = 2
# See Battle.drops(): fitted to footage of a 1-1 clear (3 waves, 3 coin icons of 250).
COIN_PER_WAVE = 250

# Per-stage drop tables, [(item id, count), ...], reconstructed from footage one stage at
# a time. Real drop tables were live-ops data and are in NO client file -- not in any of
# the pack's 55 forms, not on the stage row, not on mob_group -- which is exactly why the
# client asks the server for them (StageRpc GetDrops 8 -> 25). So this table only ever
# grows by observation.
#
# Both Battle.drops() (what a clear PAYS) and stage_drop_preview() (what the Drop Info
# button SHOWS) read it, so the panel cannot promise something the clear does not hand
# over. Stages with no entry fall back to one coin stack per wave.
class RuneDrop(NamedTuple):
    """A starshard drop.

    The results panel can only render an ITEM id -- BattleReward..ctor (0x1685F3C) builds
    RewardData{id, count} from elements [1] and [2] and ignores [0] -- but the piece
    itself is an equipment instance that has to land in backpack storage 2. So a rune
    drop carries both: `display_item` for the results icon, and the item/slot/level for
    the real grant.

    `item_id` is a STARSHARD item -- `_action` 111..116, which is both the slot
    (action - 110) and what routes it into the Starshards list. 201201 is
    "\u26051 Endearment II", the part-2 piece. `level` is the rune's +N.
    """
    display_item: int
    item_id: int
    slot: int
    level: int = 0
    enhance: int = 0
    count: int = 1


# ---- Starshard Temple ------------------------------------------------------
# **`_book == 2` marks a Starshard Temple stage** -- exactly the 41 of them under dmap
# root 40011 and nothing else in the pack, so it is the trigger rather than a stage list.
#
# These stages MUST drop at least one starshard or the client HANGS. Clearing one opens
# `PanelBattleRuneResult`, and that panel is driven entirely by the rune list:
#   * `OnPanelEnable` (0x16BAF7C) reads the list, and with a count of 0 it skips its fill
#     loop and parks at `UpdateResultState(0)`;
#   * `OnBattleEnd` (0x16BAB78) then does `if (EventArg.Count < 1) return;` -- so it never
#     reaches the `UpdateResultState(7)` that finishes the sequence.
# The result is the empty altar room with no UI on it and no way out but a restart.
# Dropping coins here (which is what the generic per-wave fallback did) is exactly that
# case. Observed live: a 2-wave temple clear shows TWO shards side by side, which is the
# panel's own `_rune2_Left_Info` / `_rune2_Right_Info` pair.
#
# Starshard item ids encode `ELEMENT*1000 + slot*100 + rank*10 + star`, where rank
# 0..4 = N/R/SR/UR/LR (and `_param2` grade = rank + 1); `_action` is 110 + slot.
STARSHARD_BOOK = 2
# The eight Temple sets, and the DAY ROTATION the in-game banner states: four sets on
# Mon/Wed/Fri, the other four on Tue/Thu/Sat/Sun. "Fortitude" is the current EN name for
# the set the pack still calls Endearment (201) -- confirmed both ways: the banner reads
# "Fortitude Set(2): HP+19%" and `equip_suit` row 1 "Endearment" is Set(2) HP 190.
STARSHARD_SETS_MWF = (201, 205, 206, 208)     # Fortitude, Nightshade, Mystery, Devotee
STARSHARD_SETS_TTSS = (207, 202, 203, 204)    # Defender, Chaos, Hawkeye, Slayer
STARSHARD_TEMPLE_SLOTS = tuple(range(1, 7))   # all six slots can drop
STARSHARD_DROPS_PER_CLEAR = 2                 # what the live results panel shows
STARSHARD_MAX_STAR = 6                        # ★1..★6 shard items all exist
STARSHARD_MAX_RANK = 4                        # 0..4 = N / R / SR / UR / LR
STARSHARD_SET_NAMES = {201: "Endearment", 202: "Chaos", 203: "Hawkeye", 204: "Slayer",
                       205: "Nightshade", 206: "Mystery", 207: "Defender",
                       208: "Devotee"}

# ---- the drop ladder, out of the stage rows themselves ----------------------
# **`_itemrank_str` is really `box_rank`** -- the constant it is read through is
# `_FIELD_ITEMRANK_STR = "box_rank"` (dump.cs:497328). An earlier version of this file
# dismissed it as "dead data" because `DesignStageRow` has no accessor for it, and
# invented a ladder instead: star sliding 1..5 across the 41 floors on a triangular
# window, with rarity deliberately NOT tied to depth.
#
# That reasoning had it backwards. The client never reads the field precisely BECAUSE
# the drop table was server-side -- which makes `box_rank` the surviving record of what
# the live server dropped, not junk. Reported from play 2026-08-18: our layout does not
# match the real one.
#
# It is "<max>,<min>", each a two-digit `star*10 + rank` code, and the two digits bound
# their axes independently. Across the whole pack the field is a generic [max, min]
# pair (book 0 "21,11", book 1 "10,4", books 21-24 "2,1"); only the Temple varies it
# per stage, and there it walks:
#
#     ST-1..5   31,11     ★1-3  rank R          ST-26..28  42,11    ★1-4  rank R-SR
#     ST-6..10  32,11     ★1-3  rank R-SR       ST-29..31  43,11    ★1-4  rank R-UR
#     ST-11..15 33,11     ★1-3  rank R-UR       ST-32..37  44,11    ★1-4  rank R-LR
#     ST-16..20 34,11     ★1-3  rank R-LR       ST-38..40  52..54   ★1-5  rank SR-LR
#     ST-21..25 41,11     ★1-4  rank R          ST-41      60,11    ★1-6  any rank
#
# So the star CEILING climbs ★3 -> ★6 and the rank ceiling re-walks R -> LR inside each
# star band, while the floor stays ★1 R for every floor. Two things our invented ladder
# got wrong and this fixes: ★6 was unreachable (STARSHARD_MAX_STAR was 5), and rarity
# was flat across all 41 floors when the data ties it to depth.
#
# ST-41's max rank digit is **0**, which is out of range for a ceiling. It is the one
# anomaly (a lv457, 1-AP stage), and is read as "no rank cap" -- the top floor offering
# every rank is the only reading that is not a downgrade from ST-40.
STARSHARD_RANK_NO_CAP = 0


def _box_rank_bounds(stage_id):
    """-> ((min_star, max_star), (min_rank, max_rank)) from the stage's `box_rank`."""
    row = dd.row("stage", int(stage_id)) or {}
    parts = [int(x) for x in str(row.get("_itemrank_str") or "").split(",") if x.strip()]
    if len(parts) < 2:
        return None
    hi, lo = parts[0], parts[1]
    lo_star, lo_rank = divmod(lo, 10)
    hi_star, hi_rank = divmod(hi, 10)
    if hi_rank == STARSHARD_RANK_NO_CAP:
        hi_rank = STARSHARD_MAX_RANK
    lo_star = max(1, min(lo_star, STARSHARD_MAX_STAR))
    hi_star = max(lo_star, min(hi_star, STARSHARD_MAX_STAR))
    lo_rank = max(0, min(lo_rank, STARSHARD_MAX_RANK))
    hi_rank = max(lo_rank, min(hi_rank, STARSHARD_MAX_RANK))
    return (lo_star, hi_star), (lo_rank, hi_rank)


# The Temple rotation flips on the SAME 4AM boundary as everything else in the server
# (daily missions, shop resets, dungeon passes -- player_state.core._daily_period).
# It used to use plain `date.today()`, i.e. midnight, so between 00:00 and 04:00 the
# Temple had already moved to the next day's sets while the rest of the game had not.
# player_state.core.DAILY_RESET_HOUR is bound to this so the two cannot drift.
DAILY_RESET_HOUR = 4


def starshard_sets_for_day(when=None):
    """The four sets the Temple offers today. Monday=0 .. Sunday=6.

    Confirmed against the in-game "Starshard Set" banner: MON/WED/FRI are Fortitude,
    Nightshade, Mystery and Devotee; TUE/THU/SAT/SUN are Defender, Chaos, Hawkeye and
    Slayer. ("Fortitude" is the current EN name for the set the pack still calls
    Endearment, 201 -- the banner's "Set(2): HP+19%" matches `equip_suit` row 1.)

    Verified working against a live log 2026-08-18: one clean switch across the whole
    file, MWF sets all Monday evening and TTSS from Tuesday on. A report of "only
    Chaos/Hawkeye/Slayer/Defender" is that half doing its job -- check the day, and
    check the hour, before touching this.

    `when` may be a date (used as the game-day directly) or left None to derive the
    current game day, 4AM-to-4AM.
    """
    import datetime as _dt
    if when is None:
        when = (_dt.datetime.now() - _dt.timedelta(hours=DAILY_RESET_HOUR)).date()
    return STARSHARD_SETS_MWF if when.weekday() in (0, 2, 4) else STARSHARD_SETS_TTSS


def starshard_temple_depth(stage_id):
    """-> 0.0..1.0 through the Temple (ST-1 .. ST-41), or None if not a Temple stage.

    Kept for callers that want a simple progress fraction; the DROP ladder no longer
    rides on it (see _box_rank_bounds).
    """
    row = dd.row("stage", int(stage_id)) or {}
    if row.get("_book") != STARSHARD_BOOK:
        return None
    order = int(row.get("_sort") or 1)
    return max(0.0, min((order - 1) / max(STARSHARD_TEMPLE_COUNT - 1, 1), 1.0))


def starshard_star_weights(stage_id):
    """-> [(weight, star), ...] for a Temple stage.

    The floor is flat ★1 across the whole Temple, so a uniform roll over the band would
    make ST-40 feel identical to ST-1 for a player who only notices the common case.
    Weight rises linearly toward the ceiling instead: the band's top star is the most
    likely outcome and ★1 stays possible but rare, which is what a rising ceiling is
    for.
    """
    row = dd.row("stage", int(stage_id)) or {}
    if row.get("_book") != STARSHARD_BOOK:
        return []
    bounds = _box_rank_bounds(stage_id)
    if not bounds:
        return []
    (lo, hi), _ranks = bounds
    return [(float(star - lo + 1), star) for star in range(lo, hi + 1)]


def starshard_rank_weights(stage_id):
    """-> [(weight, rank), ...]: the same shape for rarity, which the data DOES tie to
    depth (the old table was one flat set of odds on every floor)."""
    bounds = _box_rank_bounds(stage_id)
    if not bounds:
        return []
    _stars, (lo, hi) = bounds
    # Rarity is the scarce axis: weight FALLS toward the ceiling, so the ceiling rising
    # widens what is possible without making LR routine.
    return [(float(hi - rank + 1), rank) for rank in range(lo, hi + 1)]


def starshard_typical_star(stage_id):
    """The star a Temple stage MOSTLY pays -- what Drop Info previews."""
    weights = starshard_star_weights(stage_id)
    return max(weights)[1] if weights else None


def starshard_temple_tier(stage_id):
    """Truthy for a Temple stage (its typical star), None otherwise. Kept as the cheap
    "is this the temple?" test callers use."""
    return starshard_typical_star(stage_id)


STARSHARD_TEMPLE_COUNT = 41       # ST-1..ST-41; set below from the pack at import


def starshard_temple_pool(stage_id, when=None):
    """Every shard a Temple stage can drop today -> [(item_id, slot), ...], or [].

    The client does not know the answer either: it asks the server for drop previews
    (StageRpc GetDrops 8 -> 25). The band comes from the stage's own `box_rank` (see
    _box_rank_bounds), the set from today's rotation off the in-game banner, and the
    slot is any of the six.

    **The WHOLE band is listed, deliberately.** Capping it to the top few stars was
    tried and is a lie: the floor stays ★1 all the way down the Temple, so a deep floor
    really can pay a ★1 and a truncated preview under-reports it. On the deepest floors
    that is 6 stars x 4 sets = 24 icons, which is a lot -- but the alternative is Drop
    Info disagreeing with the payout, which is the exact drift the preview/payout check
    in test_battle_effects.py exists to catch (and did catch, here).

    Rank is not split out: every rank in the band can drop, so no variant is "the"
    answer and the base icon stands for the set.
    """
    stars = starshard_star_weights(stage_id)
    if not stars:
        return []
    pool = []
    for star in sorted({star for _w, star in stars}):
        for element in starshard_sets_for_day(when):
            icons = starshard_set_icon(element, star)
            if icons and (icons[0], 0) not in pool:
                pool.append((icons[0], 0))
    return pool


def _weighted(rng, pairs):
    """Pick a value from [(weight, value), ...]."""
    total = sum(w for w, _v in pairs)
    roll = rng.random() * total
    for weight, value in pairs:
        if roll < weight:
            return value
        roll -= weight
    return pairs[-1][1]


def starshard_temple_drops(stage_id, rng=None, when=None):
    """[RuneDrop, ...] a Temple clear actually pays: STARSHARD_DROPS_PER_CLEAR shards,
    each rolling its own set, slot, star and rank.

    Star AND rank both come from the stage's own `box_rank` band now; the rank used to
    be one flat table shared by all 41 floors, which is what made deep runs feel the
    same as shallow ones.
    """
    import random as _r
    rng = rng or _r
    stars = starshard_star_weights(stage_id)
    ranks = starshard_rank_weights(stage_id)
    if not stars or not ranks:
        return []
    sets = starshard_sets_for_day(when)
    # Distinct slots, so the two candidates are a real choice rather than near-duplicates
    # -- the live panel shows a slot I next to a slot II.
    slots = list(STARSHARD_TEMPLE_SLOTS)
    rng.shuffle(slots)
    out = []
    for slot in slots[:STARSHARD_DROPS_PER_CLEAR]:
        star = _weighted(rng, stars)
        rank = _weighted(rng, ranks)
        item_id = rng.choice(sets) * 1000 + slot * 100 + rank * 10 + star
        if not dd.row("item", item_id):
            continue                 # never hand out an id the client cannot draw
        out.append(RuneDrop(display_item=item_id, item_id=item_id, slot=slot))
    return out


_set_icon_cache = {}


def starshard_set_icon(element, star):
    """The display-only "Random ★N <set>" icon for a set at a star, or None.

    `_action 2` items that exist purely to say "one of this set" -- the same ones the
    storefront Drop Info uses. Five per (set, star), one per rank in `_param1` order.
    """
    key = (element, star)
    if key not in _set_icon_cache:
        name = (dd.row("item", element * 1000 + 100 + 4 + star * 0) or {})
        want = STARSHARD_SET_NAMES.get(element)
        found = []
        for iid, row in (dd.rows("item") or {}).items():
            if row.get("_action") != 2 or not want:
                continue
            n = (row.get("_itemName_en") or "").strip()
            if n.startswith("Random \u2605") and n.endswith(" " + want):
                star_txt = n[len("Random \u2605"):-(len(want) + 1)].strip()
                if star_txt == ("I" if star == 1 else str(star)):
                    found.append((int(row.get("_param1") or 0), int(iid)))
        _set_icon_cache[key] = [i for _p, i in sorted(found)]
    return _set_icon_cache[key]


# ---- Transcend Corridor (the Gremlin daily) --------------------------------
# **`_book == 23` marks the Transcender dungeon** -- 48 "Transcend Corridor" stages plus
# the 32 of its "[Double] Transcender Hunt" variant, and nothing else in the pack. Each
# daily dungeon carries its own exclusive book (21 Trainers Gym, 22 Evolution Abyss,
# 23 here, 24 Treasure Raiders), the same way the Starshard Temple owns book 2.
#
# It pays Pieces of Transcender Gremlin, which the Soul Altar's Summoning Orbs tab then
# exchanges for the Gremlins themselves -- item 111+n costs item 116+n. Nothing else in
# the game drops them, so without this the whole Gremlin line is unreachable and those
# five shop cards are dead.
#
# The corridor runs in five named difficulty bands and there are exactly five Piece
# tiers, so the mapping is one-to-one:
#     Trans-1..10   "Ground".."Ultimate"          -> ★1 Pieces
#     Trans-11..20  the same ten, EX              -> ★2
#     Trans-21..30  EX+                           -> ★3
#     Trans-31..40  Ultimate                      -> ★4
#     Trans-41..45  Ultimate+                     -> ★5
# Banded on `_sort` rather than the name, because the three SP stages (sorts 16/27/38)
# sit inside a band without carrying its suffix, and `_stagelv` cannot separate Ultimate
# from Ultimate+ (both run to 545).
TRANSCEND_BOOK = 23
GREMLIN_PIECE_ITEMS = (116, 117, 118, 119, 120)      # ★1..★5 Pieces
GREMLIN_PIECES_PER_CLEAR = 3
# dmap root of the "[Double]" variant, which is the same dungeon at double rewards.
TRANSCEND_DOUBLE_ROOT = 31014
# The main corridor's length. The Double variant only runs to sort 32, and its depth is
# measured against THIS so its stages line up with the corridor stages of the same sort
# -- scaling it to its own length would stretch 32 stages of EX+ content up to ★5.
TRANSCEND_STAGE_COUNT = 48
# Rolled like the Temple's star, so every stage beats the one before it instead of all
# ten inside a band tying. **Deliberately NARROWER than the Temple's window (2.0):** a
# Piece buys its Gremlin one-for-one, so a wide spread would let Trans-1 mint ★5
# Gremlins. At 1.5 only the neighbouring tier bleeds in.
GREMLIN_TIER_SPREAD = 1.5


def transcend_corridor_depth(stage_id):
    """-> 0.0..1.0 through the Transcender dungeon, or None if not one of its stages."""
    row = dd.row("stage", int(stage_id)) or {}
    if row.get("_book") != TRANSCEND_BOOK:
        return None
    sort = int(row.get("_sort") or 1)
    return max(0.0, min((sort - 1) / max(TRANSCEND_STAGE_COUNT - 1, 1), 1.0))


def gremlin_tier_weights(stage_id):
    """-> [(weight, tier index), ...]: the sliding window over the five Piece tiers."""
    depth = transcend_corridor_depth(stage_id)
    if depth is None:
        return []
    top = len(GREMLIN_PIECE_ITEMS)
    centre = 1.0 + depth * (top - 1)
    out = []
    for tier in range(1, top + 1):
        weight = 1.0 - abs(tier - centre) / GREMLIN_TIER_SPREAD
        if weight > 0:
            out.append((weight, tier))
    return out


def transcend_corridor_pool(stage_id):
    """Every Piece tier a stage can roll -> [item id, ...]; [] if not the dungeon."""
    return [GREMLIN_PIECE_ITEMS[t - 1]
            for _w, t in sorted(gremlin_tier_weights(stage_id), key=lambda x: x[1])]


def transcend_corridor_drops(stage_id, rng=None):
    """[(item id, count)] of Gremlin Pieces for a clear, or [] if not the dungeon."""
    import random as _r
    rng = rng or _r
    weights = gremlin_tier_weights(stage_id)
    if not weights:
        return []
    total = sum(w for w, _t in weights)
    roll = rng.random() * total
    tier = weights[-1][1]
    for weight, t in weights:
        if roll < weight:
            tier = t
            break
        roll -= weight
    row = dd.row("stage", int(stage_id)) or {}
    dmap = dd.row("dmap", row.get("_dmap_id")) or {}
    root = int(dmap.get("_link") or row.get("_dmap_id") or 0)
    count = GREMLIN_PIECES_PER_CLEAR * (2 if root == TRANSCEND_DOUBLE_ROOT else 1)
    return [(GREMLIN_PIECE_ITEMS[tier - 1], count)]


STAGE_DROPS = {
    # Trainers Gym (dmap 30004) -- the material dungeon for Level Training, so it pays
    # Trainers (items 101-105). 1400001 "Beginner Class" observed dropping ★2 Trainer x6.
    # NOTE the lowest rung does NOT pay the lowest tier: "Beginner Class" is stagelv 1
    # yet gives ★2, not ★1, so do not extrapolate the remaining rungs from the stagelv
    # ladder -- each one needs its own observation. (Main-story 2-1, a far easier stage,
    # pays ★1 -- and the ★1 and ★2 icons look near-identical, so read the tier off the
    # item name rather than the picture.)
    1400001: [(102, 6)],

    # Main story chapter 2. Both stages pay a ★1 Trainer alongside a stage-specific
    # material, which is the shape to expect elsewhere: a per-stage item plus a common
    # trainer, one drop icon per wave (2-1 and 2-2 are both 2-wave).
    2101: [(210, 10), (101, 1)],        # ★3 Minion Summon Orb x10 + ★1 Trainer
    # 2-2 drops a REAL Endearment starshard. Footage shows a specific set piece -- the
    # one for part 2 -- not the random box we used to hand out.
    # 2-2 drops a real Endearment starshard for part 2 (what the footage shows), not the
    # random box. WARNING: a malformed storage-2 entry makes PlayerBackpack's login sync
    # throw partway through; the sync never signals completion, so the client hangs
    # forever on `Subsystem 'PlayerBackpack' still in syncing...` AND, because the entry
    # is persisted, so does every later login until it is deleted by hand. `attr` must
    # always carry `lv`. See player_state.make_rune.
    2102: [RuneDrop(display_item=201201, item_id=201201, slot=2), (101, 1)],
}
# Corrects an earlier note here: items 1001-1005 are NOT "one per piece". They differ by
# RANK -- zh 普通/優良/稀有/史詩/傳說 = N/R/SR/UR/LR -- and every one of them is a
# random-SLOT box. Their English names are all identical because the translation dropped
# the rank word, which is what made them look like piece variants. `equipment` carries no
# slot column at all; the slot is a property of the instance. See the starshard memory.


def grow_rung(char_row, star, super_star=0):
    """Index into the RAW `_growStar` array, exactly as DesignCharRow.GetCharGrowRow
    computes it: `star - 1 + super_star`, falling back to rung 0 when that is past the
    end or lands on a 0 (the client logs the miss and does the same)."""
    raw = char_row.get("_growStar") or []
    idx = star - 1 + super_star
    if idx < 0 or idx >= len(raw) or not raw[idx]:
        return 0
    return idx


def _grow(char_row, star, lv, super_star=0):
    """Stats come from DesignCharGrowForm, indexed the way the client indexes it --
    the RAW ladder (mobs repeat the same grow id in every star slot)."""
    raw = char_row.get("_growStar") or []
    grow = dd.row("char_grow", raw[grow_rung(char_row, star, super_star)]) if raw else None
    if not grow:
        return {"hp": 100, "atk": 10, "def": 0, "spd": 500}
    steps = max(0, lv - 1)
    return {"hp": grow["base_hp"] + grow["add_hp"] * steps,
            "atk": grow["base_atk"] + grow["add_atk"] * steps,
            "def": grow["base_def"] + grow["add_def"] * steps,
            "spd": grow["spd"]}


def stage_drops_for(stage_id, waves=None, rng=None):
    """What ONE clear of `stage_id` pays -> [(item, count) | RuneDrop, ...].

    Module-level so the auto-play sweep pays exactly what beating the stage by hand
    pays. Battle.drops() is a thin wrapper over this; keeping two copies is how the
    Drop Info preview drifted out of step with the payout once already.
    """
    known = STAGE_DROPS.get(int(stage_id))
    if known is not None:
        return list(known)
    # **A Starshard Temple stage MUST drop starshards, or the client hangs.**
    # See starshard_temple_drops.
    temple = starshard_temple_drops(stage_id, rng)
    if temple:
        return temple
    gremlins = transcend_corridor_drops(stage_id, rng)
    if gremlins:
        return gremlins
    if waves is None:
        row = dd.row("stage", int(stage_id)) or {}
        waves = len(dd.csv_ints(row.get("_mobGroup_datas"))) or 1
    return [(COIN_ITEM_ID, COIN_PER_WAVE)] * waves


def stage_drop_preview(stage_id):
    """Item ids for the stage's "Drop Info" panel (StageRpc GetDrops 8 -> 25).

    There is NO drop table in the client data -- not in any of the pack's 55 forms, not
    on the stage row (`_itemrank_str` is a rank category: it is '21,11' on every
    main-story stage), not on mob_group. That is not an oversight: the client ASKS the
    server for this list, which it would not do if the table shipped locally. Real
    per-stage drop tables were live-ops data.

    So the contents are ours to choose, and the only honest choice is to preview exactly
    what a clear actually pays -- see Battle.drops(). **This and drops() must change
    together or the panel starts lying**, which is exactly what happened when the
    Starshard Temple learned to pay shards and this still advertised coins.

    A temple clear rolls its ELEMENT, so the preview lists the whole pool -- one entry
    per possibility, the same convention the storefronts use for a random box.
    """
    known = STAGE_DROPS.get(int(stage_id))
    if known is not None:
        return [d.display_item if isinstance(d, RuneDrop) else d[0] for d in known]
    pool = starshard_temple_pool(stage_id)
    if pool:
        return [i for i, _slot in pool]
    gremlins = transcend_corridor_pool(stage_id)
    if gremlins:
        return list(gremlins)
    row = dd.row("stage", int(stage_id)) or {}
    waves = len(dd.csv_ints(row.get("_mobGroup_datas"))) or 1
    return [COIN_ITEM_ID] * waves


def soulbook_bonus(book_rank):
    """The Soul Link rank reward: a flat ATK/DEF/HP bonus on every one of your casts.

    Straight out of DesignSoulbookRewardForm (`soulbook_reward`, 300 rows), which is
    perfectly linear -- rank N gives atk 5N / def 2N / hp 35N -- but read the table
    rather than the formula so a design update cannot silently drift from it.
    """
    row = dd.row("soulbook_reward", int(book_rank or 0))
    if not row:
        return {}
    return {"atk": row.get("_atk", 0), "def": row.get("_def", 0),
            "hp": row.get("_hp", 0)}


def _default_star(char_row):
    """Characters enter play at the star tier matching their rarity -- the star
    ladder is steep (1* Leviathan is 194 HP / 52 ATK, 5* is 795 / 215), so building
    a 5-rarity character at 1* makes the tutorial unwinnable and puts the party
    slower than its own mobs.

    Picked off the RAW ladder so the rung is never a 0 -- some rows (10032) are
    placeholders with holes in the array, and the client indexes it raw."""
    raw = (char_row.get("_growStar") or [])[:MAX_STAR]
    valid = [i + 1 for i, g in enumerate(raw) if g]
    if not valid:
        return 1
    rarity = char_row.get("_rarity") or 1
    allowed = [s for s in valid if s <= rarity]
    return max(allowed) if allowed else min(valid)


def skill_ratio(skill_id):
    """ATK multiplier for a skill, e.g. 1.8 for "180%攻擊力的傷害".

    DesignSkillForm carries no numeric damage column -- the coefficients only exist
    in the localized note text, so read them from there. Falls back to 1.0.
    """
    if skill_id in _ratio_cache:
        return _ratio_cache[skill_id]
    row = dd.row("skill", skill_id) or {}
    note = row.get("_note1") or ""
    m = re.search(r"(\d+)%", note)
    ratio = int(m.group(1)) / 100.0 if m else 1.0
    _ratio_cache[skill_id] = ratio
    return ratio


class Unit:
    """One combatant. `order` is the dictionary key the client uses everywhere --
    BattleUnitManager stores units by it and every later message (damage targets,
    turn order, HP sync) refers to units by this string."""

    def __init__(self, order, char_id, team, index, lv=1, star=None, super_star=0,
                 book_bonus=None, uid="", skill_limit=0, pact_iid=0, pact_lv=0,
                 gear_bonus=None):
        self.order, self.char_id, self.team, self.index = order, char_id, team, index
        # Worn bloodpact, resolved by player_state.roster (battle never sees the
        # backpack). Drives the aura -- see blood_effect.
        self.pact_iid, self.pact_lv = int(pact_iid or 0), int(pact_lv or 0)
        # The roster uid for player units (mobs have none). Only BattleCharData
        # (cmd 601) needs it; light() does not carry it.
        self.uid = uid
        # Battle-record totals, reported through BtCollector at wave end. Damage taken
        # and healing are tracked alongside damage dealt so the stats page has all
        # three columns; healing stays 0 until the engine grows heal skills.
        self.dmg_done = 0
        self.dmg_taken = 0
        self.healed = 0
        row = dd.row("char", char_id) or {}
        self.row = row
        # STR/AGI/TEC class, for "if the target is a STR Type cast" gates: _job 2/3/4
        # (CommonUtil.GetJobUseText renders job names via GetText(job + 12099)).
        self.job = row.get("_job") or 0
        self.lv = lv
        self.star = star or _default_star(row)
        # LightBattleChar carries only Star, so super_star never crosses the wire --
        # it only moves which _growStar rung the stats come from.
        self.super_star = super_star
        stats = _grow(row, self.star, lv, super_star)
        # Soul Link rank pays out as a flat stat bonus on every cast -- that IS the
        # "reward", there is no item drop. It has to be applied here because the
        # client never reads DesignSoulbookRewardRow at all (its ATK/DEF/HP getters
        # have no code xrefs, only Puerts wrapper data), so the bonus was
        # server-authoritative and only ever showed up in the stats the server sent.
        bonus = book_bonus or {}
        # Worn starshards + soulmirrors + set bonuses, already resolved to flat
        # numbers (percentages applied against these same base stats) by
        # player_state.roster._annotate_gear -- battle never sees the backpack.
        # **This used to be missing entirely**, so a fully geared cast fought at base
        # stats while the lobby sheet showed the ▲ deltas the CLIENT had computed.
        # Mobs pass nothing and are unaffected.
        gear = gear_bonus or {}
        self.gear_bonus = dict(gear)
        self.max_hp = stats["hp"] + bonus.get("hp", 0) + gear.get("hp", 0)
        self.hp = self.max_hp
        self.atk = stats["atk"] + bonus.get("atk", 0) + gear.get("atk", 0)
        self.defense = stats["def"] + bonus.get("def", 0) + gear.get("def", 0)
        self.spd = stats["spd"] + gear.get("spd", 0)
        # The blue bar under each character's HP: the 0..100 MOVE GAUGE. It fills at
        # the unit's own SPD (Battle._roll_turn_order) and empties when the unit takes
        # its turn, so it reads full exactly when the unit is acting. Float internally,
        # int on the wire.
        self.scv = 0.0
        # Own turns taken, which is what gates the special move at battle open. NEVER
        # spent -- see ultimate_charge() for why _charge is an opening delay and not a
        # recurring cost. This is what scv used to stand in for.
        self.charge = 0
        # Per-slot rank from the cast's total limit (skill_limit = limit_book +
        # limit_char, plus super_star), NOT a flat rank -- see skill_ranks.
        base_skills = [s for s in (row.get("_skills") or []) if s]
        ranks = skill_ranks(row, (skill_limit or 0) + (super_star or 0))
        self.skills = [skill_at_rank(s, ranks[i] if i < len(ranks) else 1)
                       for i, s in enumerate(base_skills)]
        # Remaining cooldown per skill slot. DesignSkillRow._cdTurn is the reload
        # time (0 for basics, 3-5 for the big ones); publishing 0 for everything
        # lets the player spam their strongest skill every turn.
        self.cooldowns = [0] * len(self.skills)
        # Active buffs/debuffs (battle_effects.Status). The effect engine appends here;
        # they tick down on this unit's own turns and modify its effective stats /
        # incoming damage. Empty for the simple-damage path.
        self.statuses = []

    def cd_turns(self, slot):
        if 0 <= slot < len(self.skills):
            return (dd.row("skill", self.skills[slot]) or {}).get("_cdTurn") or 0
        return 0

    def use_skill(self, slot):
        if 0 <= slot < len(self.cooldowns):
            self.cooldowns[slot] = self.cd_turns(slot)

    def tick_cooldowns(self):
        """End-of-own-turn housekeeping: reload skills and count the turn taken.
        The move gauge is NOT touched here -- it is spent by Battle.end_turn and
        refilled by the ATB in _roll_turn_order."""
        self.cooldowns = [max(0, c - 1) for c in self.cooldowns]
        self.charge += 1

    def ultimate_charge(self):
        """Own turns the special move must wait before its FIRST cast, from
        `DesignSkillRow._charge` (design column "charge").

        **An opening delay, not a recurring cost, and not a meter the player spends.**
        The column is real and deliberately authored -- it exists only on `_type` 3 SP
        rows, and across 831 skill groups it FALLS with skill level in 263 and rises in
        exactly 0, so it is a level-up reward, unlike `_target`/`_count`/`_action` which
        are byte-identical across a group's six levels. But it cannot recur per use:
        `_charge <= _cdTurn` in 2368 of 2380 rows, so a per-use charge would be masked
        by the longer reload essentially always and the per-level tuning would be
        invisible. The only moment it can bite is battle open. There is no UI for it
        either -- the one "Charge" string (text 113034) is in the top-up cluster -- so
        `charge` here just counts own turns and is never spent.
        """
        if ULTIMATE_SLOT >= len(self.skills):
            return DEFAULT_ULTIMATE_CHARGE
        row = dd.row("skill", self.skills[ULTIMATE_SLOT]) or {}
        return int(row.get("_charge") or DEFAULT_ULTIMATE_CHARGE)

    def ultimate_ready(self):
        return self.charge >= self.ultimate_charge()

    def fill_time(self):
        """How long this unit still needs to fill its move gauge, in SPD-units. Used
        only to compare units against each other, so the absolute scale is arbitrary."""
        return max(0.0, SCV_FULL - self.scv) / max(1, self.spd)

    def fill_gauge(self, seconds):
        self.scv = min(float(SCV_FULL), self.scv + seconds * max(1, self.spd))

    def passives(self):
        """The unit's PASSIVE skill ids that the effect engine fully understands.
        Only `complete` passives fire, so a half-parsed one is silently inert rather
        than firing a guessed effect (Lucifer's Fear Nothing, not yet complete, is one)."""
        return [s for s in self.skills
                if (dd.row("skill", s) or {}).get("_type") == SKILLTYPE_PASSIVE
                and fx.is_complete(s)]

    @property
    def alive(self):
        return self.hp > 0

    def light(self):
        """LightBattleChar -- what the client spawns and renders from. `id` must be
        a real DesignCharForm row: BattleUnitSpawner.SpawnOne feeds it straight into
        GetRow and then DesignRoleModelInfoForm to find the battle model prefab."""
        return {
            "tm": self.team, "idx": self.index, "id": self.char_id,
            "hp": self.hp, "shp": 0, "mhp": self.max_hp, "thp": self.max_hp,
            # SkillList is List<List<int>>: [skill_id, cooldown] per slot -- entry [1]
            # is the remaining cooldown, which HandleJudge overwrites for slots 1..3
            # from its intargs. A non-zero value here greys the skill button out.
            "skdic": [[s, cd] for s, cd in zip(self.skills, self.cooldowns)],
            "scv": int(self.scv), "spd": self.spd, "lv": self.lv, "atk": self.atk,
            "star": self.star, "plus": 0,
            # be1/be2 are LightBattleChar.BloodEffectSerise / BloodEffectRank: the
            # persistent bloodpact aura at the unit's feet. BattleUnit.BloodEffect
            # (0x1979D98, called from StartState.OnEnter and HandleWaveBegin) requires
            # BOTH to be >= 1, then clones the prefab named
            # `String.Format("fx_state_{0}_{1:D3}", serise, rank)` and parents its
            # FxBloodPact component to the doll. Publishing 0/0 is why no unit has ever
            # shown one. `art_fx_prefab_states_*.ab` carries series 1..15 x ranks 1..4,
            # and there are exactly 15 bloodpact families (item `_action` 131..133).
            **dict(zip(("be1", "be2"), self.blood_effect())),
        }

    def blood_effect(self):
        """(series, rank) for the bloodpact aura, or (0, 0) for none.

        SEVENSINS_BLOOD_EFFECT="series,rank" forces a value on every unit -- a probe for
        confirming the wire contract before the real derivation is settled, since which
        field feeds `rank` (pact rarity vs its level band) is still unproven.
        """
        if not _BE_OVERRIDE:
            # Only ONE aura can render -- BloodEffect clones a single prefab per unit --
            # so these are ordered, not combined. The CLASS aura wins: only 14 casts in
            # the game have job 1 or 5, while any cast can wear a pact, so the rarer
            # mark is the one worth showing.
            if self.job in CLASS_AURA_JOBS:
                # rank IS the job here, not a growth stage -- see CLASS_AURA_SERIES.
                return (CLASS_AURA_SERIES, self.job)
            if self.pact_iid:
                pact = bloodpact_aura(self.pact_iid, self.pact_lv)
                if pact:
                    return pact
            return (0, 0)
        series, rank, start = (_BE_OVERRIDE + (1,))[:3]
        if series == "spread":
            # Order keys are sequential decimal strings from "101", so this walks the
            # field 1,2,3... and wraps -- every unit on screen shows a different series.
            try:
                n = int(self.order) - 101
            except (TypeError, ValueError):
                n = self.index
            return ((start - 1 + n) % BE_SERIES_MAX + 1, rank)
        return (series, rank)

    def _attributes(self, current):
        """BattleAttributeData. Keys from the 2.2.4 JsonProperty thunks
        (tools/json_keys.py --class BattleAttributeData). Only the four stats we
        actually model vary; the rest are real fields the client will read, so send
        them as zeros rather than omitting them."""
        return {
            "hp": self.hp if current else self.max_hp,
            "atk": self.atk, "def": self.defense, "spd": self.spd,
            "scv": int(self.scv) if current else SCV_FULL,
            "cri": 0, "tgn": 0, "cdi": 0, "cdr": 0, "prc": 0,
            "ehit": 0, "eanti": 0, "ddi": 0, "ddr": 0,
        }

    def battle_char_data(self, owner_uid=""):
        """BattleCharData -- the in-battle unit-detail popup (cmd 601 -> 1601).

        A DIFFERENT class from light()'s LightBattleChar: `HandleCharCurInfoCmd`
        deserializes this one as BattleCharData and raises BattleEvent 7.
        `SuperLimit`/`StorageSuperLimit` are 2.2.7-only ints that default to 0, so they
        are omitted deliberately (same reasoning as the super-limit wire sweep).
        """
        return {
            "tm": self.team, "owner": owner_uid, "order": str(self.order),
            "idx": self.index, "uid": self.uid or "", "id": self.char_id,
            "lv": self.lv, "star": self.star,
            "limit_book": 0, "limit_char": 0, "limit_suit": 0,
            "super_star": 0,
            "max": self._attributes(current=False),
            "cur": self._attributes(current=True),
            "skdic": [[sk, cd] for sk, cd in zip(self.skills, self.cooldowns)],
            "eqdic": [], "st": [],
        }

    def sync(self):
        """BattleUnitManager.SyncData reads exactly [MaxHP, HP, Scv, SPD]."""
        return [self.max_hp, self.hp, int(self.scv), self.spd]

    def to_state(self):
        """Only the fields that ever change after __init__ (see Battle.to_state's
        docstring) -- atk/defense/spd/skills are re-derived identically on
        reconstruction, so freezing them here would be redundant, not safer."""
        return {
            "hp": self.hp, "max_hp": self.max_hp, "scv": self.scv,
            "charge": self.charge,
            "cooldowns": self.cooldowns,
            "dmg_done": self.dmg_done, "dmg_taken": self.dmg_taken,
            "healed": self.healed,
            "statuses": [_status_to_state(s) for s in self.statuses],
        }

    def restore_state(self, saved):
        self.hp = saved["hp"]
        self.max_hp = saved["max_hp"]
        self.scv = float(saved["scv"])
        self.charge = saved.get("charge", 0)
        self.cooldowns = list(saved["cooldowns"])
        self.dmg_done = saved.get("dmg_done", 0)
        self.dmg_taken = saved.get("dmg_taken", 0)
        self.healed = saved.get("healed", 0)
        self.statuses = [_status_from_state(s) for s in saved.get("statuses", [])]


def _status_to_state(st):
    """A catalog-backed status (the overwhelming majority) re-links to
    fx.catalog()[name] on restore rather than freezing its `definition` -- so a later
    catalog/status-data fix is picked up by an in-progress fight too. Only a
    SYNTHESIZED status (the `stat_mod` op's ad-hoc ATK/DEF/SPD buffs, built inline as
    `Status(tag, ..., synth)` with no catalog entry under that exact tag) carries its
    own definition, since there is nothing to re-link to."""
    d = {"name": st.name, "remaining": st.remaining, "stacks": st.stacks,
        "shield_hp": st.shield_hp, "dot_atk": st.dot_atk,
        "taunt_source": st.taunt_source}
    if st.name not in fx.catalog():
        d["definition"] = st.definition
    return d


def _status_from_state(d):
    definition = d.get("definition")
    if definition is None:
        definition = fx.catalog().get(d["name"], {})
    st = fx.Status(d["name"], d["remaining"], definition)
    st.stacks = d.get("stacks", 1)
    st.shield_hp = d.get("shield_hp", 0)
    st.dot_atk = d.get("dot_atk")
    st.taunt_source = d.get("taunt_source")
    return st


def _char_job(char_id):
    """DesignChar `_job`: 2 STR / 3 AGI / 4 TEC. -> 0 when the row is unknown."""
    return int((dd.row("char", int(char_id)) or {}).get("_job") or 0)


class Battle:
    """One run of one stage: a list of waves, each a mob group from the design data."""

    def __init__(self, stage_id, team_char_ids, team_level=1, team_star=None,
                 team_super_star=0, book_rank=0):
        # Stashed verbatim (not re-derived from the account at resume time) so
        # to_state()/restore_battle() reconstruct the EXACT battle a killed/restarted
        # server was running, even if the player's roster/soulbook changed since --
        # see restore_battle().
        self._ctor_args = (int(stage_id), team_char_ids, team_level, team_star,
                           team_super_star, book_rank)
        self.book_bonus = soulbook_bonus(book_rank)
        self.stage_id = int(stage_id)
        self.stage = dd.row("stage", self.stage_id) or {}
        self.wave_groups = dd.csv_ints(self.stage.get("_mobGroup_datas"))
        # Story cutscenes, one entry per wave. Sending zeros here is what makes the
        # tutorial feel "off": StartState plays pavg_id before the opening,
        # RushState.OnNextWave plays the wave-end NextAVGID during the run to the next
        # room, and the scripted tutorial is written around those beats happening.
        self.pre_avgs = dd.csv_ints(self.stage.get("_preAVG_datas"))
        self.before_avgs = dd.csv_ints(self.stage.get("_beforeBattleAVG_datas"))
        self.interlude_avgs = dd.csv_ints(self.stage.get("_interludeAVG_datas"))
        self.after_avgs = dd.csv_ints(self.stage.get("_afterBattleAVG_datas"))
        self.wave = 1
        self.wave_max = max(1, len(self.wave_groups))
        self.round = 1
        self.damage_sum = 0
        self.units = {}
        # status name -> times it landed on a PLAYER unit this run (rating kind 20)
        self.status_taken = {}
        # set by the reward path once battle XP has been applied (rating kind 5)
        self.levelled = False
        self.turn_order = []
        # Auto-battle, toggled by PlayerBattleServerCmd.Auto (501). Server-driven:
        # see auto_move().
        self.auto = False
        self.wave_begun = False        # WaveBegin (1100) is sent once per wave
        # order -> [index, char_id, dmg_done, dmg_taken, healed] for enemies that have
        # already been despawned by a wave change. Slots are reused across waves, so
        # totals accumulate per slot and the row count stays that of a single wave.
        self.enemy_totals = {}
        self.turn_open = False         # a turn is live; ignore repeat 101 requests
        self.team_level = team_level
        # Optional star-tier override for the party. None means "use each character's
        # own tier -- their roster star/super_star if the caller passed roster entries,
        # otherwise the tier matching their rarity" (see _default_star). A number
        # forces every slot to that tier; it is a STAR (1..6), so the super-star rungs
        # are reached with team_super_star, the same offset super_star is per character.
        self.team_star = team_star
        self.team_super_star = team_super_star
        self._add_player_team(team_char_ids)
        self._spawn_wave()
        # Passive battle-start effects fire before the turn order is rolled (a SPD buff
        # can reorder it) and with both teams already on the field.
        self._apply_battle_start(list(self.units.values()))
        self._roll_turn_order()
        self.all_mob_ids, self.all_skill_ids = self._collect_all_waves()

    # -- setup ------------------------------------------------------------
    def _add_player_team(self, char_ids):
        # Index is 0-based: BattleUnit.ResetLayerTransforms uses it to index
        # BattleFormationData's 5-entry position list, and 5 would throw.
        # An entry is either a bare char id or a roster dict ({id, lv, star,
        # super_star}), so a battle fought from the lobby uses the same rungs the
        # lobby showed rather than a flat default.
        for slot, cid in enumerate(char_ids[:MAX_SLOTS]):
            entry = cid if isinstance(cid, dict) else {}
            order = str(ORDER_BASE + slot)
            index = PLAYER_SLOTS[slot] if slot < len(PLAYER_SLOTS) else slot
            self.units[order] = Unit(
                order, entry.get("id", cid), TEAM_PLAYER, index,
                lv=entry.get("lv", self.team_level),
                star=self.team_star or entry.get("star"),
                super_star=self.team_super_star or entry.get("super_star") or 0,
                book_bonus=self.book_bonus, uid=entry.get("uid", ""),
                skill_limit=(entry.get("limit_book", 0) or 0)
                + (entry.get("limit_char", 0) or 0),
                pact_iid=entry.get("pact_iid", 0), pact_lv=entry.get("pact_lv", 0),
                gear_bonus=entry.get("gear_bonus"))
        # enemies keep numbering on from the party, and keep the same numbers across
        # waves so orders stay stable for the whole fight
        self.enemy_order_base = ORDER_BASE + len(char_ids[:MAX_SLOTS])

    def _spawn_wave(self):
        """Replace the enemy side with this wave's mob group. Mob ids in
        DesignMobGroupForm._mob_list are ordinary DesignCharForm rows."""
        for order in [o for o, u in self.units.items() if u.team == TEAM_ENEMY]:
            # Bank the outgoing wave's battle-record totals before the unit goes away,
            # or the Result panel only ever reports the LAST wave's enemies while the
            # player rows accumulate over the whole fight, and the two sides disagree.
            self._bank_enemy(self.units[order])
            del self.units[order]
        if not self.wave_groups:
            return
        group_id = self.wave_groups[min(self.wave, len(self.wave_groups)) - 1]
        group = dd.row("mob_group", group_id) or {}
        level = group.get("_mob_level", 1) or 1
        # keep the CSV slot number: it is the mob's field position, and blanks matter
        for slot, part in enumerate(str(group.get("_mob_list", "")).split(",")):
            part = part.strip()
            if not part or part == "0":
                continue
            order = str(self.enemy_order_base + slot)
            index = ENEMY_SLOTS[slot] if slot < len(ENEMY_SLOTS) else slot
            self.units[order] = Unit(order, int(part), TEAM_ENEMY, index, lv=level)

    def _collect_all_waves(self):
        """Every mob and skill the whole stage can field, for BattleDatas'
        all_mob_ids / all_skill_ids.

        These are the PRELOAD lists -- BattleResManager loads exactly these models
        and skill effects when the battle scene builds. Listing only the current
        wave means wave 2's boss has no model when it spawns: CloneBattleDoll fails
        ("ResAgent has no model(...)"), CreateNewEnemy NPEs, and HandleNextWave never
        reaches its event dispatch, so the fight silently stalls with no skill bar.
        """
        mob_ids, skill_ids = set(), set()
        for group_id in self.wave_groups:
            group = dd.row("mob_group", group_id) or {}
            for mob_id in dd.csv_ints(group.get("_mob_list")):
                mob_ids.add(mob_id)
                row = dd.row("char", mob_id) or {}
                skill_ids.update(s for s in (row.get("_skills") or []) if s)
        for unit in self.units.values():
            skill_ids.update(unit.skills)
        return sorted(mob_ids), sorted(skill_ids)

    def _roll_turn_order(self):
        """Run the move gauges forward until someone is full, then project the queue.

        Two halves, and the split is the point:
          * the REAL gauges advance only as far as the first unit needs to fill, so
            whoever is about to act is sitting at a full bar -- which is what the
            player sees under the portrait. Idempotent: once anyone is at SCV_FULL the
            step is zero, so calling this again (a death prune, a resume) is free.
          * the rest of the queue is simulated on a COPY, so merely showing the order
            never spends anyone's gauge.
        The projection is deduped down to one entry per unit: `line` has always been a
        permutation of the live orders and UpdateTimeLine keys its row per unit, so a
        fast unit that would lap the field stays a single entry.
        """
        alive = [u for u in self.units.values() if u.alive]
        if not alive:
            self.turn_order = []
            return
        # Ties (everyone opens at 0, so the whole field ties on the first roll) break
        # the way they always did: faster first, then the player team, then slot.
        def rank_key(u):
            return (-u.spd, u.team, u.index)
        step = min(u.fill_time() for u in alive)
        if step > 0:
            for u in alive:
                u.fill_gauge(step)
            # Same rounding guard as the projection below: the leader must actually
            # read full, or the client draws a 99% bar on the unit taking its turn.
            lead = min(alive, key=lambda u: (u.fill_time(), *rank_key(u)))
            lead.scv = float(SCV_FULL)
        rank = {u.order: rank_key(u) for u in alive}
        sim = {u.order: float(u.scv) for u in alive}
        queue = []
        # Bounded: each pass either appends a new order or laps someone already in the
        # queue, and a lap costs a full gauge, so len(alive) passes per entry is ample.
        for _ in range(len(alive) * len(alive) + len(alive)):
            if len(queue) == len(alive):
                break
            ready = [u for u in alive if sim[u.order] >= FULL_EPS]
            if not ready:
                gap = min((SCV_FULL - sim[u.order]) / max(1, u.spd) for u in alive)
                for u in alive:
                    sim[u.order] = min(float(SCV_FULL),
                                       sim[u.order] + gap * max(1, u.spd))
                # Whoever needed the least time IS full now; float rounding must not
                # be allowed to leave the step with nobody ready and the loop stuck.
                ready = [u for u in alive if sim[u.order] >= FULL_EPS]
                if not ready:
                    ready = [min(alive, key=lambda u: (SCV_FULL - sim[u.order])
                                 / max(1, u.spd))]
            nxt = min(ready, key=lambda u: rank[u.order])
            sim[nxt.order] = 0.0
            if nxt.order not in queue:
                queue.append(nxt.order)
        self.turn_order = queue

    def _defend_reduce(self):
        """A damage-reduction function the effect engine calls per target: the client's
        own defend-ratio curve applied to the target's post-status DEF."""
        return lambda u: defend_ratio(
            u.defense * fx.stat_multiplier(u.statuses, "DEF")
            + fx.flat_bonus(u.statuses, "DEF"))

    def _forced_target(self, attacker):
        """Taunt/Charm/Confuse override the ATTACKER's own target choice -- enforced
        here so it applies whether the target was auto-picked (enemy AI / auto-battle)
        or tapped by the player; a client can't route around its own status by picking
        someone else in the request. -> the forced unit, or None to leave the caller's
        target alone.

        confused_targeting (Charm/Confuse) turns the attack on the attacker's OWN side
        (checked first: a unit can be both taunted-by-an-enemy and charmed at once, and
        losing control of your target trumps being drawn to a specific one). forced_target
        (Taunt) redirects to whoever inflicted it, if that unit is still alive."""
        if fx.has_flag(attacker, "confused_targeting"):
            own = [u for u in self.units.values()
                  if u.team == attacker.team and u is not attacker and u.alive]
            if own:
                return own[0]
            return None
        for st in attacker.statuses:
            if st.taunt_source and "forced_target" in st.definition.get("flags", []):
                src = self.units.get(st.taunt_source)
                if src and src.alive:
                    return src
        return None

    def _apply_gauge_cd(self, outcome):
        """Fold an effect outcome's charge-gauge and cooldown changes back onto the
        units. scv is the 0..100 ultimate gauge; cooldowns are per-skill-slot turns."""
        for g in outcome["gauge"]:
            u = g["unit"]
            u.scv = max(0, min(SCV_FULL, u.scv + int(SCV_FULL * g["pct"] / 100.0)))
        for c in outcome["cd"]:
            u = c["unit"]
            u.cooldowns = [max(0, cd + c["delta"]) for cd in u.cooldowns]

    def _status_wire(self, events):
        """Turn engine status applications into DamageInfo.status entries
        [unit_order, skillID, round]. skillID is the _type-6 STATUS skill whose
        _statusID names the icon (fx.status_skill_id); a status we have no graphic for
        is dropped -- no icon beats a wrong one. `round` is the remaining turn count the
        client displays and self-decrements; a permanent ("battle") status shows as 99.
        Because two statuses map to two different skillIDs, several distinct icons can
        sit on the same unit at once."""
        wire = []
        for ev in events:
            sid = fx.status_skill_id(ev["name"])
            if sid is None:
                continue
            r = ev["round"]
            rnd = 99 if r == "battle" else int(r)
            if rnd > 0:
                wire.append([int(ev["unit"].order), sid, rnd])
        return wire

    def _apply_battle_start(self, units):
        """Fire the battle-start effects of each given unit's passive skills against the
        current field. Called when units ENTER the fight -- the whole roster at battle
        open, and each new wave's enemies as they spawn -- so team buffs, enemy debuffs
        and self-immunities (e.g. Leviathan's Jealousy Vortex) are in place before the
        first turn. Runs before the turn order is rolled so a SPD buff can reorder it."""
        field = list(self.units.values())
        for u in units:
            allies = [x for x in field if x.team == u.team]
            enemies = [x for x in field if x.team != u.team]
            for sid in u.passives():
                fx.run_phase(sid, "battle_start", u, None, allies, enemies,
                             env={"turn": self.round})

    # -- payloads ---------------------------------------------------------
    def battle_datas_json(self):
        """BattleDatas for cmd 1505. HandleServerStart deserializes this one string
        and then walks it without null checks -- `collector` in particular must be
        present or it returns early and the loading overlay never closes."""
        return json.dumps({
            "stage_id": self.stage_id,
            "type": BATTLE_TYPE_STAGE,
            "l_units": {o: u.light() for o, u in self.units.items()},
            "mob_group_ids": self.wave_groups,
            # every wave's mobs/skills, not just the current one -- these drive
            # resource preloading for the whole fight
            "all_mob_ids": self.all_mob_ids,
            "all_skill_ids": self.all_skill_ids,
            "wave": self.wave,
            "wave_max": self.wave_max,
            "collector": {"dmgSum": self.damage_sum, "cnt": self.round},
            "time_line": list(self.turn_order),
            "player_lists": {},
            # ONE ENTRY PER WAVE, and it must match the number of clear-reward drops.
            # PaserInterludeData walks IPlays and appends one InterludeList entry per
            # element, grading each against the stage's `box_rank` (ItemsRank, "21,11"
            # for 1-1): `v > rank[0]` -> 0, `v > rank[1]` -> 1, else 2, and **0 becomes
            # 3**, which UpdateRegularRewardData reads as "hide this reward slot".
            # That panel then indexes `InterludeList[i]` for every reward item with NO
            # bounds guard, so an empty list plus a non-empty item_list throws
            # ArgumentOutOfRange out of coInitResultData -- the results screen loads its
            # background, never populates, and cannot be exited. Empty was only ever
            # safe because item_list was empty too.
            "i_plays": [INTERLUDE_BOX_VALUE] * self.wave_max,
            # StartState.OnEnter plays pavg_id and only then runs the battle opening
            "pavg_id": self.avg(self.pre_avgs),
            "b1_avg_id": self.avg(self.before_avgs),
            "status": {},
            "backup_order": "",
            # sk_overwrite / mod_overwrite are OVERRIDES and must stay null (absent).
            # DesignRoleModelInfoRow.get_BattleModel checks only for non-null, so an
            # empty string still wins and every model resolves to "art/character/_01",
            # which 404s the bundle and kills the battle scene load.
            "custom_value": {},
            "team_skill": [],
        }, separators=(",", ":"))

    def battle_cmd_json(self, cur_team=TEAM_PLAYER):
        """BattleCmd, the per-turn payload for 1100/1101/1200/1201.
        `sync` feeds BattleUnitManager.SyncData ([MaxHP, HP, Scv, SPD] per order)
        and `line` becomes BattleDatas.ActionOrderList via UpdateTimeLine, which is
        what GetFirst reads to decide whose turn it is."""
        return json.dumps({
            "tm": cur_team,
            "combo": [],
            "line": list(self.turn_order),
            "sync": {o: u.sync() for o, u in self.units.items()},
            # HandleStartTurn reads coll_f[0] into CollectorData.TotalRound.
            "coll_f": [self.round, self.damage_sum],
            "tskill": [],
        }, separators=(",", ":"))

    def spend_skill(self, attacker_order, slot):
        """Put the used skill on cooldown. Nothing else is spent -- the special move's
        `_charge` is an opening delay, not a per-use cost (see Unit.ultimate_charge)."""
        unit = self.units.get(attacker_order)
        if not unit:
            return
        unit.use_skill(slot)

    def attack_cmd_json(self, attacker_order, defender_order, skill_id):
        """BattleCmd for cmd 1201, carrying the actual attack in `combo`.

        AttackJsonData is built through .ctor(string caster, int skill), so those two
        ctor parameter names are its JSON keys; `data` is List<List<DamageInfo>> --
        outer list per hit, inner per target.
        """
        attacker = self.units.get(attacker_order)
        target = self.units.get(defender_order)
        if attacker and target:
            target = self._forced_target(attacker) or target

        # DamageInfo shape (from HandleAttack): a hit is mode 1 with a NEGATIVE amount --
        # IsDamage is `Mode == 1 && Damage < 0`, HasHP is `Mode in (1,2) && Damage != 0`.
        def dmg_info(u, amount):
            return {"c": u.order, "md": 1, "cg": 0, "dmg": -amount, "cri": 0,
                    "die": 1 if not u.alive else 0,
                    # status is filled below from the engine's applied statuses; extra/
                    # picons stay empty (picons = passive-icon list, not yet used).
                    "status": [], "extra": [], "picons": [], "pskill_id": 0}

        # `data` is List<List<DamageInfo>>: ONE INNER LIST PER SWING, not one list of
        # everything. The skill's cinematic fires a BscTagKind-5 tag per hit and
        # AttackBehavior.BscTag (0x1BE3924) pops `DmgInfo[0]` for each one, so a 3-hit
        # skill shipped as a single group animates once and drops the other two swings.
        # DesignSkillRow._count is the swing count (see fx.hit_count).
        swings = fx.hit_count(skill_id) if skill_id else 1
        groups = [[] for _ in range(swings)]

        def add(seq, u, amount):
            """Fold `amount` into this swing's row for `u`, creating it if needed.

            **A unit may appear at most ONCE per group.** The client reads each group
            into a dictionary keyed by `c`, so a second row for the same order throws
            `An item with the same key has already been added. Key: 101` -- caught
            by the generic event handler, which means no stack, no skill animation,
            and a fight that simply stops: the enemy stands there and never yields the
            turn, so the client never asks for the next one. Nothing on the server
            says anything is wrong; it had already applied the damage and moved on.

            Duplicates arise whenever a skill has more than one DAMAGE effect: the
            effect engine emits one strike per (effect x target x swing), so a 2-effect
            AoE over 2 targets across 2 swings is 8 strikes and every group names both
            targets twice. Guild Weekly's Gabriel (skill 100001101) is the first one
            the party ever meets, which is why ordinary stages never showed this.

            Summing is not a workaround for the client's benefit -- one number per
            target per swing is all the wire shape can express, and it is what the
            damage popup shows either way. The total is unchanged."""
            g = groups[min(max(seq, 0), swings - 1)]
            for row in g:
                if row["c"] == u.order:
                    row["dmg"] -= amount            # dmg rides negative
                    row["die"] = 1 if not u.alive else 0
                    return
            g.append(dmg_info(u, amount))

        status_events = []
        if attacker and target and fx.is_complete(skill_id):
            # Trusted skill -> full effect engine: correct per-hit coefficients, real
            # targeting (a debuff can land on a different unit than the damage), and
            # server-side buff/debuff tracking that feeds back into damage.
            allies = [u for u in self.units.values() if u.team == attacker.team]
            enemies = [u for u in self.units.values() if u.team != attacker.team]
            reduce = self._defend_reduce()
            # env feeds the condition gates: the turn counter for odd/even and turn-cap
            # gates. No crit model exists yet, so crit-gated branches stay dormant.
            env = {"turn": self.round}
            outcome = fx.execute_skill(attacker, target, allies, enemies, skill_id,
                                       damage_reduce=reduce, env=env)
            status_events += outcome["status_events"]
            # Totals off the per-target FOLD (one addition per target), the wire rows
            # off the per-swing list -- same damage, different shape.
            for h in outcome["hits"]:
                if h["damage"] > 0:
                    self.damage_sum += h["damage"]
                    attacker.dmg_done += h["damage"]
                    h["target"].dmg_taken += h["damage"]
            for st in outcome["strikes"]:
                if st["damage"] > 0:
                    add(st["seq"], st["target"], st["damage"])
            # on_use / after_action gauge & cooldown changes (e.g. drain the target's
            # gauge, delay its skills, refresh the caster's own cooldowns).
            self._apply_gauge_cd(outcome)
            # Passive counters: any struck-and-still-alive enemy with an on_counter
            # passive hits the attacker back in the same combo.
            for h in outcome["hits"]:
                tgt = h["target"]
                if h["damage"] <= 0 or not tgt.alive:
                    continue
                for sid in tgt.passives():
                    c_out = fx.run_phase(sid, "on_counter", tgt, attacker,
                                         [u for u in self.units.values()
                                          if u.team == tgt.team],
                                         [u for u in self.units.values()
                                          if u.team != tgt.team],
                                         damage_reduce=reduce, env=env)
                    status_events += c_out["status_events"]
                    for ch in c_out["hits"]:
                        if ch["damage"] > 0:
                            # The counter lands after the combo, so it rides the last
                            # swing rather than opening the sequence.
                            add(swings - 1, ch["target"], ch["damage"])
                            self.damage_sum += ch["damage"]
                            tgt.dmg_done += ch["damage"]
                            ch["target"].dmg_taken += ch["damage"]
                    self._apply_gauge_cd(c_out)
        elif attacker and target:
            # Fallback: the simple-damage path, for a skill whose parse is incomplete.
            # **It still has to respect AoE.** This branch used to hit exactly one unit
            # no matter what the skill said, so every AoE whose parse fell short landed
            # on a single enemy -- 226 skills whose record had already identified the
            # AoE, against 114 that reached the effect engine and worked.
            live = [u for u in self.units.values()
                    if u.team != attacker.team and u.alive]
            # The DESIGN ROW is the authority on how many units a skill hits -- it is
            # what the panel's "Range 2 enemies" label is drawn from. Prose is the
            # fallback for the rows it does not describe, since a description often
            # says "the target" for a skill the panel calls multi-target.
            victims = fx.design_enemy_targets(skill_id, target, live)
            if victims is None:
                victims = live if fx.aoe_damage(skill_id) else [target]
            victims = victims or [target]
            for victim in victims:
                # Rolled per target: `damage` reads the victim's own DEF, so a shared
                # number would over-hit the tanky and under-hit the frail.
                # The prose coefficient is PER SWING ("Deals 120% DEF as damage 3
                # times"), which is how the effect engine reads it too, so a multi-hit
                # skill lands its roll once per swing instead of once in total.
                for seq in range(swings):
                    damage = self.damage(attacker, victim, skill_id)
                    victim.hp = max(0, victim.hp - damage)
                    self.damage_sum += damage
                    attacker.dmg_done += damage
                    victim.dmg_taken += damage
                    add(seq, victim, damage)
        # An empty group would eat one of the cinematic's hit tags and show nothing,
        # so only the groups that actually carry rows go on the wire.
        groups = [g for g in groups if g]
        if not groups and target:
            groups = [[dmg_info(target, 0)]]    # never send an empty combo
        # `die` belongs on the LAST row that names a unit: dmg_info reads the unit's
        # FINAL state, so a unit killed on swing 1 would otherwise be told to play its
        # death animation on every remaining swing.
        died_seen = set()
        for g in reversed(groups):
            for r in reversed(g):
                if not r["die"]:
                    continue
                if r["c"] in died_seen:
                    r["die"] = 0
                else:
                    died_seen.add(r["c"])
        # Attach status icons to the lead DamageInfo. Each entry is [order, skillID,
        # round]: the client resolves the graphic from a _type-6 STATUS skill's
        # _statusID (see _status_wire) and counts `round` down itself, so one push per
        # application is enough. Every entry names its own unit order, so hanging them
        # all off the first row reaches every affected unit.
        if groups:
            groups[0][0]["status"] = self._status_wire(status_events)
        self._tally_statuses(status_events)

        cmd = json.loads(self.battle_cmd_json(
            cur_team=attacker.team if attacker else TEAM_PLAYER))
        cmd["combo"] = [{
            "caster": attacker_order, "skill": skill_id, "pskill_id": 0,
            "data": groups,
        }]
        # sync/line have to reflect the post-damage state, so rebuild them after
        # applying the hit rather than reusing the pre-attack snapshot.
        cmd["sync"] = {o: u.sync() for o, u in self.units.items()}
        return json.dumps(cmd, separators=(",", ":"))

    def _bank_enemy(self, unit):
        """Fold a despawning enemy's battle-record totals into the carry store."""
        row = self.enemy_totals.get(unit.order)
        if row is None:
            self.enemy_totals[unit.order] = [unit.index, unit.char_id,
                                             unit.dmg_done, unit.dmg_taken,
                                             unit.healed]
            return
        row[1] = unit.char_id
        row[2] += unit.dmg_done
        row[3] += unit.dmg_taken
        row[4] += unit.healed

    def collector_json(self):
        """BtCollector for cmd 1506 (wave end) -- the data behind the Result button.

        `PlayerBattle.HandleWaveEnd` (0x168A2FC) deserializes strargs[0] straight into
        `PlayerBattle.CollectorData`, and `PanelBattleRecord` renders it. Newtonsoft
        builds the object through
        `.ctor(List<List<Dictionary<string, List<int>>>> dmgTbl)`, so `dmgTbl` is the
        wire key (a ctor parameter, not a field) and `type` is the only real
        JsonProperty.

        Shape, read off the ctor (0x1686170) and `InitTeamStatisticDataList`:
            dmgTbl[battle][team] = {uid: [order, charId, damage, damaged, heal]}
        `AllDamageList[b][t]` ends up as that dict's VALUES, sorted ascending on
        row[0] -- so row[0] is display order. The stats page reads team **0**, and
        indexes rows [1]..[4] directly, so every row needs at least five ints.
        `TeamOneDamageSumDic` sums team 0 across battles and MVP is the charId with the
        highest row[2].

        We report ONE battle entry: a campaign fight is a single battle whose waves
        accumulate, unlike arena's three rounds (which is what the panel's next/prev
        page buttons are for).
        """
        def rows(team_id):
            out = {}
            if team_id == TEAM_ENEMY:
                out = {o: list(r) for o, r in self.enemy_totals.items()}
            for unit in self.units.values():
                if unit.team != team_id:
                    continue
                # Mobs carry no roster uid; the dict key is only an identity, and the
                # unit `order` is the client's canonical key everywhere else.
                key = unit.uid or unit.order
                row = out.get(key)
                if row is None:
                    out[key] = [unit.index, unit.char_id, unit.dmg_done,
                                unit.dmg_taken, unit.healed]
                else:
                    row[1] = unit.char_id
                    row[2] += unit.dmg_done
                    row[3] += unit.dmg_taken
                    row[4] += unit.healed
            return out

        # BOTH teams are required. `InitEnemyStatisticDataList` (0x16B15C8) bounds
        # checks `AllDamageList[battle].Count > 1` before taking [1], so a
        # player-only table gets past the team page and then throws on the enemy one.
        return json.dumps(
            {"type": BATTLE_TYPE_STAGE,
             "dmgTbl": [[rows(TEAM_PLAYER), rows(TEAM_ENEMY)]]},
            separators=(",", ":"))

    @staticmethod
    def damage(attacker, target, skill_id):
        """atk * the skill's coefficient, reduced by the target's defence."""
        raw = attacker.atk * skill_ratio(skill_id)
        return max(1, int(raw * (1.0 - defend_ratio(target.defense))))

    # -- flow -------------------------------------------------------------
    def acting_unit(self):
        return self.units.get(self.turn_order[0]) if self.turn_order else None

    def action_order(self):
        """BattleData.ActionOrderList / CharListByScv -- the turn queue as order keys."""
        return list(self.turn_order)

    def swap_units(self, incoming, outgoing):
        """Apply a ChangeChar swap (cmd 504).

        NOTE: we never put a unit on the bench -- BattleDatas ships the whole party on
        the field -- so the client does not offer the swap button and this is currently
        unreachable. It is implemented so the command is answered correctly if a future
        change adds a backup slot. The client swaps the pair's screen slot and sets
        BackupCharOrder; mirroring the slot swap and the turn-queue position is the
        server-side equivalent.
        """
        a, b = self.units.get(incoming), self.units.get(outgoing)
        if not a or not b or a is b:
            return False
        a.index, b.index = b.index, a.index
        order = self.turn_order
        if incoming in order and outgoing in order:
            i, j = order.index(incoming), order.index(outgoing)
            order[i], order[j] = order[j], order[i]
        return True

    def player_turn(self):
        u = self.acting_unit()
        return bool(u and u.team == TEAM_PLAYER)

    def judge_args(self):
        """intargs for Judge (1200) describing the acting unit's skill buttons.

        HandleJudge maps them as: [0] action timeout, [1]/[2] -> nowSkillEnable for
        buttons 2 and 3 (enabled when 0), [3]/[4]/[5] -> SkillList[1..3][1], the
        cooldown numbers drawn on those buttons.
        """
        unit = self.acting_unit()
        cds = list(unit.cooldowns) if unit else []
        cd = [cds[i] if i < len(cds) else 0 for i in (1, 2, 3)]
        sealed = bool(unit and fx.has_flag(unit, "ability_seal"))
        # Button 3 is the ultimate, and it is gated by CHARGE as well as by cooldown.
        # The button has exactly one number -- UISkillBtn.LeftCD, drawn from
        # SkillList[i][1] and only when it is > 0 (PanelBattle._setBtnState 0x176B6BC)
        # -- so publish "turns until this lights up": the larger of the reload and the
        # charge still to bank. Without this a charge-gated ultimate is a dark button
        # with no number and no explanation, because the blue bar is the move gauge now
        # and no longer doubles as the charge readout.
        charge_left = max(0, unit.ultimate_charge() - unit.charge) if unit else 0
        cd[1] = max(cd[1], charge_left)
        ult_locked = sealed or bool(cd[1])
        return [0, 1 if (sealed or cd[0]) else 0, 1 if ult_locked else 0,
                cd[0], cd[1], cd[2]]

    def end_turn(self):
        """Rotate the acting unit to the back, drop anyone who died, then run the NEW
        acting unit's start-of-turn housekeeping (DoT/HoT ticks, and an immobilized unit
        auto-skips instead of waiting on an attack that will never come)."""
        acted = self.acting_unit()
        if acted:
            acted.tick_cooldowns()
            # Taking the turn is what SPENDS the move gauge -- the bar empties here
            # and refills over the following turns at the unit's own SPD.
            acted.scv = 0.0
            # Count down this unit's statuses on its own turn; drop the expired.
            if acted.statuses:
                acted.statuses = [s for s in acted.statuses if not s.tick()]
        self._roll_turn_order()
        self.round += 1
        self.turn_open = False
        self._start_of_turn()

    def _start_of_turn(self, _depth=0):
        """DoT/HoT ticks for whoever is now at the front of the queue, then skip its
        turn outright if it's immobilized (Stun/Freeze/Daze/...) -- recurses (bounded by
        unit count) so a fully-crowd-controlled lineup still resolves instead of
        hanging. A tick that kills the unit re-prunes the order before recursing."""
        unit = self.acting_unit()
        if not unit or _depth > len(self.units):
            return
        fx.tick_dot_hot(unit)
        if not unit.alive:
            self._roll_turn_order()
            self._start_of_turn(_depth + 1)
            return
        if fx.is_immobilized(unit.statuses):
            unit.tick_cooldowns()
            if unit.statuses:
                unit.statuses = [s for s in unit.statuses if not s.tick()]
            # A skipped turn still costs the gauge, or the queue never moves on.
            unit.scv = 0.0
            self._roll_turn_order()
            self.round += 1
            self._start_of_turn(_depth + 1)

    def team_alive(self, team):
        return any(u.alive for u in self.units.values() if u.team == team)

    def avg(self, avg_list, wave=None):
        """The AVG id for a wave (1-based) out of one of the stage's CSV lists.
        Missing entries mean "no cutscene", which is a legitimate 0."""
        idx = (self.wave if wave is None else wave) - 1
        return avg_list[idx] if 0 <= idx < len(avg_list) else 0

    # -- clear rewards ----------------------------------------------------
    def _tally_statuses(self, events):
        """Count statuses that landed on OUR casts, for rating kind 20.

        `_apply_status` reports every application through `outcome["status_events"]`
        with the unit it hit, so this is the one funnel every source passes through --
        skills, passives and DoT re-applications alike.
        """
        for ev in events or ():
            unit = ev.get("unit")
            if unit is not None and unit.team == TEAM_PLAYER:
                name = ev.get("name")
                if name:
                    self.status_taken[name] = self.status_taken.get(name, 0) + 1

    def rating_rows(self):
        """The stage's rating conditions, in `_rating_datas1..4` order.

        `DesignStageRow.PaserRatingData` splits each on ',' into ints, so a row is
        **[type, item_id, count, threshold]**: `[0]` selects the label text
        (DesignTextForm 11121/11122/11123/... ) and those labels are formatted with
        `[3]`, which is what pins `[1]`/`[2]` down as the reward pair. Stage 1-1's
        "1,1,10" is the 10 gems (item 1 = ダイヤ) the real client shows for Stage
        Clear, and "3,1,5,12" the 5 for clearing within 12 turns.
        """
        rows = []
        for i in range(1, RATING_SLOTS + 1):
            row = dd.csv_ints(self.stage.get(f"_rating_datas{i}"))
            if row:
                rows.append(row)
        return rows

    @property
    def deaths(self):
        """Allies defeated this run. Player units persist across waves and nothing
        revives them, so the dead ones at the end are the whole count."""
        return sum(1 for u in self.units.values()
                   if u.team == TEAM_PLAYER and not u.alive)

    def rating_flags(self):
        """1/0 per rating condition, index-aligned with rating_rows(), which is what
        BattleReward.ratingList carries. Conditions we cannot judge (the kizuna-lead
        and other-mode types 4..22) report 0 rather than a false positive.

        **Always RATING_SLOTS long, even when the stage defines no conditions.**
        `PanelBattleResult.CheckAppsFlyer` opens with

            list = PlayerBattle.GetRewardRatingList()
            if (list.Count <= 3) ThrowArgumentOutOfRangeException()
            if (list[3] == 1) ...

        -- an unconditional read of index 3 before any of its stage-id branches, and it
        runs from `OnClickResultEnd`. A short list therefore throws on the Tap to End
        button itself: the result panel renders fine, the score is banked, and then the
        button does nothing, every tap, forever. Guild Weekly is where this surfaced
        (book 8 leaves all four `_rating_datas` empty, so we sent []), but it would
        strike any stage defining fewer than four conditions.

        Padding is honest here: a slot with no condition was not met, and both
        consumers are indifferent -- rating_rewards zips against rating_rows and
        truncates, rating_mask contributes no bit for a zero."""
        cleared = self.wave_cleared()
        flags = []
        for row in self.rating_rows():
            kind = row[0]
            limit = row[3] if len(row) > 3 else 0
            if kind == RATING_CLEARED:
                ok = cleared
            elif kind == RATING_MAX_DEATHS:
                ok = cleared and self.deaths <= limit
            elif kind == RATING_MAX_TURNS:
                ok = cleared and self.round <= limit
            elif kind == RATING_LEVEL_UP:
                ok = cleared and self.levelled
            elif kind == RATING_STATUS_MAX:
                # [3] is the skill id, [4] the allowance. The catalog is keyed by the
                # skill's own `_name_en`, which is what the tally counts.
                name = (dd.row("skill", row[3]) or {}).get("_name_en") if len(row) > 3 else None
                allowed = row[4] if len(row) > 4 else 0
                ok = cleared and (not name
                                  or self.status_taken.get(name, 0) <= allowed)
            elif kind in RATING_JOB_MIN:
                # Composition, not performance: judged over the party as fielded, dead
                # members included -- the condition is what you brought, not what
                # survived.
                want = kind - RATING_JOB_BASE
                have = sum(1 for u in self.units.values()
                           if u.team == TEAM_PLAYER and _char_job(u.char_id) == want)
                ok = cleared and have >= max(1, limit)
            else:
                ok = False
            flags.append(1 if ok else 0)
        # CheckAppsFlyer reads index 3 unconditionally -- see the docstring.
        flags += [0] * (RATING_SLOTS - len(flags))
        return flags

    def rating_mask(self, flags=None):
        """The conditions met this run as a bitmask, bit i = `_rating_datas{i+1}`.

        This is the same shape `state["stages"][id]` stores and `GetStageRating` reads
        for the star row on the stage select -- which is why writing a flat 15 there
        was wrong twice over: it claimed stars the player had not earned, and it left
        nothing to compare against when deciding what to pay.
        """
        flags = self.rating_flags() if flags is None else flags
        return sum(1 << i for i, ok in enumerate(flags) if ok)

    def rating_rewards(self, already=0):
        """[(item_id, count), ...] for conditions met this run and NOT already earned.

        `already` is the stored mask for this stage. Rating rewards are one-time per
        condition in the real game; paying them on every clear turned a Hell Express
        stage into a 50-fragment-per-run Grimoire farm.
        """
        return [(row[1], row[2])
                for i, (row, ok) in enumerate(zip(self.rating_rows(),
                                                  self.rating_flags()))
                if ok and len(row) > 2 and not (already >> i) & 1]

    def drops(self):
        """[(item_id, count), ...] for the Drops row of the results panel.

        **The COUNT is right, the CONTENTS are a placeholder.** Two clears of footage
        agree that there is one drop per wave -- 1-1 has three waves and shows three
        icons, 2-1 has two and shows two -- and that is what this reproduces.

        What it does NOT reproduce is what actually drops. 1-1 pays three 250 coin
        stacks, but 2-1 pays an emblem x10 plus a character card, so "250 Mira per wave"
        was overfitted to the tutorial. Real per-stage drop tables were live-ops data:
        the stage row has no drop column at all (`box_rank` is a chest-rank pair, not
        items), so each stage needs its own footage to reconstruct, exactly like the
        karma payouts. Until then every stage drops coins.
        """
        if not self.wave_cleared():
            return []
        return stage_drops_for(self.stage_id, waves=self.wave_max)

    def wave_cleared(self):
        return not self.team_alive(TEAM_ENEMY)

    def party_wiped(self):
        return not self.team_alive(TEAM_PLAYER)

    def has_next_wave(self):
        return self.wave < self.wave_max

    def advance_wave(self):
        """Bring in the next wave's mob group. TurnEndState.DoNextState has already
        incremented the client's own BattleData.Wave by the time it asks for this."""
        self.wave += 1
        self._spawn_wave()
        # New wave's enemies get their battle-start passives now that they are on the
        # field; the party's already fired at battle open and does not re-trigger.
        self._apply_battle_start([u for u in self.units.values()
                                  if u.team == TEAM_ENEMY])
        self._roll_turn_order()
        # Every wave needs its own WaveBegin: BattleUnitManager.SetAllCollider() runs
        # only in HandleWaveBegin, and BattleUnit.InitBattleUnit does NOT add a
        # collider. Skip it and the new wave's enemies render but cannot be clicked --
        # the fight looks alive yet no attack can be aimed. OnWaveBegin with an empty
        # combo simply asks for the turn again, so re-sending it is safe.
        self.wave_begun = False
        self.turn_open = False

    def next_wave_strargs(self):
        """cmd 1503 takes three strings: the NEW ENEMIES ONLY (HandleNextWave clears
        the old ones and adds these to team 2), the new action order, and syncData."""
        enemies = {o: u.light() for o, u in self.units.items() if u.team == TEAM_ENEMY}
        return [json.dumps(enemies, separators=(",", ":")),
                json.dumps(self.turn_order, separators=(",", ":")),
                json.dumps({o: u.sync() for o, u in self.units.items()},
                           separators=(",", ":"))]

    def enemy_attack(self):
        """Pick a move for the acting enemy: its first skill against a live player."""
        return self.auto_move(TEAM_PLAYER)

    def auto_move(self, target_team):
        """Choose the acting unit's move against `target_team`.

        Used for BOTH the enemy turn and player auto-battle -- the two are the same
        problem, and auto-battle is server-driven: SituationJudgeState's OnEnter,
        OnUpdate and OnLeave are all stubs, so once the client is idling there it acts
        only when the SERVER pushes Attack (1201). It never picks a move itself, which
        is why enabling auto without this just hid the skill bar and deadlocked.
        """
        attacker = self.acting_unit()
        targets = [u for u in self.units.values()
                   if u.team == target_team and u.alive]
        if not attacker or not targets:
            return None
        # Strongest skill it is actually ALLOWED to use. skills[0] is the weak basic,
        # but picking the best one unconditionally let bosses fire their ultimate
        # every turn -- enemies obey the same cooldowns and charge gauge as the party.
        slot = max(self.usable_slots(attacker), key=lambda i: skill_ratio(attacker.skills[i]))
        return attacker.order, targets[0].order, attacker.skills[slot], slot

    def usable_slots(self, unit):
        """Skill buttons currently legal for a unit: off cooldown, and for the
        ultimate slot only with a full gauge. The basic is always available -- UNLESS
        ability_seal (Silence/Skill Seal/...) is active, which locks everything else."""
        if fx.has_flag(unit, "ability_seal"):
            return [0]
        slots = []
        for i in range(min(len(unit.skills), ULTIMATE_SLOT + 1)):
            if unit.cooldowns[i] > 0:
                continue
            if i == ULTIMATE_SLOT and not unit.ultimate_ready():
                continue
            slots.append(i)
        return slots or [0]

    # -- persistence: resume a fight across a server restart ----------------------
    # Only HP/max_hp/scv/cooldowns/statuses/dmg totals ever change after Unit.__init__
    # (atk/defense/spd/skills are fixed at construction from char_id+lv+star+book_bonus,
    # never mutated -- grep confirms it), so to_state() only needs those, plus enough of
    # the Battle-level bookkeeping to know which wave/turn/round it stopped at. Nothing
    # here is design-row-derived data (skills, wave_groups, avg lists, base stats) --
    # restore_battle() re-derives all of that by reconstructing fresh and replaying
    # wave advances, so a later design-data fix is picked up automatically instead of
    # being frozen into whatever was true when the save happened.
    def to_state(self):
        """-> a JSON-safe dict a killed/restarted server can hand back to
        restore_battle() to pick this fight back up exactly where it left off."""
        stage_id, team_char_ids, team_level, team_star, team_super_star, book_rank = \
            self._ctor_args
        return {
            "stage_id": stage_id, "team_char_ids": team_char_ids,
            "team_level": team_level, "team_star": team_star,
            "team_super_star": team_super_star, "book_rank": book_rank,
            "wave": self.wave, "round": self.round, "damage_sum": self.damage_sum,
            "auto": self.auto, "enemy_totals": self.enemy_totals,
            "turn_order": self.turn_order,
            "units": {o: u.to_state() for o, u in self.units.items()},
        }


def restore_battle(saved):
    """The inverse of Battle.to_state(): reconstruct fresh (deterministic from the
    stashed constructor args -- same char rosters, same stage), fast-forward through
    the waves already cleared (advance_wave() faithfully replays each wave's own
    battle-start passives and turn roll, exactly as the original run experienced
    them), then patch in the dynamic per-unit/per-battle state that has since
    diverged from a fresh construction. -> a live Battle, ready to answer the next
    request as if the server had never restarted.

    Raises on a genuinely unreconstructable save (e.g. the stage was removed from
    design data) -- the caller decides what "give up on this save" means, this
    function never silently returns a half-built battle."""
    battle = Battle(saved["stage_id"], saved["team_char_ids"], saved["team_level"],
                    saved["team_star"], saved["team_super_star"], saved["book_rank"])
    for _ in range(max(0, saved["wave"] - battle.wave)):
        battle.advance_wave()
    battle.round = saved["round"]
    battle.damage_sum = saved["damage_sum"]
    battle.auto = saved["auto"]
    battle.enemy_totals = saved["enemy_totals"]
    # wave_begun/turn_open stay False (advance_wave's default, and __init__'s for
    # wave 1) regardless of what they were at save time: the reconnect handoff always
    # re-sends WaveBegin/StartTurn fresh, so the client's own FSM re-enters cleanly
    # rather than trusting a mid-request flag from a connection that no longer exists.
    for order, u_state in saved["units"].items():
        unit = battle.units.get(order)
        if unit:
            unit.restore_state(u_state)
    # Overwrite the freshly-rolled order with the exact saved one -- a fresh roll on
    # full-HP units could differ from the saved order once dead units (0 HP, just
    # patched in above) are excluded, and turn_order is otherwise never recomputed
    # from HP changes mid-wave.
    battle.turn_order = [o for o in saved["turn_order"] if o in battle.units]
    if not battle.turn_order:
        battle._roll_turn_order()
    return battle

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
import dataclasses
import json
from typing import NamedTuple
import os
import random
import re

import design_data as dd
import settings
import battle_ai
from engine import core as _engine_core
from engine import status as _engine_status
from engine import passives as _engine_passives
from engine import specs as _engine_specs
from engine import wire as _engine_wire

# The move CHOOSER, same shape of escape hatch as the engine switch above and for the
# same reason: a bad weight table should be one environment variable away from the old
# behaviour, not a rollback. `SEVENSINS_BATTLE_AI=old` restores _auto_move_greedy.
TIER1_AI = os.environ.get("SEVENSINS_BATTLE_AI", "new").strip().lower() != "old"

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
# driven (battle_start / on_counter), not fired by an active use.
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
        # This branch had called a `_clamp` that exists nowhere: a NameError waiting
        # behind the flip switch above, invisible for as long as the default held, and
        # invisible to lint because titan_server's star import had switched pyflakes'
        # undefined-name check off. Found the day that import became explicit.
        rarity = int((dd.row("item", int(item_id)) or {}).get("_rarity") or 1)
        rank = max(1, min(BLOODPACT_AURA_RANKS, rarity))
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
# **Kind 4 is "clear with a SPECIFIC cast in the team"**, and `[3]` is that cast's
# charID -- not a turn/death threshold like the other kinds put there. 579 rows use it,
# across 122 distinct casts, every one of which resolves to a real `char` row whose
# `_group` is itself: 10001 Lucifer, 10011 Leviathan, 10021 Satan and on down. They sit
# in the bond dungeons (dmap 20001+), 8 rows to a map, and each pays a Diamond. Falling
# through to the catch-all meant that star could never be earned on ANY of them, no
# matter who was fielded, so the Diamond behind it never paid.
#
# Matching is by `_group`, NOT by raw charID, and it has to cut both ways:
#   * a Bunrei or grow-star form must COUNT -- 10002 "Lucifer's Bunrei XE" is its own
#     fieldable unit but carries `_group` 10001, so it satisfies a row naming Lucifer;
#   * an ALT COSTUME must NOT -- 20241 Beelzebub "Nightingale Empress" carries `_group`
#     20241 while 10051 Beelzebub carries 10051. Same character NAME, different units,
#     and the row names one of them specifically. Matching on name, or folding 20xxx
#     onto 10xxx, would hand the star to the wrong Beelzebub.
RATING_LEAD_CHAR = 4
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

# ---- generated drop tables -------------------------------------------------
#
# STAGE_DROPS below still wins outright: observed footage beats anything generated here.
# This is what a stage with NO observation pays.
#
# **The real per-stage drop tables are in no client form.** The client asks the server
# for them (StageRpc GetDrops 8 -> 25); they were live-ops data, and for ordinary stages
# that pool is simply gone.
#
# **THE TWO KINDS BELOW ARE NOT EQUALLY WELL FOUNDED. Do not read them as one thing.**
#
#   DERIVED -- the dungeons. A farm or bond dungeon exists to pay ONE resource, and the
#     stage's own Stage Clear row names it. There is a real answer and we read it, so
#     kizuna_drops and material_dungeon_drops reconstruct rather than invent. The
#     material dungeon amounts are fitted to live footage on top of that.
#
#   PLACEHOLDER -- ordinary stages, i.e. generated_drops and DROP_LADDER. A normal stage
#     had a POOL of items it rolled from and that information is lost; nothing in the
#     pack states it. The flat 250-per-wave coin payout was the original stand-in, and
#     this is a better-shaped stand-in, not a recovery. What it pays is grounded (every
#     item in the pool names Main Story as its own source -- see the note by
#     TRAINER_ITEMS) but HOW MUCH, and the whole ap/rarity ladder, is invented.
#
# So: correcting a dungeon means finding the row that already knew the answer.
# Correcting an ordinary stage means observation, or a deliberate design choice.
#
# `_ap` -- the stamina a run costs -- is the game's own statement of what a stage is
# worth, so it is the scale. 1-1 costs 5 ap and pays 250, which fixes COIN_PER_AP at 50.
# Note `_ap` is 0 on 1,569 of the 6,628 rows (arena, tutorial rooms, the ap-0 challenge
# stages); those floor at COIN_PER_WAVE in generated_drops rather than paying nothing.
BASE_AP = 5                                     # 1-1's cost, the 250 baseline's stage
COIN_PER_AP = COIN_PER_WAVE // BASE_AP          # 50 Mira per stamina point
COIN_SPREAD = 0.20                              # coins swing +/-20% per wave

# ---- the ordinary-stage drop POOL ------------------------------------------
#
# Membership comes from two INDEPENDENT sources that agree with each other, which is
# what makes this a reconstruction rather than a guess:
#
#   (a) THE ITEM SAYS SO. Each member's own `_note1` names Main Story as a source.
#       Read the JP note, not only the EN one -- the EN translation dropped the word
#       "Story" from the Gremlin Pieces, whose JP still reads
#       "【ストーリー/ダンジョン入手可能】" (obtainable from Story / Dungeon).
#   (b) IT WAS SEEN DROPPING. 19 live chapter 1-3 clears, 38 drop slots in total.
#
# Every member below is confirmed by at least one and contradicted by neither -- EXCEPT
# the karma gifts, which are a deliberate design addition and are marked as such.
#
# **Two near misses, kept out on purpose.** Both look like obvious members and both are
# refuted by their own notes, so check the note before adding anything here:
#   * EX Evolution Shard 139 -- "Can be exchanged in Belphie's Booth -> PVP Shop".
#     The contributed ladder dropped it from ap 40 up; that would have had story stages
#     printing a PVP-shop currency.
#   * Transcender Gremlin 111..115 (the WHOLE creatures) -- JP reads
#     "【魂の祭壇交換可能】", exchangeable at the Soul Altar. Only the PIECES drop.
TRAINER_ITEMS = {1: 101, 2: 102, 3: 103, 4: 104, 5: 105}
EVOLUTION_GEM = 556
GREMLIN_PIECE_TIERS = {1: 116, 2: 117, 3: 118, 4: 119, 5: 120}
MINION_SUMMON_ORB = 210         # observed as a x10 stack, matching STAGE_DROPS[2101]
MINION_SUMMON_ORB_COUNT = 10

# ---- karma gifts: a DESIGN CHOICE, not a reconstruction ---------------------
#
# These three are in the pool because we put them there, not because the data says so.
# Being explicit because every other member earned its place by evidence:
#   * No gift item names a source in ANY language -- all 148 `_action 3` rows carry a UI
#     hint ("Tap Karmameter to send gifts") where the pool members carry
#     "(Main Story / Daily Dungeon Reward)". Checked EN, JP, TC and SC.
#   * None was ever seen in a Drops slot across the 19 clears. Gifts appear only in the
#     RATINGS block as Stage Clear rewards (2103 pays Popular Manga x5, 2106 x10) and
#     the bond dungeons' clear rows are almost entirely gifts.
#
# The three chosen are the GENERIC ones -- the only gifts not tied to one cast's taste.
# Manga and Poster say "Everybody's favorite" outright; Ramen carries no favourite
# clause. Every other gift is "<Character>'s favorite" and stays out, or a story stage
# would be handing out one specific cast's gift track at random.
#
# WEIGHTED BY VALUE, INVERSELY. `_param2` is the karma each is worth, and they are far
# apart -- so a flat weight would make the Poster the dominant karma source in the game.
# Note the EN notes are stale here and claim "by 10" for both Manga and Poster; the JP
# and `_param2` agree on the real figures, which is what charprogress reads.
#
#     401 Ramen           10 karma   weight 3
#     486 Popular Manga   20 karma   weight 2
#     487 Popular Poster 100 karma   weight 1   <- lower this first if karma flows fast
KARMA_GIFT_WEIGHTS = ((401, 3), (486, 2), (487, 1))

# ap threshold -> (tier, material count span). ONE stack per slot; the span is an
# inclusive (low, high) for that stack. Rising stamina buys a higher TIER -- of trainer
# and of gremlin piece alike, both being ★1..★5 families -- and a slightly larger stack.
#
# **The counts are small on purpose.** In every observed clear the material stacks show
# no number at all, which is this panel's way of writing x1; the only stacks carrying a
# number are coin, the x10 summon orb and the x6 cards. So ap 5 pays exactly x1 and the
# high rungs grow slowly. An earlier draft paid Evolution Gem x146 at ap 120, which no
# footage supports.
DROP_LADDER = [
    (100, 5, (2, 4)),
    (80,  5, (2, 4)),
    (60,  5, (2, 3)),
    (40,  5, (1, 3)),
    (30,  5, (1, 3)),
    (25,  4, (1, 2)),
    (20,  4, (1, 2)),
    (15,  3, (1, 2)),
    (10,  2, (1, 2)),
    (5,   1, (1, 1)),
    (0,   1, (1, 1)),
]

# **Each WAVE contributes exactly one drop stack, and every stack is one pool roll.**
# That shape is the single most solid thing known about ordinary-stage drops: across all
# 19 clears the stack count equals the wave count, every time, and a clear can roll the
# same member twice (one 2-wave clear paid two trainer cards) or no coin at all.
#
#   1101 (3 waves)   coin 250 | ★1 Trainer | coin 250
#   2101 (2 waves)   coin 250 | coin 250
#   2109 (2 waves)   Evolution Gem | Minion Summon Orb x10
#   2110 (2 waves)   Evolution Gem | coin 250
#   3-1.. (2 waves)  ★1 Trainer | ★1 Trainer
#   3-x  (2 waves)   Evolution Gem | Gremlin Piece
#
# WEIGHTS are the observed slot frequencies over those 38 slots, which is why they are
# not round numbers. Coin is a POOL MEMBER like any other, not a fallback:
#
#      coin 14   evolution gem 9   trainer 8   gremlin piece 5   summon orb 2
#
# (The two x6 "cards" are counted as trainers -- the icon family matches and no other
# member is drawn as a character card.)
#
# The karma gifts are then added at weight 6 total, which is the ONE part of this table
# not read off the footage -- see the design-choice block above. They take ~14% of slots
# and leave the observed members' proportions to each other intact.
#
# Chapter 3 skewed materially heavier than chapters 1-2 (14 of 18 slots vs 10 of 20).
# That may mean the coin share falls with progression, but 38 slots cannot separate that
# from ordinary variance, so ONE weight table is used everywhere. It is the first thing
# to revisit if deeper footage turns up.
DROP_POOL_COIN = 14
DROP_POOL_WEIGHTS = (
    (EVOLUTION_GEM, 9),
    ("trainer", 8),             # tier from the ladder
    ("gremlin", 5),             # tier from the ladder
    (MINION_SUMMON_ORB, 2),
) + KARMA_GIFT_WEIGHTS          # 401 x3, 486 x2, 487 x1 -- design choice, see above

# On the 18 stages a "Starshards Hunter" quest names, that stage's ★1 shard joins the
# pool at this weight. Tuned for "farm, but not forever": on a 2-wave stage it is
# 8/52 a slot, so about 28% a clear -- three or four runs on average, which is a visible
# grind without being a wall. It has to be reachable: the same shards feed the goal-chain
# quest that wants a full Endearment set equipped, so six of them gate a later step.
STORY_SHARD_WEIGHT = 8

# ---- items whose note names SPECIFIC main-story stages ----------------------
#
# Two items do not merely say "Main Story" -- they name the stages, so they are placed
# rather than pooled:
#
#   1400009 Limbo Legacy        "Can be obtained in Main Story Normal stages,
#                                Hard 5-10, 6-10, 7-10, etc."
#   1400010 Stardust of Inferno "Can be obtained in Main Story Nightmare 5-10, 6-10,
#                                7-10, etc."
#
# Both are gacha-summon currencies -- Limbo Legacy is the SOLE cost of the "In the
# Enchanted Stars" banner (see player_state.gacha), which nothing else in the server
# grants, so that banner is unbuyable until these drop.
#
# "5-10" is chapter 5 stage 10, i.e. `_sort` 10 of a main-story dmap, and the "etc."
# means every chapter from 5 on. Normal Limbo Legacy is unrestricted across main story;
# the Hard/Nightmare grants are the chapter finales only.
STORY_DMAP_MAX = 100            # main-story dmaps are 1..34; the dungeons start at 20001
LIMBO_LEGACY_ITEM = 1400009
STARDUST_OF_INFERNO_ITEM = 1400010
CHAPTER_FINALE_SORT = 10        # the "X-10" the notes name
FINALE_DROP_MIN_CHAPTER = 5     # "5-10, 6-10, 7-10, etc." starts at chapter 5
# One extra slot, appended rather than replacing a pool roll: the notes describe these
# as things the stage gives, and the observed slot-count rule is about the POOL. Rolled
# so a finale is not a guaranteed farm.
FINALE_DROP_CHANCE = 0.5
STORY_SPECIAL_SPAN = (1, 2)


def story_chapter(stage_id):
    """(dmap, sort) for a main-story stage, or None when it is not one."""
    row = dd.row("stage", int(stage_id)) or {}
    dmap = int(row.get("_dmap_id") or 0)
    if not 0 < dmap < STORY_DMAP_MAX:
        return None
    return dmap, int(row.get("_sort") or 0)


def story_special_item(stage_id):
    """The item a note names for this stage by id, or None.

    Limbo Legacy on any Normal main-story stage and on the Hard chapter finales from
    chapter 5; Stardust of Inferno on the Nightmare finales from chapter 5. Straight
    off the two item notes -- nothing here is extrapolated beyond the "etc.".

    Split from the roll so the Drop Info preview can ask "CAN this stage pay it?"
    without rolling, which is a different question from "did it this time".
    """
    where = story_chapter(stage_id)
    if where is None:
        return None
    dmap, sort = where
    # BOTH items are chapter-finale only. Read the CHINESE note, not the English one:
    #
    #   TC 1400009  可在主線普通、困難的5-10、6-10、7-10...關卡中獲得
    #               "Main Story NORMAL AND HARD, stages 5-10, 6-10, 7-10..."
    #   EN 1400009  "Main Story Normal stages, Hard 5-10, 6-10, 7-10, etc."
    #
    # `普通、困難的` is ONE possessive phrase governing the stage list, so the numbers
    # qualify both difficulties. The EN comma-splice drops the shared qualifier and makes
    # "Normal stages" read as unrestricted -- which had this granting Limbo Legacy on
    # every Normal main-story stage in the game. Same translation-loss class as the
    # Gremlin Pieces and the Manga/Poster karma values.
    #
    # (The JP note on both rows is unrelated text about a different gacha banner, so it
    # is no help here; TC and SC agree with each other.)
    if sort != CHAPTER_FINALE_SORT or dmap < FINALE_DROP_MIN_CHAPTER:
        return None
    difficulty = stage_difficulty(stage_id)
    if difficulty in (DIFFICULTY_NORMAL, DIFFICULTY_HARD):
        return LIMBO_LEGACY_ITEM
    if difficulty == DIFFICULTY_NIGHTMARE:
        return STARDUST_OF_INFERNO_ITEM
    return None


def story_special_drop(stage_id, rng):
    """[(item, count)] for a stage a note names by id, else [] -- rolled."""
    item = story_special_item(stage_id)
    if item is None or rng.random() >= FINALE_DROP_CHANCE:
        return []
    return [(item, _roll(STORY_SPECIAL_SPAN, rng,
                         difficulty_multiplier(stage_difficulty(stage_id))))]


# ---- the ★1 starshards main story drops -------------------------------------
#
# **The quest table states this outright, one stage at a time.** The 18 "Starshards
# Hunter" quests each name a stage and a shard in their own text -- "Clear main story
# 2-1 and get ★1 Endearment Starshard ①" -- so this is read, not inferred:
#
#     chapter 2, stages 1-6  ->  Endearment slots 1-6   (201101..201601)
#     chapter 3, stages 1-6  ->  Defender   slots 1-6   (207101..207601)
#     chapter 4, stages 1-6  ->  Devotee    slots 1-6   (208101..208601)
#
# The slot always equals the stage number, and every one is ★1 rank 0 -- the three
# starter sets, which is what a new player is being walked through collecting.
#
# A shard is a POOL MEMBER on its stage, not a guaranteed drop. Two clears of 2-1 are on
# record and NEITHER paid a shard: one shows coin 250 twice, the other (STAGE_DROPS, from
# earlier footage) an orb and a trainer. The quest says "get", and a player is expected to
# farm for it -- which is also the only reading compatible with one stack per wave.
#
# Derived from the quest rows rather than typed out, so it cannot drift from them. The
# stage is only in the display text, so it is parsed -- but from the COLOUR-TAGGED number
# pair, which is identical in every language, not from English prose.
_STORY_SHARD_QUEST_CASE = 1             # `_case_type` 1 is the obtain-item counter
_story_shard_cache = None
_SHARD_STAGE_RE = re.compile(r"\[00FFFF\](\d+)-(\d+)\[-\]")


def story_shard_stages():
    """{stage id: ★1 shard item id} read off the "Starshards Hunter" quests."""
    global _story_shard_cache
    if _story_shard_cache is not None:
        return _story_shard_cache
    out = {}
    try:
        normal = {}
        for stage_id, row in dd.rows("stage").items():
            dmap = int(row.get("_dmap_id") or 0)
            if 0 < dmap < STORY_DMAP_MAX and int(row.get("_difficulty") or 0) == DIFFICULTY_NORMAL:
                normal[(dmap, int(row.get("_sort") or 0))] = int(stage_id)
        for _qid, quest in dd.rows("quest").items():
            if int(quest.get("_case_type") or 0) != _STORY_SHARD_QUEST_CASE:
                continue
            item_id = int(quest.get("_case_v1") or 0)
            if not starshard_slot(item_id):
                continue
            text = str(quest.get("_content_en") or quest.get("_content") or "")
            hit = _SHARD_STAGE_RE.search(text)
            if not hit:
                continue
            stage_id = normal.get((int(hit.group(1)), int(hit.group(2))))
            if stage_id:
                out[stage_id] = item_id
    except Exception:                       # noqa: BLE001 -- never break a clear
        out = {}
    _story_shard_cache = out
    return out


def starshard_slot(item_id):
    """The equipment slot a starshard item sits in (1..6), or 0 when it is not one.

    Ids encode ELEMENT*1000 + slot*100 + rank*10 + star, and `_action` is 110 + slot, so
    the row's own action is the check rather than a number range -- an id that merely
    looks like a shard but is not routed as one must not be treated as a drop.
    """
    action = int((dd.row("item", int(item_id)) or {}).get("_action") or 0)
    slot = action - 110
    return slot if 1 <= slot <= 6 else 0


def story_shard_drop(stage_id):
    """A RuneDrop for this stage's quest shard, or None."""
    item_id = story_shard_stages().get(int(stage_id))
    if not item_id:
        return None
    slot = starshard_slot(item_id)
    if not slot:
        return None
    return RuneDrop(display_item=item_id, item_id=item_id, slot=slot)


def generated_drop_pool(stage_id):
    """Every item id an ordinary stage CAN drop -> [item ids], in weight order.

    **This is the POOL, not a sample of it.** The Drop Info panel asks what a stage can
    pay, and answering it by rolling generated_drops once -- which is what this used to
    do -- shows only the two or three members that one roll happened to pick, so a stage
    whose pool has nine possibilities advertised two. That is the same "the panel starts
    lying" failure the preview already documents, arrived at from the other direction.

    One entry per possibility, which is the convention the Temple preview and the
    storefronts already use for anything random.
    """
    star, _span = drop_rung(stage_ap(stage_id))
    out = [COIN_ITEM_ID]
    for member, _weight in DROP_POOL_WEIGHTS:
        if member == "trainer":
            out.append(TRAINER_ITEMS[star])
        elif member == "gremlin":
            out.append(GREMLIN_PIECE_TIERS[star])
        else:
            out.append(member)
    shard = story_shard_stages().get(int(stage_id))
    if shard:
        out.append(shard)
    special = story_special_item(stage_id)
    if special is not None:
        out.append(special)
    return out


# `_difficulty` 1/2/3 = Normal/Hard/Nightmare -- confirmed by the same content existing
# three times, one row per tier: dmap 1 sort 1 is stage 1101 (ap 5, difficulty 1), 1201
# (ap 15, difficulty 2) and 1301 (ap 20, difficulty 3). The multiplier stacks ON TOP of
# the stamina scaling, so a Nightmare run already costs more ap and then pays a further
# 1.5x. `_difficulty` 4 is exactly 7 ap-0 "<Virtue>'s Challenge" rows under dmap 30009 --
# never observed, so it takes Nightmare's multiplier rather than an invented one.
DIFFICULTY_NORMAL, DIFFICULTY_HARD, DIFFICULTY_NIGHTMARE = 1, 2, 3
DIFFICULTY_MULTIPLIER = {DIFFICULTY_NORMAL: 1.0, DIFFICULTY_HARD: 1.2,
                         DIFFICULTY_NIGHTMARE: 1.5, 4: 1.5}
DIFFICULTY_MULTIPLIER_DEFAULT = 1.0


def stage_ap(stage_id):
    """The stamina a stage charges; 0 when it charges none (arena, tutorial rooms).

    Note this is the stage's NOTIONAL cost. Nothing deducts it -- see the stamina
    invariant in player_state.roster -- it is read here purely as a measure of worth.
    """
    row = dd.row("stage", int(stage_id)) or {}
    return int(row.get("_ap") or 0)


def stage_difficulty(stage_id):
    """1/2/3 = Normal/Hard/Nightmare. Defaults to Normal for a stage with no row."""
    row = dd.row("stage", int(stage_id)) or {}
    return int(row.get("_difficulty") or DIFFICULTY_NORMAL)


def difficulty_multiplier(difficulty):
    return DIFFICULTY_MULTIPLIER.get(int(difficulty or 0),
                                     DIFFICULTY_MULTIPLIER_DEFAULT)


def _scaled(count, mult):
    """Apply the difficulty multiplier without letting a nonzero drop round to nothing.

    round() rather than int(): 3 x 1.2 = 3.6 should be 4, not 3, or Hard would be
    indistinguishable from Normal at the small counts the early ladder pays.
    """
    if not count:
        return 0
    return max(1, int(round(count * mult)))


def _roll(span, rng, mult=1.0):
    """Roll an inclusive (low, high) span, apply the difficulty multiplier, then the
    server's drop_count rate. -> int.

    Every material stack in the pool comes through here, so this is the single point the
    rate needs to touch; `mult` is the stage's DIFFICULTY scaling and is a reconstruction,
    while the rate is the operator's deliberate choice on top of it.
    """
    low, high = span
    if high <= 0:
        return 0
    return settings.scale(_scaled(rng.randint(int(low), int(high)), mult), "drop_count")


def drop_rung(ap):
    """-> (trainer star, material count span) for a stamina cost."""
    for threshold, star, span in DROP_LADDER:
        if ap >= threshold:
            return star, span
    return DROP_LADDER[-1][1:]


def stage_clear_reward(stage_id):
    """(item_id, count) from the stage's guaranteed Stage Clear rating row, or None.

    `DesignStageRow.PaserRatingData` splits each `_rating_datas` on ',' into ints, so a
    row is [type, item, count, threshold]; type 1 is the unconditional Stage Clear row
    (the rest are graded on turns, deaths and so on). Battle.rating_rows parses the same
    columns for the star flags -- this is the module-level read of the one row that is
    guaranteed, which the drop generators use to learn what a dungeon is FOR.
    """
    row = dd.row("stage", int(stage_id)) or {}
    for i in range(1, RATING_SLOTS + 1):
        parts = dd.csv_ints(row.get(f"_rating_datas{i}"))
        if len(parts) >= 3 and parts[0] == 1:
            return int(parts[1]), int(parts[2])
    return None


# ---- material dungeons -----------------------------------------------------
#
# The three farm dungeons each exist to pay ONE resource, and the stage row says which:
#   dmap 30003 Treasure Raiders  -> Coin          (Stage Clear 5,000 .. 700,000)
#   dmap 30002 Evolution Abyss   -> Evolution Gem (Stage Clear x100 on every rung)
#   dmap 30004 Trainers Gym      -> ★3/★4 Trainer (Stage Clear x5 .. x15)
#
# **The two panels are different reward streams and must not be confused.** The results
# screen shows Ratings above Drops:
#
#     Ratings   Stage Clear                     Evolution Gem x100     <- ONE-TIME, all
#               Clear within 30 turns           Diamond x1                four of them
#               Clear within 20 turns           Diamond x3                (rating_rewards,
#               Less than 0 cast(s) defeated    Diamond x5                masked per stage)
#     Drops     Evolution Gem x4  x7  x9                               <- EVERY run
#
# That is live footage of an Evolution Abyss clear, and it is what this generator is
# fitted to. The Ratings block is handled entirely by rating_rewards and is paid once
# per condition; this function only ever produces the Drops row.
#
# What the footage establishes:
#   * The drop ITEM is the dungeon's own resource -- the same item as the Stage Clear
#     row, which is the only place the pack states what a farm rung is for. That also
#     means each dungeon's real ladder is reproduced in proportion: Trainers Gym
#     switches ★3 -> ★4 where its own rungs do, Treasure Raiders climbs 5,000 ->
#     700,000, and the "SP" rungs pay Master Coin.
#   * ONE STACK PER WAVE. All 48 Evolution Abyss stages are 3-wave and the panel shows
#     exactly 3 stacks, which is the same per-wave shape the coin drops already use.
#   * The stacks are ROLLED, not fixed: 4 / 7 / 9 on one clear.
#
# And the two constants below are read straight off it rather than invented:
#   * 4 + 7 + 9 = 20 against a Stage Clear reward of 100 -> DUNGEON_DROP_SHARE = 0.20.
#   * 20 over 3 waves is 6.67 a wave, and the observed 4..9 is that mean +/-35%
#     -> DUNGEON_SPREAD = 0.35, which regenerates the footage's own band.
#
# One clear is one sample, so the SHARE is the part to re-check first if more footage
# turns up -- the per-wave shape and the item are directly observed. The ascending 4/7/9
# may also mean later waves pay more rather than each wave rolling independently; one
# clear cannot tell those apart, and independent rolls are the weaker assumption.
#
# For reference, this replaces the contributed version's `base x (1 + 0.05 * rung)`
# ramp, which re-derived the count from the dungeon's FIRST rung and paid it as a single
# stack -- both the wrong shape and, at 15,388 against a listed 700,000, the wrong size.
MATERIAL_DUNGEON_DMAPS = {30002, 30003, 30004}
DUNGEON_DROP_SHARE = 0.20       # a clear drops this much of the Stage Clear reward...
DUNGEON_SPREAD = 0.35           # ...split per wave, each rolled at this spread


def material_dungeon_drops(stage_id, waves=1, rng=None):
    """[(item, count) per wave] for a farm-dungeon stage, or None when it is not one."""
    row = dd.row("stage", int(stage_id)) or {}
    if int(row.get("_dmap_id") or 0) not in MATERIAL_DUNGEON_DMAPS:
        return None
    reward = stage_clear_reward(stage_id)
    if not reward:
        return None
    item_id, clear_count = reward
    if not clear_count:
        return None

    import random as _r
    rng = rng or _r
    waves = max(1, int(waves))
    mult = difficulty_multiplier(stage_difficulty(stage_id))
    # Flat by default, and only the Abyss can climb -- see `_evolution_depth_bonus`.
    per_clear = clear_count * DUNGEON_DROP_SHARE * _evolution_depth_bonus(stage_id, row)
    per_wave = per_clear / waves
    lo = int(per_wave * (1.0 - DUNGEON_SPREAD))
    hi = int(per_wave * (1.0 + DUNGEON_SPREAD))
    return [(item_id, settings.scale(
                # FLOOR OF ONE. `5 * 0.20` is 1.0 a wave, and the spread then rounds the
                # bottom of the band to zero -- so rung 1 of the Trainers Gym paid
                # literally nothing, an empty Drops panel on a cleared stage. No dungeon
                # in the game hands back an empty clear; whatever the arithmetic says,
                # the smallest honest payout is one.
                max(1, _scaled(rng.randint(min(lo, hi), max(lo, hi)) if hi > lo
                               else int(per_wave), mult)), "dungeon_drop"))
            for _ in range(waves)]


# ---- HOUSE RULE: the Evolution Abyss can be made to reward depth ------------------
#
# Retail pays the same at Evo-1 and Evo-48 -- same Stage Clear (100), same stamina (1),
# only the enemy level climbs (11 -> 555). That is deliberate: the Trainers Gym is the
# same family on the same 1 stamina and DOES scale its reward by rung, so the designers
# scaled where they wanted to and left this flat. What the ladder actually pays for is
# the one-time rating diamonds, not repeatable gem income.
#
# So this is not a reconstruction and must never be mistaken for one. It exists because a
# 48-rung ladder that pays the same at the bottom and the top is a poor private-server
# experience, and `settings.RATES["evolution_depth"]` is where that opinion lives: 1.0
# restores retail exactly, and the code path below is skipped entirely at that value.
#
# Rung 1 is the fixed point of the ramp, so the live footage the `dungeon_drop` default
# reproduces (a clear paying 4+7+9 against a Stage Clear of 100) still reproduces at any
# setting. Only depth is bought.
EVOLUTION_DMAP = 30002
_evolution_span = None


def _evolution_depth_bonus(stage_id, row):
    """-> the multiplier for this rung, 1.0 everywhere except a tuned-up Abyss."""
    global _evolution_span
    if int(row.get("_dmap_id") or 0) != EVOLUTION_DMAP:
        return 1.0
    depth = settings.rate("evolution_depth")
    if depth <= 1.0:
        return 1.0                      # faithful, and the arithmetic below is skipped
    if _evolution_span is None:
        sorts = [int(r.get("_sort") or 0) for r in (dd.rows("stage") or {}).values()
                 if int(r.get("_dmap_id") or 0) == EVOLUTION_DMAP]
        _evolution_span = (min(sorts), max(sorts)) if sorts else (1, 1)
    first, last = _evolution_span
    order = max(first, min(last, int(row.get("_sort") or first)))
    # Linear from 1.0 at the shallowest rung to `depth` at the deepest.
    return 1.0 + (depth - 1.0) * (order - first) / float(max(1, last - first))


# ---- Department Store tower floors (NOT the bond dungeons) ------------------
#
# **These 120 stages are not bond dungeons and the coin is not a cast's gift track.**
# They are the Tower Base / Tower Body floors of the Skyscraper Department Store event
# (dmap root 41416), and 9511-9520 are its ten CATEGORY tokens -- Drink, Restaurant,
# Cosmetic, Sex Toy, Music, Festival, Gaming, Sports, Bookstore, Weapon. The item says so
# itself: 9511 `_note1_en` is "Please exchange drink items at the Department Store".
#
# The actual bond dungeons are dmaps 22001-22076, one per cast, and they pay that cast's
# FAVOURITE GIFT as a one-time Stage Clear reward (Ravinia: 4/6/8 Roses by difficulty).
# Different subsystem, different economy -- see docs/EVENT_TOWERS.md, which also records
# that the Department Store's exchange never shipped, so these tokens currently buy
# nothing.
#
# WHICH token is not guessed: it is the stage's own guaranteed Stage Clear reward --
# `1,9511,1` on dmap 41419 is Drink Coin, through 9520 Weapon Coin. So the panel and the
# payout cannot disagree about which category a run feeds.
#
# **Exactly 120 stages qualify.** The rest of the family has a type-1 reward of item 36
# (Soul Gem, the generic Kizuna-skill material) and is deliberately NOT treated as karma
# -- paying Soul Gems as though they were a gift coin would be wrong. Those fall through
# to the generic generator.
KARMA_COIN_MIN, KARMA_COIN_MAX = 9511, 9520

# Chest tier -> how many coins a NORMAL clear pays. Bronze 1 / Silver 2 / Gold 3 is the
# whole Normal band, so the tier IS the roll on Normal; Hard and Nightmare widen it.
# **All 120 karma stages in this pack are `_difficulty` 1**, so the Hard and Nightmare
# bands are unreachable today. They are here so a rebuilt dungeon carrying a higher tier
# works without another code change, not because such a stage has been seen.
KIZUNA_CHEST_BRONZE, KIZUNA_CHEST_SILVER, KIZUNA_CHEST_GOLD = 1, 2, 3
KIZUNA_DROP_BAND = {
    DIFFICULTY_NORMAL: (KIZUNA_CHEST_BRONZE, KIZUNA_CHEST_GOLD),     # 1-3
    DIFFICULTY_HARD: (3, 5),
    DIFFICULTY_NIGHTMARE: (7, 10),
    4: (7, 10),
}


def kizuna_karma_item(stage_id):
    """The Karma coin a bond stage pays, or None when it is not a karma stage."""
    reward = stage_clear_reward(stage_id)
    if not reward:
        return None
    item_id, _count = reward
    return item_id if KARMA_COIN_MIN <= item_id <= KARMA_COIN_MAX else None


def kizuna_drops(stage_id, rng=None):
    """[(karma coin, count)] for a bond stage -- REPLACES the generic loot entirely.

    A bond run exists to feed one character's gift track, so paying it trainers and
    evolution material instead would make the dungeon pointless. Returns None for a
    stage that is not one, so the caller falls through to the ordinary generator.
    """
    item_id = kizuna_karma_item(stage_id)
    if item_id is None:
        return None
    import random as _r
    rng = rng or _r
    band = KIZUNA_DROP_BAND.get(stage_difficulty(stage_id),
                                KIZUNA_DROP_BAND[DIFFICULTY_NORMAL])
    return [(item_id, settings.scale(rng.randint(*band), "dungeon_drop"))]


# **Evolution Gem is NOT gated behind the Evolution Abyss.** An earlier draft locked it
# until a stage reached the dungeon's own entry rung (lowest `_stagelv` in dmap 30002,
# which is 11) on the reasoning that handing out rank-up material before the farm opens
# makes the farm pointless. Live footage says otherwise: chapter 2 is `_stagelv` 1..10
# and Evolution Gem drops there in four separate observed clears. The gate would have
# blocked precisely what the game actually paid, so it is gone rather than retuned.

def _pool_roll(rng, stage_id, ap, mult):
    """One drop slot -> (item id, count) or a RuneDrop, drawn from the pool by weight."""
    star, span = drop_rung(ap)
    shard = story_shard_drop(stage_id)
    total = DROP_POOL_COIN + sum(w for _m, w in DROP_POOL_WEIGHTS)
    if shard is not None:
        total += STORY_SHARD_WEIGHT
    roll = rng.random() * total
    if shard is not None:
        if roll < STORY_SHARD_WEIGHT:
            return shard
        roll -= STORY_SHARD_WEIGHT
    if roll < DROP_POOL_COIN:
        # An ap-0 stage floors at the flat rate rather than paying nothing. 250 at ap 5
        # is the observed anchor and every observed coin stack in main story is 250.
        base = max(COIN_PER_WAVE, ap * COIN_PER_AP)
        lo = int(base * (1.0 - COIN_SPREAD))
        hi = int(base * (1.0 + COIN_SPREAD))
        return COIN_ITEM_ID, settings.scale(_scaled(rng.randint(lo, hi), mult), "coin")
    roll -= DROP_POOL_COIN
    member = DROP_POOL_WEIGHTS[-1][0]
    for candidate, weight in DROP_POOL_WEIGHTS:
        if roll < weight:
            member = candidate
            break
        roll -= weight

    if member == "trainer":
        return TRAINER_ITEMS[star], _roll(span, rng, mult)
    if member == "gremlin":
        return GREMLIN_PIECE_TIERS[star], _roll(span, rng, mult)
    if member == MINION_SUMMON_ORB:
        # Observed as a x10 stack twice, never any other size.
        return MINION_SUMMON_ORB, _scaled(MINION_SUMMON_ORB_COUNT, mult)
    return member, _roll(span, rng, mult)


def generated_drops(stage_id, waves=1, rng=None):
    """The Drops row for a stage with no observed table -> [(item, count) per wave].

    ONE stack per wave, each an independent roll of the pool -- see the slot model above
    the weights. A stage whose own item notes name it (the Limbo Legacy / Stardust of
    Inferno chapter finales) may add one further stack on top.
    """
    import random as _r          # local, matching starshard_temple_drops
    rng = rng or _r
    ap = stage_ap(stage_id)
    mult = difficulty_multiplier(stage_difficulty(stage_id))
    out = [_pool_roll(rng, stage_id, ap, mult) for _ in range(max(1, int(waves)))]
    out.extend(story_special_drop(stage_id, rng))
    return out


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
# The candidate count is now one per wave; this is the ceiling the CLIENT imposes on
# that. `PanelRuneSelect.OnPanelEnable` indexes a 3-entry base table by `count-1` and
# has objects for `_rune1`/`_rune2`/`_rune3` only, so a fourth candidate would be rolled
# server-side and never drawn. Every Temple floor is 2 or 3 waves, so this does not bind
# today -- it is here so that a 4-wave floor could never silently lose a shard.
STARSHARD_PANEL_MAX = 3
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
    in the engine suites exists to catch (and did catch, here).

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


def starshard_temple_drops(stage_id, rng=None, when=None, waves=None):
    """[RuneDrop, ...] the CANDIDATES a Temple clear offers -- one per wave.

    Each rolls its own set, slot, star and rank. Star AND rank both come from the
    stage's own `box_rank` band now; the rank used to be one flat table shared by all 41
    floors, which is what made deep runs feel the same as shallow ones.

    **One per wave, like every other stage in the game** -- the Temple is unusual in that
    the player then KEEPS one of them rather than all, but the count is the same rule.
    This was a flat 2 for every floor, read off a live panel showing a slot I next to a
    slot II; the 10 two-wave floors reproduce that exactly, and the 31 three-wave floors
    were quietly offering one candidate fewer than their waves.

    Three is safely inside what the client draws: `PanelRuneSelect` lays out 1, 2 or 3
    (`_rune1_Middle_Info`, `_rune2_Left/Right_Info`, `_rune3_*`) chosen by a `count-1`
    lookup into a base table of {1, 2, 4} -- see docs/STARSHARD_TEMPLE.md. Six slots are
    eligible, so three distinct candidates are always available to draw.
    """
    import random as _r
    rng = rng or _r
    stars = starshard_star_weights(stage_id)
    ranks = starshard_rank_weights(stage_id)
    if not stars or not ranks:
        return []
    sets = starshard_sets_for_day(when)
    if waves is None:
        row = dd.row("stage", int(stage_id)) or {}
        waves = len(dd.csv_ints(row.get("_mobGroup_datas"))) or STARSHARD_DROPS_PER_CLEAR
    # Distinct slots, so the candidates are a real choice rather than near-duplicates --
    # the live panel shows a slot I next to a slot II. Capped at what the panel can lay
    # out; a fourth would have no object to render into.
    count = max(1, min(int(waves), STARSHARD_PANEL_MAX, len(STARSHARD_TEMPLE_SLOTS)))
    slots = list(STARSHARD_TEMPLE_SLOTS)
    rng.shuffle(slots)
    out = []
    for slot in slots[:count]:
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


def transcend_corridor_drops(stage_id, rng=None, waves=None):
    """[(item id, count)] of Gremlin Pieces for a clear, or [] if not the dungeon.

    ONE STACK PER WAVE, which is the rule every other stage in the game follows: a
    3-wave clear shows three drop icons. This paid the whole clear as a single stack
    regardless, so all 80 corridor stages -- every one of them 3 waves -- drew one icon
    where the panel has room for three.

    The TOTAL is unchanged: the same `GREMLIN_PIECES_PER_CLEAR` is split across the
    waves rather than handed over at once, and the tier is still rolled once for the
    clear. Splitting a total nobody has re-observed is a presentation fix, and rolling a
    fresh tier per wave would have been a balance change wearing its clothes.
    """
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
    if waves is None:
        waves = len(dd.csv_ints(row.get("_mobGroup_datas"))) or 1
    waves = max(1, int(waves))
    item = GREMLIN_PIECE_ITEMS[tier - 1]
    # Spread the remainder over the EARLY waves rather than dropping it: 4 pieces over
    # 3 waves is 2/1/1, never 1/1/1 with one piece quietly lost.
    base, extra = divmod(count, waves)
    return [(item, base + (1 if i < extra else 0)) for i in range(waves)
            if base + (1 if i < extra else 0) > 0]


STAGE_DROPS = {
    # Trainers Gym (dmap 30004) -- the material dungeon for Level Training, so it pays
    # Trainers (items 101-105). 1400001 "Beginner Class" observed dropping ★2 Trainer x6.
    # NOTE the lowest rung does NOT pay the lowest tier: "Beginner Class" is stagelv 1
    # yet gives ★2, not ★1, so do not extrapolate the remaining rungs from the stagelv
    # ladder -- each one needs its own observation. (Main-story 2-1, a far easier stage,
    # pays ★1 -- and the ★1 and ★2 icons look near-identical, so read the tier off the
    # item name rather than the picture.)
    1400001: [(102, 6)],

    # **2-1 and 2-2 USED to be pinned here and no longer are.** Their observations are
    # kept as a note rather than a table because the pool now covers both, and because
    # two clears of 2-1 disagree with each other:
    #
    #     2101   observed once as ★3 Minion Summon Orb x10 + ★1 Trainer
    #            observed again (footage) as coin 250 + coin 250
    #     2102   observed as ★1 Endearment II + ★1 Trainer
    #
    # Neither 2-1 result contains the other, so these are POOL ROLLS, not a fixed table
    # -- every item in them is a pool member, and the Endearment shard is exactly what
    # story_shard_stages() reads off quest 20062 for that stage. Pinning them here did
    # real harm: STAGE_DROPS short-circuits before the pool runs, so 2-1 could never pay
    # the ★1 Endearment I its own "Starshards Hunter" quest requires, and that quest was
    # uncompletable no matter how many times it was cleared.
    #
    # WARNING for whatever goes here next: a malformed storage-2 entry makes
    # PlayerBackpack's login sync throw partway through; the sync never signals
    # completion, so the client hangs forever on `Subsystem 'PlayerBackpack' still in
    # syncing...` AND, because the entry is persisted, so does every later login until it
    # is deleted by hand. `attr` must always carry `lv`. See player_state.make_rune.
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
    # Resolved BEFORE the branches below, not after: one drop per wave is the rule the
    # whole game follows, so every branch needs the wave count, not just the last one.
    if waves is None:
        row = dd.row("stage", int(stage_id)) or {}
        waves = len(dd.csv_ints(row.get("_mobGroup_datas"))) or 1
    # **A Starshard Temple stage MUST drop starshards, or the client hangs.**
    # See starshard_temple_drops.
    temple = starshard_temple_drops(stage_id, rng, waves=waves)
    if temple:
        return temple
    gremlins = transcend_corridor_drops(stage_id, rng, waves=waves)
    if gremlins:
        return gremlins
    # A bond stage pays its karma coin INSTEAD of generic loot, and a farm dungeon pays
    # the one resource it exists for; only what neither claims falls to the generator.
    karma = kizuna_drops(stage_id, rng)
    if karma is not None:
        return karma
    farmed = material_dungeon_drops(stage_id, waves, rng)
    if farmed is not None:
        return farmed
    return generated_drops(stage_id, waves, rng)


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
    per possibility, the same convention the storefronts use for a random box, and the
    rule every branch below follows: **list what CAN drop, never a sample of it.**
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
    # A bond stage and a farm dungeon each pay exactly ONE item, named by the stage's own
    # Stage Clear row, so their preview is that item -- read, not rolled.
    karma = kizuna_karma_item(stage_id)
    if karma is not None:
        return [karma]
    row = dd.row("stage", int(stage_id)) or {}
    if int(row.get("_dmap_id") or 0) in MATERIAL_DUNGEON_DMAPS:
        reward = stage_clear_reward(stage_id)
        if reward and reward[1]:
            return [reward[0]]
    # An ordinary stage rolls each wave slot from the pool, so the panel lists the whole
    # pool. Rolling generated_drops once and previewing the result -- which this used to
    # do -- advertised only the two or three members that one roll picked.
    seen, out = set(), []
    for item_id in generated_drop_pool(stage_id):
        if item_id not in seen:
            seen.add(item_id)
            out.append(item_id)
    return out


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


# The client's own rarity -> minimum star table: Game.Player.Char.CharRareMinStar,
# N = 1, R = 3, SR = 4, SSR = 5, LE = 5 (verified in the 2.2.7 dump). `_rarity` is the
# tier number, 1 = N up to 5 = LE, which is how gacha.py already buckets its pools.
#
# THIS USED TO BE `star <= rarity` -- the rarity number reused as a rung index -- and
# that is one rung LOW for the two tiers where the table is not the identity: an SSR
# Awaker entered play at *4 instead of *5, an SR cast at *3 instead of *4. A whole
# growth rung of HP/ATK/DEF/SPD missing, and it disagreed with the gacha, which grants
# by bucket: the same Awaker was *5 from a pull and *4 from a shard exchange or a quest
# reward. (Contributed fix, UserContrib rarity-star-mapping.)
RARITY_MIN_STAR = {1: 1, 2: 3, 3: 4, 4: 5, 5: 5}


def rarity_min_star(char_row):
    """-> the star a cast of this rarity enters play at (CharRareMinStar)."""
    return RARITY_MIN_STAR.get(int(char_row.get("_rarity") or 1), 1)


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
    allowed = [s for s in valid if s <= rarity_min_star(char_row)]
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




class Unit(_engine_core.Unit):
    """One combatant. `order` is the dictionary key the client uses everywhere --
    BattleUnitManager stores units by it and every later message (damage targets,
    turn order, HP sync) refers to units by this string.

    Subclasses the ENGINE's unit rather than duplicating it, so the server has exactly
    one combatant model. The base carries the combat stats the new engine reads;
    everything added here is identity, progression or wire serialisation that the old
    engine still owns. Because it is ONE object there is nothing to mirror -- a status
    the engine applies lands on the same list this class serialises.

    The base is a dataclass but this defines its own `__init__` and never calls
    `super().__init__`: the base's scalar fields (`cri`, `ddi`, ...) are plain class
    attributes and read correctly unset, while `statuses` uses a default_factory and so
    is assigned explicitly below.
    """

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
        # `_job` IS the attribute (STR/AGI/TEC/ABYSS/SOLAR); see engine.formula for how
        # that was established. Kept under both names: `job` because the class-aura code
        # reads it that way, `attribute` because that is the base class field the
        # engine's advantage triangle reads.
        self.job = self.attribute = row.get("_job") or 0
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
        # SECONDARY STATS. engine.formula has always modelled crit rate, crit damage,
        # pierce and effect accuracy -- `getattr(caster, "cri", None)` at formula.py:184
        # and the `ehit`/`eanti` pair in effect_lands -- but nothing ever SET them, so
        # they read off a Unit that had no such attributes and every cast in the game
        # fought at the BASE_CRIT_RATE fallback with no crit-damage scaling at all.
        #
        # SCALE: these are CharAttribute.PercentStyleAttrs, stored MULTIPLIED BY TEN
        # (150 renders as "15.0%" -- char_flv 2000128 says 15% where _flvBonus says
        # 150), so /1000 converts a design value into the 0..1 fraction formula wants.
        #
        # THE SPLIT BETWEEN None AND 0.0 IS THE ENGINE'S CONTRACT, NOT A CHOICE HERE.
        # `_engine_core.Unit` declares `cri: Optional[float] = None` and every other one
        # as `float = 0.0`, and the two are read differently on purpose:
        #
        #   cri  -- `BASE_CRIT_RATE if crit_rate is None else float(crit_rate)`.
        #           None MEANS "use the base rate". Assigning 0.0 for an unpaid cri
        #           therefore does not preserve behaviour, it silently drops EVERY unit
        #           in the game, mobs included, from the 5% base crit to 0%.
        #   rest -- added and subtracted directly (`1.0 + caster.cdi - target.cdr`,
        #           `mitigation(..., caster.prc)`, `chance + ehit - eanti`), so a None
        #           there is a TypeError mid-fight. Absent genuinely means "no bonus".
        _cri = gear.get("cri")
        self.cri = None if _cri is None else _cri / 1000.0
        for _attr in ("cdi", "cdr", "prc", "ehit", "eanti"):
            setattr(self, _attr, gear.get(_attr, 0) / 1000.0)
        #
        # Today the only source that reaches here is the Consonance ladder's rungs 28
        # and 30 (roster._annotate_consonance). Starshard/soulmirror CRI/CDI sub-stats
        # are still dropped by gear.equipped_stat_bonus -- see the stale comment there;
        # wiring those up is its own piece of work, not this one.
        self.max_hp = stats["hp"] + bonus.get("hp", 0) + gear.get("hp", 0)
        self.hp = self.max_hp
        self.atk = stats["atk"] + bonus.get("atk", 0) + gear.get("atk", 0)
        self.defence = stats["def"] + bonus.get("def", 0) + gear.get("def", 0)
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
        # Active buffs/debuffs (engine.status.Active). The engine appends here;
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
        only to compare units against each other, so the absolute scale is arbitrary.

        A unit whose gauge cannot rise never becomes ready, so it reports infinity --
        `Headwind` says "Move Gauge will not increase", and without this it kept its
        place in the queue and took turns anyway.
        """
        if _engine_status.blocks_gauge_gain(self):
            return float("inf")
        return max(0.0, SCV_FULL - self.scv) / self.effective_spd()

    def effective_spd(self):
        """SPD as the move gauge sees it: base SPD through the unit's active SPD statuses.

        THIS USED TO BE `self.spd` EVERYWHERE THE GAUGE IS COMPUTED, so every speed
        status in the game -- SPD UP, Slow, Admonition (Reduce SPD), Linear Speedup,
        Power Fist -- was cosmetic: it changed the number on the stat popup and nothing
        about who acted when. Found 2026-08-26 reading the client: its own action-line
        prediction (BattleUnitManager.GetNextAction) runs (100 - scv) / SPD off the
        SPD in `sync`, so the server has to both USE the modified speed and SEND it, or
        the "next" badge on the phone points at a different unit than acts.

        Every SPD status in the registry is a percentage (34 of 34 with a stated
        magnitude), so the multiplier is the whole story here. Floored at 1 so a
        stack of Slows cannot stop the gauge -- that is Headwind's job, and it has
        its own path (blocks_gauge_gain).
        """
        return max(1.0, float(self.spd) * _engine_status.stat_multiplier(self, "SPD"))

    def fill_gauge(self, seconds):
        if _engine_status.blocks_gauge_gain(self):
            return
        self.scv = min(float(SCV_FULL), self.scv + seconds * self.effective_spd())

    def passives(self):
        """The unit's PASSIVE skill ids that the effect engine fully understands.
        Only `complete` passives fire, so a half-parsed one is silently inert rather
        than firing a guessed effect (Lucifer's Fear Nothing, not yet complete, is one)."""
        return [s for s in self.skills
                if (dd.row("skill", s) or {}).get("_type") == SKILLTYPE_PASSIVE
                and (_engine_specs.skill(s) or {}).get("type") == "passive"]

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
            "atk": self.atk, "def": self.defence, "spd": self.spd,
            "scv": int(self.scv) if current else SCV_FULL,
            # Report what the unit actually carries. These are real fields the client
            # renders in the stat popup, and sending 0 while the engine fought with a
            # real value is what made the panel disagree with the damage.
            #
            # Back to the DESIGN scale (x10) the client expects -- the inverse of the
            # /1000 in __init__. `cri` is None when nothing paid it; the wire has no way
            # to say "use the engine default", so an unpaid cri goes out as 0 and the
            # popup shows the pack's own number rather than our BASE_CRIT_RATE, which is
            # a design choice of ours and not something the client should be told.
            #
            # tgn/ddi/ddr stay 0: nothing in the pack pays them yet.
            "cri": int(round((self.cri or 0.0) * 1000)),
            "tgn": 0,
            "cdi": int(round(self.cdi * 1000)),
            "cdr": int(round(self.cdr * 1000)),
            "prc": int(round(self.prc * 1000)),
            "ehit": int(round(self.ehit * 1000)),
            "eanti": int(round(self.eanti * 1000)),
            "ddi": 0, "ddr": 0,
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
        """BattleUnitManager.SyncData reads exactly [MaxHP, HP, Scv, SPD].

        SPD is the EFFECTIVE speed, not the base: the client feeds this value straight
        into its own action-line prediction (GetNextAction: (100 - scv) / SPD), so a
        Slowed unit reported at base SPD gets its "next" badge one place too early.
        """
        return [self.max_hp, self.hp, int(self.scv), int(round(self.effective_spd()))]

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
        # None = a legacy status the registry could not name; dropped, see
        # _migrate_legacy_status.
        self.statuses = [st for st in
                         (_status_from_state(s) for s in saved.get("statuses", []))
                         if st is not None]


def _status_to_state(st):
    """A status -> its save form. Engine statuses round-trip whole: they carry their
    meaning in plain fields rather than a catalog link, so `asdict` is lossless.

    Still TAGGED `_engine`, even though it is now the only kind written, because
    `_status_from_state` has to keep reading saves written before the cutover -- an
    untagged dict is a legacy status and takes the migration path.

    The tag also cost a live drop when it was missing: an in-progress fight is persisted
    on every message (battle resume), so the FIRST turn after an engine status landed
    raised `'Active' object has no attribute 'dot_atk'` and killed the connection.
    """
    return {"_engine": True, **dataclasses.asdict(st)}


def _status_from_state(d):
    if d.get("_engine"):
        # Unknown keys are DROPPED rather than passed through: a save written by a newer
        # build must not crash an older one on a field its dataclass has never heard of.
        fields = {f.name for f in dataclasses.fields(_engine_status.Active)}
        return _engine_status.Active(**{k: v for k, v in d.items()
                                        if k != "_engine" and k in fields})
    return _migrate_legacy_status(d)


def _migrate_legacy_status(d):
    """A saved `battle_effects.Status` -> the engine's `Active`, or None to drop it.

    Battles are persisted on EVERY message, so at any moment there are live saves whose
    units carry old-format statuses. Without this they would either crash the restore or
    silently keep a status the engine cannot read -- and an in-progress raid is not a
    thing to throw away on a deploy.

    The fields line up better than they look, because both models store the same runtime
    facts under different names:

        remaining "battle" -> None (the engine's permanent)
        dot_atk            -> source_atk   (inflicter's ATK, snapshotted at apply time)
        taunt_source       -> source_order (who this status forces you to attack)

    A name the registry does not know is dropped, not invented. That loses a buff on one
    restore, which is recoverable; a status with a made-up id is not -- the client feeds
    every id to GetRow and that throws (contract 3.8).
    """
    name = d.get("name")
    sid = _engine_passives.registry_id(name) if name else None
    if sid is None:
        return None
    row = _engine_specs.status(sid) or {}
    remaining = d.get("remaining")
    return _engine_status.Active(
        status_id=sid, name=row.get("name") or name,
        kind=row.get("kind"), category=row.get("category"), stat=row.get("stat"),
        remaining=None if remaining in ("battle", None) else int(remaining),
        stacks=int(d.get("stacks") or 1),
        stack_cap=row.get("stack_cap"),
        unremovable=bool(row.get("unremovable")),
        source_atk=d.get("dot_atk"),
        source_order=d.get("taunt_source"),
        shield_hp=int(d.get("shield_hp") or 0))


def _char_job(char_id):
    """DesignChar `_job`: 2 STR / 3 AGI / 4 TEC. -> 0 when the row is unknown."""
    return int((dd.row("char", int(char_id)) or {}).get("_job") or 0)


_grade_group_index = None


def _grade_groups():
    """{any grow-star grade id: canonical group id}. Lazy, cached, never raises.

    `_group` alone is not quite enough. A unit declares its whole grade ladder in
    `_growStar` -- Lucifer declares 10001..10006 plus 110001..110006 -- but the pack
    ships only some of those as rows of their own: **1,327 of the 3,801 declared grade
    ids have no `char` row at all.** A grade with no row would fall back to comparing
    equal only to itself, so a starred-up cast could fail a condition naming its base.
    Indexing `_growStar` covers the grades that were never shipped as rows.
    """
    global _grade_group_index
    if _grade_group_index is not None:
        return _grade_group_index
    index = {}
    try:
        for char_id, row in dd.rows("char").items():
            group = int(row.get("_group") or char_id)
            index[int(char_id)] = group
            for grade in (row.get("_growStar") or []):
                if grade:
                    index.setdefault(int(grade), group)
    except Exception:                       # noqa: BLE001 -- never break a clear
        index = {}
    _grade_group_index = index
    return index


def _char_group(char_id):
    """The canonical unit id shared by every grade and Bunrei form of a cast.

    Falls back to the charID itself when nothing knows it, so an unrecognised unit
    compares equal to itself rather than to everything else that is also unknown.
    """
    char_id = int(char_id)
    group = _grade_groups().get(char_id)
    if group:
        return group
    return int((dd.row("char", char_id) or {}).get("_group") or char_id)



# Opt-in action trace: `SEVENSINS_BATTLE_TRACE=1`. Off by default and one line per
# action when on. Every battle defect in this project so far has failed SILENTLY -- the
# server computes a reply, logs nothing wrong, and the client draws the wrong thing --
# so the cheapest thing that shortens the next one is seeing who cast what and which
# statuses each side is holding, on the turn it happens.
TRACE = os.environ.get("SEVENSINS_BATTLE_TRACE", "").strip() not in ("", "0", "false")


def _trace_names(unit):
    return ",".join(str(getattr(s, "name", "?")) for s in
                    getattr(unit, "statuses", [])) or "-"


def _trace_action(battle, attacker, target, skill_id):
    if not TRACE or attacker is None:
        return
    try:
        from engine import specs
        spec = specs.skill(skill_id) or {}
        print(f"[trace] r{getattr(battle, 'round', '?')} "
              f"{attacker.order}/char{getattr(attacker, 'char_id', '?')} "
              f"-> {getattr(target, 'order', '?')} "
              f"skill {skill_id} {spec.get('name') or '?'}", flush=True)
        for u in battle.units.values():
            if u.alive:
                print(f"[trace]    {u.order}/char{getattr(u, 'char_id', '?'):<6} "
                      f"hp={u.hp}/{u.max_hp} [{_trace_names(u)}]", flush=True)
    except Exception as exc:                              # noqa: BLE001
        print(f"[trace] failed: {exc!r}", flush=True)     # never break a battle



# --- initial status map (BattleDatas.status) ---------------------------------------
#
# Statuses reach the client on TWO channels, and we were only ever using one.
#
#   per-action  DamageInfo `status` rows, `[order, skill_id, round]`. BattleUnit.
#               UpdateStatus reads args[1] as the id and args[2] as the round, and a
#               round of **0 REMOVES** the status (removeStatusDataByID) rather than
#               applying it for zero turns.
#   battle open BattleDatas.status, `{order: {skill_id: args}}`, walked by
#               BattleDatas.RebuildAllStatus from BattleDataInitializer._InitUI. This
#               is the ONLY way a status that no attack applied can ever be drawn --
#               which is every battle-start passive, so Lucifer's The Divine, all the
#               Field/Commendation auras and the boss's opening seals were invisible.
#
# The two arg layouts DIFFER, which is the trap. StatusST has two constructors:
#   .ctor(List<int> curArgs)            [1]=skill [2]=round, and [3]=lv [4]=value
#                                       [5]=actOn only when Count >= 4
#   .ctor(int skillID, List<int> args)  [0]=round [1]=value [2]=actOn [5]=lv
# The second reads index 5 UNCONDITIONALLY, so a short list throws
# ArgumentOutOfRangeException inside battle load. Six entries is the minimum here.
ROUND_PERMANENT = -1
# UpdateStatusRound decrements only when `round >= 1` and removes at exactly 0, so a
# negative round is never counted down and never expires. That is the sentinel for
# "lasts the whole battle", not a large number.


def _status_wire_id(st):
    """-> the design row id the client keys this status by, or None if unsendable."""
    sid = getattr(st, "status_id", None)
    if sid is None:
        defn = getattr(st, "definition", None)          # legacy fx status
        sid = getattr(defn, "id", None) if defn is not None else None
    return _engine_status.wire_status_id(sid)


def _status_extras(st):
    """-> (lv, value, actOn) the client draws for one live Active.

    lv is the stack count (drawn on the icon when > 1), value the shield amount and
    actOn the shield marker the shield bar sums over -- see engine.wire._status_row
    for where each of those was read out of the client.
    """
    stacks = int(getattr(st, "stacks", 1) or 1)
    lv = stacks if stacks > 1 else 0
    if getattr(st, "kind", None) == "shield":
        return (lv, int(getattr(st, "shield_hp", 0) or 0), _engine_wire.ACT_ON_SHIELD)
    return (lv, 0, 0)


def _status_round(st):
    rem = getattr(st, "remaining", None)
    if rem is None:
        return ROUND_PERMANENT
    try:
        return max(1, int(rem))
    except (TypeError, ValueError):
        return ROUND_PERMANENT


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
        # Status changes that happened OUTSIDE an attack -- turn-start nested scripts.
        # They have no DamageInfo of their own to ride on, and `status` rows are
        # unit-addressed (`[order, id, round]` names its own unit), so they are queued
        # here and attached to the next payload that goes out. Deliberately not saved
        # with the battle: on reconnect the client rebuilds every unit's statuses from
        # BattleDatas.status, so a dropped queue costs nothing.
        self._pending_status_rows = []
        # Units killed by their OWN start-of-turn DoT, queued for the wire. Same
        # not-saved reasoning as the status rows above: a reconnect rebuilds the field
        # from BattleDatas, where the unit is already dead.
        self._pending_dot_deaths = []
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
        The projection is deduped down to one entry per unit, and that is safe for a
        reason worth writing down: the client does NOT read `line[1]` for its "Next"
        badge. `BattleUnitManager.GetNextAction` (0x197E100) re-runs the ATB itself from
        `LightBattleChar.Scv`/`SPD` -- refreshed each turn out of `sync` -- docking the
        head of `line` by a full gauge and putting it back in the running. So `line`
        supplies MEMBERSHIP plus whoever acts now; the lookahead is the client's own, and
        it predicts a fast unit lapping the field without our help. See
        docs/BATTLE_CLIENT_CONTRACT.md §3.5.

        Known divergence: on an exact tie in fill time the client keeps the earliest
        entry in its list (the actor having been re-inserted at the tail), i.e. it
        prefers the unit that did NOT just act, while `rank_key` below prefers the faster
        one. The badge then points at the wrong portrait for one turn.
        """
        alive = [u for u in self.units.values() if u.alive]
        if not alive:
            self.turn_order = []
            return
        # Ties (everyone opens at 0, so the whole field ties on the first roll) break
        # the way they always did: faster first, then the player team, then slot.
        def rank_key(u):
            return (-u.effective_spd(), u.team, u.index)
        # A Headwinded unit reports an infinite fill time, so the step is taken over
        # whoever can actually fill. If NOBODY can, the gauges simply do not advance --
        # better a stalled queue than inf arithmetic on every bar.
        fillable = [u for u in alive if not _engine_status.blocks_gauge_gain(u)]
        step = min((u.fill_time() for u in fillable), default=0.0)
        if step > 0 and step != float("inf"):
            for u in alive:
                u.fill_gauge(step)
            # Same rounding guard as the projection below: the leader must actually
            # read full, or the client draws a 99% bar on the unit taking its turn.
            lead = min(fillable, key=lambda u: (u.fill_time(), *rank_key(u)))
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
                # The PROJECTION has to honour Headwind too, or the client's lookahead
                # shows a unit that can never actually come round.
                movers = [u for u in alive
                          if not _engine_status.blocks_gauge_gain(u)]
                if not movers:
                    break
                gap = min((SCV_FULL - sim[u.order]) / u.effective_spd() for u in movers)
                for u in movers:
                    sim[u.order] = min(float(SCV_FULL),
                                       sim[u.order] + gap * u.effective_spd())
                # Whoever needed the least time IS full now; float rounding must not
                # be allowed to leave the step with nobody ready and the loop stuck.
                ready = [u for u in alive if sim[u.order] >= FULL_EPS]
                if not ready:
                    ready = [min(movers, key=lambda u: (SCV_FULL - sim[u.order])
                                 / u.effective_spd())]
            nxt = min(ready, key=lambda u: rank[u.order])
            sim[nxt.order] = 0.0
            if nxt.order not in queue:
                queue.append(nxt.order)
        self.turn_order = queue

    def _forced_target(self, attacker):
        """Taunt/Charm/Confuse override the ATTACKER's own target choice -- enforced
        here so it applies whether the target was auto-picked (enemy AI / auto-battle)
        or tapped by the player; a client can't route around its own status by picking
        someone else in the request. -> the forced unit, or None to leave the caller's
        target alone.

        THREE redirects, not one flag -- see engine/status.redirect. The registry states
        each exactly: Taunt "can only attack the taunt caster", Charm/Enchant "will
        attack allies", Confuse "will attack both allies and enemies". Charm and Confuse
        outrank Taunt: a unit can be both taunted and charmed, and losing control of your
        target beats being drawn to a specific one."""
        got = _engine_status.redirect(attacker)
        if not got:
            return None
        kind, source = got
        live = [u for u in self.units.values() if u.alive and u is not attacker]
        if kind == "taunt":
            src = self.units.get(source)
            return src if (src and src.alive) else None
        pool = [u for u in live if u.team == attacker.team] if kind == "allies" else live
        # Random, not first: "attacks allies" is a scramble, and always picking the same
        # slot makes a control effect look deterministic to the player.
        return random.choice(pool) if pool else None

    def _apply_gauge_cd(self, outcome):
        """Fold an effect outcome's charge-gauge and cooldown changes back onto the
        units. scv is the 0..100 ultimate gauge; cooldowns are per-skill-slot turns."""
        for g in outcome["gauge"]:
            u = g["unit"]
            u.scv = max(0, min(SCV_FULL, u.scv + int(SCV_FULL * g["pct"] / 100.0)))
        for c in outcome["cd"]:
            u = c["unit"]
            u.cooldowns = [max(0, cd + c["delta"]) for cd in u.cooldowns]

    def _apply_battle_start(self, units):
        """Fire the battle-start effects of each given unit's passive skills against the
        current field. Called when units ENTER the fight -- the whole roster at battle
        open, and each new wave's enemies as they spawn -- so team buffs, enemy debuffs
        and self-immunities (e.g. Leviathan's Jealousy Vortex) are in place before the
        first turn. Runs before the turn order is rolled so a SPD buff can reorder it."""
        field = list(self.units.values())
        # The engine's passives are a declarative rule table (engine/passives.py).
        if not hasattr(self, "_passives_fired"):
            self._passives_fired = set()
        for u in units:
            for sid in (u.skills or []):
                spec = _engine_specs.skill(sid) if sid else None
                if spec and spec.get("type") == "passive":
                    _engine_passives.fire(
                        _engine_passives.BATTLE_START, u, sid, field,
                        fired=self._passives_fired)

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
            "status": self.status_datas(),
            "backup_order": "",
            # sk_overwrite / mod_overwrite are OVERRIDES and must stay null (absent).
            # DesignRoleModelInfoRow.get_BattleModel checks only for non-null, so an
            # empty string still wins and every model resolves to "art/character/_01",
            # which 404s the bundle and kills the battle scene load.
            "custom_value": {},
            "team_skill": [],
        }, separators=(",", ":"))

    def _queue_expired(self, unit, expired):
        """Queue a round-0 removal row for every status that just fell off `unit`.

        For the expiries the client CANNOT see. It counts rounds down only for the unit
        that just acted (TurnEndState -> UpdateStatusRound on ActionOrderList[0]); a
        unit whose turn was SKIPPED, or whose gauge block aged on the battle's clock,
        never has a TurnEnd on the client, so its expired statuses stay drawn until a
        row says otherwise (status wire: 0 = remove). That is the frozen-boss icon from
        a real phone, 2026-08-26. The actor's own expiries deliberately do NOT come
        through here -- see end_turn.
        """
        self._queue_status_rows([
            {"target": unit.order, "status_id": st.status_id, "applied": False}
            for st in expired or []
            if getattr(st, "status_id", None) is not None])

    def _queue_status_rows(self, changes):
        """Queue out-of-band status changes, collapsing repeats.

        A marker re-grants the same status every turn it is held, so without collapsing
        the queue grows by a row per turn for as long as the fight runs and the client
        gets handed the same row many times over. Only the LATEST state of each
        (unit, status) matters -- the rows carry absolute state, not a delta.
        """
        for ch in changes or []:
            key = (ch.get("target"), ch.get("status_id"))
            self._pending_status_rows = [
                q for q in self._pending_status_rows
                if (q.get("target"), q.get("status_id")) != key]
            self._pending_status_rows.append(ch)

    def _drain_pending_status(self, combo):
        """Attach queued out-of-band status changes to an outgoing attack."""
        rows = self._pending_status_rows
        if not rows:
            return
        groups = (combo or {}).get("data") or []
        if not groups or not groups[0]:
            return
        lead = groups[0][0]
        for ch in rows:
            sid = _engine_status.wire_status_id(ch.get("status_id"))
            if sid is None:
                continue
            rounds = 0 if not ch.get("applied") else (
                ROUND_PERMANENT if ch.get("duration") is None
                else max(1, int(ch["duration"])))
            # Six elements, same layout as the engine's rows -- see wire._status_row.
            # Out-of-band changes come from marker scripts and expiries; the stack
            # count and shield amount are read off the unit's live Active, if any.
            lead.setdefault("status", []).append(
                [ch["target"], sid, rounds, *self._status_row_extras(ch, rounds)])
        self._pending_status_rows = []

    def _status_row_extras(self, ch, rounds):
        """-> (lv, value, actOn) for an out-of-band status row. Zeros on removal."""
        if rounds == 0:
            return (0, 0, 0)
        unit = self.units.get(ch.get("target"))
        for st in getattr(unit, "statuses", []) if unit else []:
            if getattr(st, "status_id", None) == ch.get("status_id"):
                return _status_extras(st)
        return (0, 0, 0)

    def status_datas(self):
        """BattleDatas.status -- every unit's CURRENT statuses, so the client can draw
        what is already on the field when the battle scene opens. See the notes on
        ROUND_PERMANENT for the (different) arg layout this channel uses.
        """
        out = {}
        for order, unit in self.units.items():
            rows = {}
            for st in getattr(unit, "statuses", []):
                sid = _status_wire_id(st)
                if sid is None:
                    continue
                # [round, value, actOn, _, _, lv] -- index 5 is read unconditionally.
                # The battle-open layout is [round, value, actOn, ?, ?, lv] -- NOT the
                # per-action order. StatusST's second constructor reads [0], [1], [2]
                # and [5]; six entries is the floor or it throws inside battle load.
                # value/actOn carry the shield amount so a resumed fight draws the
                # shield bar; lv the stack count so "x5" survives a reconnect.
                lv, value, act_on = _status_extras(st)
                rows[str(sid)] = [_status_round(st), value, act_on, 0, 0, lv]
            if rows:
                out[order] = rows
        return out

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

    def dot_death_cmds_json(self):
        """-> a BattleCmd per unit killed by its own start-of-turn DoT, and drains.

        Shaped exactly like an attack (cmd 1201) because that is the only message the
        client renders damage and death from: `die` is set nowhere else on the wire.
        The dying unit is its own caster and its own target, with skill 0 -- the same
        shape attack_cmd_json already falls back to when a skill has no runnable spec,
        so it is a form the client is known to accept.

        DEATHS ONLY, deliberately. A non-lethal tick still moves HP silently through
        `sync`; sending a 1201 for it would put an extra Perform in front of a unit that
        has not acted yet, and whether the client's turn machine tolerates that has not
        been tested on a device. The lock is the death case, so that is what this fixes.
        """
        deaths, self._pending_dot_deaths = self._pending_dot_deaths, []
        out = []
        for d in deaths:
            unit = self.units.get(d["order"])
            if unit is None:
                continue
            cmd = json.loads(self.battle_cmd_json(
                cur_team=unit.team if unit else TEAM_PLAYER))
            cmd["combo"] = [{
                "caster": d["order"], "skill": 0,
                "data": [[{"c": d["order"], "md": 1, "cg": 0, "dmg": int(d["dmg"]),
                           "cri": 0, "die": 1, "status": [], "extra": [],
                           "picons": [], "pskill_id": 0}]],
            }]
            out.append(json.dumps(cmd, separators=(",", ":")))
        return out

    def spend_skill(self, attacker_order, slot):
        """Put the used skill on cooldown. Nothing else is spent -- the special move's
        `_charge` is an opening delay, not a per-use cost (see Unit.ultimate_charge)."""
        unit = self.units.get(attacker_order)
        if not unit:
            return
        unit.use_skill(slot)

    def attack_cmd_json(self, attacker_order, defender_order, skill_id, rng=None):
        """BattleCmd for cmd 1201, carrying the actual attack in `combo`.

        AttackJsonData is built through .ctor(string caster, int skill), so those two
        ctor parameter names are its JSON keys; `data` is List<List<DamageInfo>> --
        outer list per hit, inner per target.

        `rng` is threaded purely so the payload the CLIENT receives is reproducible from
        a seed. The server never passes it (crit and variance should be live), but a
        fuzzer that cannot replay a failing fight can only report that one existed --
        and this is the function that builds the payload, so it is the one that has to
        be replayable. See tools/battle_fuzz.py.
        """
        attacker = self.units.get(attacker_order)
        target = self.units.get(defender_order)
        if attacker and target:
            target = self._forced_target(attacker) or target

        from engine import bridge
        combo = bridge.attack_combo(
            self, attacker_order,
            target.order if target else defender_order, skill_id, rng=rng)
        if combo is None:
            # The engine has no runnable spec for this skill. 15 rows corpus-wide reach
            # here and they are boss PHASE SCRIPTS -- `即死`, "HP Changed to 50%",
            # "boss轉階段" -- not skills anyone casts. A zero-damage entry is the honest
            # answer: the client needs a combo to drive the animation and yield the turn
            # (an empty one is not valid), and inventing a basic attack for a script
            # whose effect we cannot read would be worse than doing nothing.
            who = (target or attacker)
            combo = {"caster": attacker_order, "skill": int(skill_id or 0),
                     "data": [[{"c": who.order if who else attacker_order, "md": 1,
                                "cg": 0, "dmg": 0, "cri": 0, "die": 0,
                                "status": [], "extra": [], "picons": [],
                                "pskill_id": 0}]]}
        self._drain_pending_status(combo)
        _trace_action(self, attacker, target, skill_id)
        cmd = json.loads(self.battle_cmd_json(
            cur_team=attacker.team if attacker else TEAM_PLAYER))
        cmd["combo"] = [combo]
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
        return max(1, int(raw * (1.0 - defend_ratio(target.defence))))

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
        sealed_set = _engine_status.sealed_slots(unit) if unit else set()
        seal_skill = 1 in sealed_set          # Power Attack Seal / Skill Seal
        seal_ult = 2 in sealed_set            # Special Move Seal / Skill Seal
        # Button 3 is the ultimate, and it is gated by CHARGE as well as by cooldown.
        # The button has exactly one number -- UISkillBtn.LeftCD, drawn from
        # SkillList[i][1] and only when it is > 0 (PanelBattle._setBtnState 0x176B6BC)
        # -- so publish "turns until this lights up": the larger of the reload and the
        # charge still to bank. Without this a charge-gated ultimate is a dark button
        # with no number and no explanation, because the blue bar is the move gauge now
        # and no longer doubles as the charge readout.
        charge_left = max(0, unit.ultimate_charge() - unit.charge) if unit else 0
        cd[1] = max(cd[1], charge_left)
        ult_locked = seal_ult or bool(cd[1])
        return [0, 1 if (seal_skill or cd[0]) else 0, 1 if ult_locked else 0,
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
            # ...and only THEN does an "After the action, the caster's Move Gauge will
            # increase 25%" effect land. Applying it during the attack is pointless: the
            # caster is at a full bar when it acts, so the increase clamps to 100 and is
            # then wiped by the line above. Carried as a pending delta so it survives.
            pending = getattr(acted, "pending_scv", 0.0)
            if pending > 0 and _engine_status.blocks_gauge_gain(acted):
                acted.pending_scv = 0.0        # Headwind refuses the head start too
                pending = 0.0
            if pending:
                acted.scv = max(0.0, min(float(SCV_FULL), acted.scv + pending))
                acted.pending_scv = 0.0
            # After-action passives fire before the duration tick, so an effect the
            # actor's own turn produces is not immediately aged by it.
            _engine_passives.fire_all(
                _engine_passives.AFTER_ACTION, [acted], list(self.units.values()),
                fired=getattr(self, "_passives_fired", None))
            # A resolved turn spends a turn of the actor's own statuses. NO removal
            # row for these, on purpose: the client decrements the ACTOR's statuses
            # itself in TurnEndState.OnEnter (BattleUnit.UpdateStatusRound on
            # PlayerBattle.GetFirst(), i.e. ActionOrderList[0]) and removes what
            # reaches 0 -- and removeStatusDataByID on an id it no longer holds
            # answers with ServerRPCReportError. The two paths below are the ones the
            # client cannot see, because their holder never has a TurnEnd.
            _engine_status.tick_duration(acted)
        self._age_gauge_blocks()
        self._roll_turn_order()
        self.round += 1
        self.turn_open = False
        self._start_of_turn()

    def _age_gauge_blocks(self):
        """Spend a turn off a gauge block even though its holder never gets a turn.

        Every other status ages on its holder's own turn, and `tick_duration` says why
        that is right even for a skipped one: "a skipped turn still counts against a
        duration or a stun would never wear off." A stun still lets the bar fill, so the
        unit reaches the front of the queue and its turn is skipped there.

        Headwind does not. The bar never fills, so the unit never reaches the front, so
        nothing ever ticks -- and a two-turn gauge block removes its holder from the
        fight permanently. Beelzebub found this on a real device: her passive put a
        Headwind on herself and she took zero turns in 62, which reads as a dead unit
        rather than a debuff.

        So a gauge block is aged on the BATTLE's clock instead of its holder's, which is
        the only clock it leaves running. Only the blocking statuses age here; the rest
        of the unit's statuses keep their own-turn timing, since those are not what is
        denying the turns.
        """
        for unit in self.units.values():
            if not unit.alive or not _engine_status.blocks_gauge_gain(unit):
                continue
            for st in list(unit.statuses):
                if not isinstance(st, _engine_status.Active) or st.permanent:
                    continue
                if not _engine_status.blocks_gauge_gain_status(st):
                    continue
                st.remaining = int(st.remaining) - 1
                if st.remaining <= 0:
                    unit.statuses.remove(st)
                    self._queue_expired(unit, [st])

    def _start_of_turn(self, _depth=0):
        """DoT/HoT ticks for whoever is now at the front of the queue, then skip its
        turn outright if it's immobilized (Stun/Freeze/Daze/...) -- recurses (bounded by
        unit count) so a fully-crowd-controlled lineup still resolves instead of
        hanging. A tick that kills the unit re-prunes the order before recursing."""
        unit = self.acting_unit()
        if not unit or _depth > len(self.units):
            return
        # DAMAGE only. The duration is spent after the can-it-act decision below -- see
        # the note in engine/status.py: ticking both here made a 1-turn stun expire on
        # the very tick that should have skipped the turn.
        dot, hot = _engine_status.tick_damage(unit)
        # What the client is told below is the HP actually removed, not the raw tick:
        # a DoT far bigger than the remaining pool would otherwise render as a damage
        # number several times the unit's max HP.
        dealt = min(int(dot), int(unit.hp)) if dot else 0
        if dot:
            unit.hp = max(0, unit.hp - dot)
        if hot:
            unit.hp = min(unit.max_hp, unit.hp + hot)
        if not unit.alive:
            # A DoT that KILLS has to reach the client as an event, not just as a
            # smaller number in the next `sync`. The client runs its own action order
            # (BattleUnitManager.GetNextAction re-derives it from sync Scv/SPD), so a
            # unit that dies here is still sitting in its ActionOrderList waiting for a
            # turn the server will never hand out -- reported from a phone as a hard
            # soft-lock when a low-HP unit came up with lethal poison on it. Retail
            # played the tick on the dying unit's own turn: damage number, pain sound,
            # death, and only then the next character. Queue it so the reply that
            # follows this turn carries that beat; see dot_death_cmds_json.
            self._pending_dot_deaths.append({"order": unit.order, "dmg": dealt})
            self._roll_turn_order()
            self._start_of_turn(_depth + 1)
            return
        # Turn-start passives fire BEFORE the can-it-act decision: "at the start of
        # the turn, if you have a Commendation, you gain CC Immunity" has to be able
        # to stop the very stun being checked for.
        _engine_passives.fire_all(
            _engine_passives.TURN_START, [unit], list(self.units.values()),
            fired=getattr(self, "_passives_fired", None))
        # A status can be a trigger marker that grants further statuses while held --
        # see engine/status.py. Runs after the passives so a marker applied THIS turn
        # start does not also fire in the same tick; it fires next turn, once the
        # unit is actually holding it.
        #
        # EVERY living unit, not just the one acting. Durations tick once per global
        # turn but a unit only acts once every N turns, so refreshing a marker's
        # grants on the holder's own turn alone left them lapsing in between --
        # Lucifer's Keen and Teardown flickered on and off every other turn with two
        # units on the field, and would have been up for 2 turns in 6 with a full
        # party. "When affected by this status, X will trigger Y" is a continuous
        # consequence of holding the marker, not a once-per-own-turn event.
        for u in self.units.values():
            if u.alive:
                self._queue_status_rows(_engine_status.run_nested(u))
        if _engine_status.is_immobilized(unit):
            unit.tick_cooldowns()
            # The skipped turn still spends a turn of every status. This is THE path a
            # control status expires on -- the holder cannot act, so its only turns are
            # skipped ones -- which is why an unsent removal showed up as a permanently
            # frozen boss rather than as some lesser stale icon.
            self._queue_expired(unit, _engine_status.tick_duration(unit))
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
            elif kind == RATING_LEAD_CHAR:
                # Composition, not performance -- judged over the party as FIELDED,
                # dead members included, exactly like the job conditions below.
                # Bringing the named cast and losing it is still bringing it.
                want = _char_group(limit)
                ok = cleared and any(_char_group(u.char_id) == want
                                     for u in self.units.values()
                                     if u.team == TEAM_PLAYER)
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

        **The COUNT is right; the CONTENTS are observed where we have footage and
        DERIVED everywhere else.** Two clears agree that there is one coin drop per
        wave -- 1-1 has three waves and shows three icons, 2-1 has two and shows two --
        and that is what the coin part reproduces.

        Real per-stage drop tables were live-ops data and are in no client file, so a
        stage without footage cannot be reproduced, only derived. STAGE_DROPS holds what
        has actually been seen and always wins; everything else is generated from real
        columns on the stage row -- see the generated drop tables block. 1-1 pays three
        250 coin stacks but 2-1 pays an emblem x10 plus a character card, which is why
        "250 Mira per wave" was overfitted to the tutorial and is no longer the answer
        for every stage.
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
        # THE MOVE GAUGE RESTARTS WITH THE WAVE, for both sides.
        #
        # A wave's enemies are freshly built `Unit`s and so began at zero already; the
        # party carried whatever it had banked when the last enemy fell. That is not
        # symmetric and it is not what the game does -- a survivor sitting at 90% opened
        # the new wave with a free turn before anything on the other side could move, and
        # `next_wave_strargs` sends `sync()` (which carries scv), so the client drew the
        # carried bar rather than a reset one.
        #
        # Cooldowns and ultimate charge deliberately do NOT reset: carrying those forward
        # is the whole point of a multi-wave stage. Only the gauge restarts, exactly as it
        # does at battle open, where every unit starts at zero and the queue is decided by
        # SPD alone.
        for unit in self.units.values():
            unit.scv = 0.0
            unit.pending_scv = 0.0
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

        The choice itself lives in `battle_ai` (tier 1: simulate every legal move and
        score what it did). The greedy chooser below is kept only as the escape hatch --
        `SEVENSINS_BATTLE_AI=old` -- and goes when the old engine does, since its
        `skill_ratio` ranking cannot see basis, swings, breadth, or what a skill DOES.
        """
        if TIER1_AI:
            move = battle_ai.choose(self, target_team)
            if move:
                return move
        return self._auto_move_greedy(target_team)

    def _auto_move_greedy(self, target_team):
        """The pre-tier-1 chooser: strongest prose-ratio skill, first live target."""
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
        # The seals are per-SLOT, not all-or-nothing: Power Attack Seal locks slot 1,
        # Special Move Seal locks the ultimate, Skill Seal locks both. The old flag
        # collapsed all three into "basic only" -- and on the new path it never fired at
        # all, because it reads `st.definition`, which an engine status does not have.
        sealed = _engine_status.sealed_slots(unit)
        slots = []
        for i in range(min(len(unit.skills), ULTIMATE_SLOT + 1)):
            if i in sealed:
                continue
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

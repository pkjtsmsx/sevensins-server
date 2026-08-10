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

# SCV charge gauge (the blue bar): UICharStatus.SyncBar draws scv/100, so 100 is full.
# The ultimate (skill slot 3) is gated on a full gauge.
SCV_FULL, SCV_PER_TURN, ULTIMATE_SLOT = 100, 25, 2


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


def stage_drop_preview(stage_id):
    """Item ids for the stage's "Drop Info" panel (StageRpc GetDrops 8 -> 25).

    There is NO drop table in the client data -- not in any of the pack's 55 forms, not
    on the stage row (`_itemrank_str` is a rank category: it is '21,11' on every
    main-story stage), not on mob_group. That is not an oversight: the client ASKS the
    server for this list, which it would not do if the table shipped locally. Real
    per-stage drop tables were live-ops data.

    So the contents are ours to choose, and the only honest choice is to preview exactly
    what a clear actually pays -- see Battle.drops(), one coin stack per wave. When drops
    become per-stage, this and drops() must change together or the panel starts lying.
    """
    known = STAGE_DROPS.get(int(stage_id))
    if known is not None:
        return [d.display_item if isinstance(d, RuneDrop) else d[0] for d in known]
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
                 book_bonus=None, uid="", skill_limit=0):
        self.order, self.char_id, self.team, self.index = order, char_id, team, index
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
        self.max_hp = stats["hp"] + bonus.get("hp", 0)
        self.hp = self.max_hp
        self.atk = stats["atk"] + bonus.get("atk", 0)
        self.defense = stats["def"] + bonus.get("def", 0)
        self.spd = stats["spd"]
        # The blue bar under each character's HP: UICharStatus draws it as scv/100,
        # so it is a 0..100 charge gauge that gates the ultimate. Publishing a flat 0
        # left it permanently empty. Fill rate is a RECONSTRUCTION -- the real one
        # lived on the server -- chosen so it charges over a few turns.
        self.scv = 0
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
        self.cooldowns = [max(0, c - 1) for c in self.cooldowns]
        self.scv = min(SCV_FULL, self.scv + SCV_PER_TURN)

    def ultimate_ready(self):
        return self.scv >= SCV_FULL

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
            "scv": self.scv, "spd": self.spd, "lv": self.lv, "atk": self.atk,
            "star": self.star, "plus": 0, "be1": 0, "be2": 0,
        }

    def _attributes(self, current):
        """BattleAttributeData. Keys from the 2.2.4 JsonProperty thunks
        (tools/json_keys.py --class BattleAttributeData). Only the four stats we
        actually model vary; the rest are real fields the client will read, so send
        them as zeros rather than omitting them."""
        return {
            "hp": self.hp if current else self.max_hp,
            "atk": self.atk, "def": self.defense, "spd": self.spd,
            "scv": self.scv if current else SCV_FULL,
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
        return [self.max_hp, self.hp, self.scv, self.spd]


class Battle:
    """One run of one stage: a list of waves, each a mob group from the design data."""

    def __init__(self, stage_id, team_char_ids, team_level=1, team_star=None,
                 team_super_star=0, book_rank=0):
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
                + (entry.get("limit_char", 0) or 0))
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
        """Fastest first. The client only displays this order (and GetFirst takes
        entry 0 as the acting unit), so the server stays authoritative."""
        # Pure speed order, which is the real rule. At their proper star tier the
        # party outruns this stage's mobs anyway (Leviathan 1170 vs 992/974), so the
        # tutorial naturally opens with her turn.
        alive = [u for u in self.units.values() if u.alive]
        alive.sort(key=lambda u: (-u.spd, u.team, u.index))
        self.turn_order = [u.order for u in alive]

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
        """Put the used skill on cooldown, and drain the gauge for an ultimate."""
        unit = self.units.get(attacker_order)
        if not unit:
            return
        unit.use_skill(slot)
        if slot == ULTIMATE_SLOT:
            unit.scv = 0

    def attack_cmd_json(self, attacker_order, defender_order, skill_id):
        """BattleCmd for cmd 1201, carrying the actual attack in `combo`.

        AttackJsonData is built through .ctor(string caster, int skill), so those two
        ctor parameter names are its JSON keys; `data` is List<List<DamageInfo>> --
        outer list per hit, inner per target.
        """
        attacker = self.units.get(attacker_order)
        target = self.units.get(defender_order)

        # DamageInfo shape (from HandleAttack): a hit is mode 1 with a NEGATIVE amount --
        # IsDamage is `Mode == 1 && Damage < 0`, HasHP is `Mode in (1,2) && Damage != 0`.
        def dmg_info(u, amount):
            return {"c": u.order, "md": 1, "cg": 0, "dmg": -amount, "cri": 0,
                    "die": 1 if not u.alive else 0,
                    # DamageInfo.status wire format is not yet reversed; leave it empty so
                    # the engine's status MECHANICS apply server-side (they change future
                    # damage) without risking the client on a malformed field.
                    "status": [], "extra": [], "picons": [], "pskill_id": 0}

        rows = []
        if attacker and target and fx.is_complete(skill_id):
            # Trusted skill -> full effect engine: correct per-hit coefficients, real
            # targeting (a debuff can land on a different unit than the damage), and
            # server-side buff/debuff tracking that feeds back into damage.
            allies = [u for u in self.units.values() if u.team == attacker.team]
            enemies = [u for u in self.units.values() if u.team != attacker.team]
            outcome = fx.execute_skill(
                attacker, target, allies, enemies, skill_id,
                damage_reduce=lambda u: defend_ratio(
                    u.defense * fx.stat_multiplier(u.statuses, "DEF")
                    + fx.flat_bonus(u.statuses, "DEF")))
            for h in outcome["hits"]:
                if h["damage"] > 0:
                    rows.append(dmg_info(h["target"], h["damage"]))
                    self.damage_sum += h["damage"]
                    attacker.dmg_done += h["damage"]
                    h["target"].dmg_taken += h["damage"]
        elif attacker and target:
            # Fallback: the original single-hit simple-damage path.
            damage = self.damage(attacker, target, skill_id)
            target.hp = max(0, target.hp - damage)
            self.damage_sum += damage
            attacker.dmg_done += damage
            target.dmg_taken += damage
            rows.append(dmg_info(target, damage))
        if not rows and target:
            rows.append(dmg_info(target, 0))    # never send an empty combo

        cmd = json.loads(self.battle_cmd_json(
            cur_team=attacker.team if attacker else TEAM_PLAYER))
        cmd["combo"] = [{
            "caster": attacker_order, "skill": skill_id, "pskill_id": 0,
            "data": [rows],
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
        # button 3 is the ultimate: locked while on cooldown OR the gauge is short
        ult_locked = bool(cd[1]) or not (unit and unit.ultimate_ready())
        return [0, 1 if cd[0] else 0, 1 if ult_locked else 0, cd[0], cd[1], cd[2]]

    def end_turn(self):
        """Rotate the acting unit to the back and drop anyone who died."""
        acted = self.acting_unit()
        if acted:
            acted.tick_cooldowns()
            # Count down this unit's statuses on its own turn; drop the expired.
            if acted.statuses:
                acted.statuses = [s for s in acted.statuses if not s.tick()]
        if self.turn_order:
            self.turn_order.append(self.turn_order.pop(0))
        self.turn_order = [o for o in self.turn_order
                           if self.units.get(o) and self.units[o].alive]
        if not self.turn_order:
            self._roll_turn_order()
        self.round += 1
        self.turn_open = False

    def team_alive(self, team):
        return any(u.alive for u in self.units.values() if u.team == team)

    def avg(self, avg_list, wave=None):
        """The AVG id for a wave (1-based) out of one of the stage's CSV lists.
        Missing entries mean "no cutscene", which is a legitimate 0."""
        idx = (self.wave if wave is None else wave) - 1
        return avg_list[idx] if 0 <= idx < len(avg_list) else 0

    # -- clear rewards ----------------------------------------------------
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
        and other-mode types 4..22) report 0 rather than a false positive."""
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
            else:
                ok = False
            flags.append(1 if ok else 0)
        return flags

    def rating_rewards(self):
        """[(item_id, count), ...] for the conditions this run newly satisfied."""
        return [(row[1], row[2])
                for row, ok in zip(self.rating_rows(), self.rating_flags())
                if ok and len(row) > 2]

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
        known = STAGE_DROPS.get(self.stage_id)
        if known is not None:
            return list(known)
        return [(COIN_ITEM_ID, COIN_PER_WAVE)] * self.wave_max

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
        ultimate slot only with a full gauge. The basic is always available."""
        slots = []
        for i in range(min(len(unit.skills), ULTIMATE_SLOT + 1)):
            if unit.cooldowns[i] > 0:
                continue
            if i == ULTIMATE_SLOT and not unit.ultimate_ready():
                continue
            slots.append(i)
        return slots or [0]

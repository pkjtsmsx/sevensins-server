"""Guild Weekly -- the `PlayerChallenge` subsystem.

Despite the enum name (`GuildRpcServerCmd`/`ChallengeRpcServerCmd` are separate), the
Guild Weekly boss is NOT part of Guild at all: it is its own subsystem with three
requests (sync 272, fight 528, rank_guild 544). Guild only gates the door -- the
week-field button lives on PanelGuild and `PanelGuildWeekly.OnKickedGuild` throws the
player out if they leave.

**The content already ships.** Book 8 of the stage form holds 28 stages,
1000001..1000037: seven bosses named for the Seven Virtues (the mirror of the Seven
Sins the game is named after) x four difficulties. `_sort` 1..7 is the boss, and
`_difficulty` 1..4 lines up exactly with `PanelGuildWeekly.Difficulty`
(Easy/Normal/Hard/Nightmare). Every mob group they name exists. Entry is free of
stamina (`_ap 0`) because the real cost is a Weekly Guild Pass (item 22), of which
`PanelGuildWeekly.ConstantDefine.MaxChallengeTimes` allows three a day.

**How a fight actually starts** -- worth writing down, because none of it is obvious
from the command names:

  1. `OnEnterGuildWeeklyHome` sends sync (272); we answer 784.
  2. The player picks a difficulty and taps Challenge. `OnChallengeClick` resolves the
     stage id ENTIRELY CLIENT-SIDE:
         stageId = StagesDic[ WeekdayGroup[ StageSyncData.weekday - 1 ] ][ difficulty-1 ]
     then opens the ordinary `PanelBattlePreparation` via
     `PlayerStage.OpenPanelHelper(stageId, 8, EnterGuildWeekly)`.
  3. Confirming runs `EnterGuildWeekly`, which sends fight (528) with
     **intargs = [use_bc, difficulty]** -- and no stage id, because the server is
     expected to derive the same stage from the same two facts.
  4. Nothing in ChallengeRpcClientCmd starts a battle. The panel is the normal
     pre-battle panel, so it is the normal pair that moves it: the stage
     EXECUTE_SUCCESS reply followed by PlayerBattle's server-start handoff.
  5. When the fight ends, 786 carries [damage, bonus, total_damage] -- the three
     labels on PanelBattleResult's Guild page.

Because step 2 is client-side, `StageSyncData.weekday` has to be right or the Challenge
button throws before a single byte is sent. See player_state.roster.stage_json.
"""

import json, time

from .core import (
    FORMATION_CHALLENGE_BASE,
    _daily_period,
    grant_reward,
    item_count,
    spend_item,
)

import battle as bt

__all__ = [
    "CHALLENGE_PASS_ITEM", "CHALLENGE_MAX_TIMES", "CHALLENGE_BOOK",
    "CHALLENGE_DIFFICULTIES", "challenge_weekday", "challenge_key",
    "challenge_stage_id", "is_challenge_stage", "challenge_formation_index",
    "challenge_stages_json", "challenge_sync_intargs",
    "challenge_reset_seconds", "start_challenge", "finish_challenge",
    "challenge_rank_json", "have_challenge_pass",
    "settle_day", "take_pending_settlement", "CHALLENGE_RPLY_REWARD_GET",
]

# The Guild Weekly stages live in book 8 and nothing else does.
CHALLENGE_BOOK = 8
CHALLENGE_DIFFICULTIES = 4          # PanelGuildWeekly.Difficulty Easy..Nightmare
CHALLENGE_WEEKDAYS = 7
# item 22, "Weekly Guild Pass" -- `_lbHomeChallengeTimes` is literally
# format(text 17001, backpack.GetItemCount(22), MaxChallengeTimes).
CHALLENGE_PASS_ITEM = 22
CHALLENGE_MAX_TIMES = 3             # PanelGuildWeekly.ConstantDefine.MaxChallengeTimes
# **`TodayScores` is one entry per TRY, not per difficulty.** Live footage of the Guild
# Raid panel shows "My Record: 1st Try / 2nd Try / 3rd Try" over a "Daily Tryouts: 2/3"
# counter, and `_lbHomePersonalPartialScore` is the List<UILabel> those three rows are.
# An earlier version sent four zeros keyed to the four difficulties, which put the wrong
# number under each label (and a fourth nobody reads).
CHALLENGE_TRIES_SHOWN = CHALLENGE_MAX_TIMES

# DesignChallengeForm.OnParsed buckets challenge_reward rows by `_rewardType`
# (CMPs at 0x19c5190/98/a0) into three dictionaries keyed by int.Parse(_group), and
# `_group` IS the weekday:
#   3 -> PersonalDailyRewards (0x40)   what one player earns for a day's damage
#   5 -> GuildDailyRewards    (0x48)   what the guild earns, thresholds ~19x higher
#   6 -> PersonalBestRewards  (0x50)
# The client reads all three straight out of the design pack for its reward tabs, so
# these constants only have to agree with it for the PAYOUT, which is ours to make.
REWARD_PERSONAL_DAILY = 3
REWARD_GUILD_DAILY = 5
REWARD_PERSONAL_BEST = 6

# ChallengeRpcClientCmd.reward_get_reply -- the end-of-day settlement announcement.
CHALLENGE_RPLY_REWARD_GET = 785


def challenge_weekday(now=None):
    """1..7, Monday = 1. Must match player_state.roster.stage_json's `weekday`, which
    is what the client indexes WeekdayGroup with."""
    t = time.localtime(now if now is not None else time.time())
    return t.tm_wday + 1


def challenge_key(now=None):
    """`ChallengeStages.curr_key`, shown verbatim on the panel as the week's name and
    used by SyncChallengeDataReply to decide whether to clear the cached guild
    ranking (it clears when the key CHANGES, so this must be stable within a week)."""
    t = time.localtime(now if now is not None else time.time())
    iso = time.strftime("%G-W%V", t)
    return iso


def _challenge_stage_rows():
    """{(sort, difficulty): stage id} for every book-8 stage."""
    out = {}
    for sid, row in (bt.dd.rows("stage") or {}).items():
        if row.get("_book") != CHALLENGE_BOOK:
            continue
        out[(int(row.get("_sort") or 0), int(row.get("_difficulty") or 0))] = int(sid)
    return out


_stages_cache = None


def _stages():
    global _stages_cache
    if _stages_cache is None:
        _stages_cache = _challenge_stage_rows()
    return _stages_cache


def is_challenge_stage(stage_id):
    """Whether a stage id is one of the 28 Guild Weekly bosses. Read off `_book`
    rather than an id range: 1000001..1000037 happens to be contiguous today, but the
    book is what the design data actually keys the family on."""
    row = bt.dd.row("stage", stage_id) or {}
    return row.get("_book") == CHALLENGE_BOOK


def challenge_stage_id(weekday, difficulty):
    """The stage the client will have resolved for (weekday, difficulty) -- we have to
    reach the same answer from the same two numbers, because the fight request carries
    the difficulty and nothing else."""
    return _stages().get((int(weekday), int(difficulty)))


def challenge_formation_index(now=None):
    """The saved team the Guild Weekly fields today.

    `PanelBattlePreparation.InitTeamIndex` case 8 sets both _nowTeamListIndex and
    _maxTeamListIndex to `weekday + 9`, so the raid has a DEDICATED team per weekday
    boss and the Edit button on the Preparation panel edits that one.

    The server has to reach the same number independently: unlike an ordinary stage,
    the fight request (528) carries only [use_bc, difficulty] -- no team index. Reading
    `battle_team_index` instead, as this used to, fielded whatever team the last
    CAMPAIGN stage happened to use, so editing the raid team changed the panel and
    nothing else."""
    return challenge_weekday(now) + FORMATION_CHALLENGE_BASE


def challenge_stages_json(state, now=None):
    """ChallengeStages, strargs[0] of the sync reply (784).

    Wire keys off the [JsonProperty] thunks: **curr_key / weekday / stages / scores**.

    `StagesDic` is `"stages"`, NOT `"g"`. Getting that wrong is what made the Guild
    Raid panel open blank for a day: `SyncChallengeDataReply` PRE-ALLOCATES an empty
    ChallengeStages and lets Newtonsoft populate it, and ChallengeStages has no ctor
    defaults -- so a key we spell wrong leaves that field **null** rather than empty,
    and `InitHome` dereferences StagesDic with no null check. The event still
    dispatches and nothing logs, so it presents as a silent NullReferenceException
    inside a panel that otherwise looks fine.

    The wrong key came from an extraction helper that reads the first string ref in the
    attribute thunk. For a field carrying BOTH [JsonProperty] and [JsonConverter] that
    can land on the converter instead of the property name -- which is why every such
    field must be read from the decompiled thunk itself. `stages` was the only one
    mis-read here; curr_key/weekday/scores were confirmed correct the same way.

    `weekday` (WeekdayGroup) must have at least `StageSyncData.weekday` entries and
    each `g` list at least CHALLENGE_DIFFICULTIES, or OnChallengeClick and InitHome
    index past the end. `scores` (TodayScores) is the one that may be short --
    InitHome renders "-" for the slots beyond it rather than throwing.
    """
    stages = _stages()
    groups = {}
    for day in range(1, CHALLENGE_WEEKDAYS + 1):
        ids = [stages.get((day, d)) for d in range(1, CHALLENGE_DIFFICULTIES + 1)]
        # A missing difficulty would shift every later one; drop the whole day rather
        # than hand the client a list that resolves to the wrong boss.
        if any(i is None for i in ids):
            continue
        groups[str(day)] = ids
    ch = _challenge(state, now)
    return json.dumps({
        "curr_key": ch["key"],
        # Boss N on day N. The design data agrees: `_sort` runs 1..7 across the seven
        # virtue bosses and challenge_reward's `_group` runs 1..7 to match.
        "weekday": list(range(1, CHALLENGE_WEEKDAYS + 1)),
        "stages": groups,
        # One score per try taken today, in order -- the panel prints them as
        # 1st/2nd/3rd Try and renders "-" for any slot past the end of this list.
        "scores": list(ch.get("today") or [])[:CHALLENGE_TRIES_SHOWN],
    }, separators=(",", ":"))


def _bracket_rewards(reward_type, group, score):
    """[(item_id, count), ...] for EVERY bracket `score` reaches in one table.

    The whole ladder up to the score, not just the top rung: the tables are cumulative
    ladders and the settlement pays all of them at once.
    """
    rows = _reward_rows(reward_type, group)
    out = []
    for row in rows[:_bracket_index(rows, int(score)) + 1]:
        for iid, cnt in zip(row.get("_idList") or [], row.get("_countList") or []):
            if iid and cnt:
                out.append((int(iid), int(cnt)))
    return out


def settle_day(state, ch):
    """Pay out a finished day and stash the announcement for reply 785.

    **Both ladders are END-OF-DAY settlements**, which is what reply 785
    (`reward_get_reply`, server-initiated, no request) exists for. Decompiled, it takes
    `intargs.Count == 2` and `strargs.Count == 2` and shows PanelItemMsg's item-list
    popup TWICE:

        intargs[0] -> text 2510 "Previous personal score: {0}"  + strargs[0]'s items
        intargs[1] -> text 2512 "Previous Guild Score: {0}"     + strargs[1]'s items

    Each strarg is a `Dictionary<int, List<RewardItem>>` (LuaTableConverter), and
    RewardItem's wire keys are plain `id` / `count`. The client flattens every value
    into one list per popup, so the dictionary key is only a grouping -- we key by
    bracket index.

    For a guild of one the guild score IS this member's best, which is already what
    `challenge_sync_intargs` reports as the guild total, so the two ladders are read off
    the same number and the panel and the payout cannot disagree.

    -> the pending announcement dict, or None when the day earned nothing (an unplayed
    day must not pop two empty popups).
    """
    score = int(ch.get("best", 0))
    if score <= 0:
        return None
    # The group the day was PLAYED under, not today's -- a settlement that lands after
    # midnight would otherwise pay the wrong boss's table.
    group = int(ch.get("group") or challenge_weekday())
    personal = _bracket_rewards(REWARD_PERSONAL_DAILY, group, score)
    guild = _bracket_rewards(REWARD_GUILD_DAILY, group, score)
    for iid, cnt in personal + guild:
        grant_reward(state, iid, cnt)
    if not personal and not guild:
        return None
    return {"pscore": score, "gscore": score,
            "personal": [[i, c] for i, c in personal],
            "guild": [[i, c] for i, c in guild]}


def _challenge(state, now=None):
    """The account's challenge record, rolled to the current day/week."""
    ch = state.get("challenge")
    if not isinstance(ch, dict):
        ch = state["challenge"] = {}
    key, period = challenge_key(now), _daily_period(now)
    if ch.get("key") != key:
        ch.clear()
        ch["key"] = key
    if ch.get("day") != period:
        # Scores are DAILY (rewardType 3 and 5 are both "daily" tables); only the week
        # key outlives the 4AM rollover. Settle the day that just ended BEFORE wiping
        # it -- this is the only moment the finished day's score still exists.
        if ch.get("day"):
            pending = settle_day(state, ch)
            if pending:
                state.setdefault("challenge_pending", []).append(pending)
        ch["day"] = period
        ch["today"] = []
        ch["best"] = 0
        ch["runs"] = 0
        # Remember which boss's tables today's runs are earning against, so a
        # settlement can pay the right ones however late it happens.
        ch["group"] = challenge_weekday(now)
    ch.setdefault("today", [])
    ch.setdefault("best", 0)
    ch.setdefault("runs", 0)
    ch.setdefault("group", challenge_weekday(now))
    return ch


def take_pending_settlement(state):
    """-> (intargs, strargs) for one queued reply 785, or None.

    Popped rather than peeked: the popup is a one-time announcement, and re-sending it
    on every panel open would re-announce rewards already in the bag.
    """
    queue = state.get("challenge_pending") or []
    if not queue:
        return None
    p = queue.pop(0)
    if not queue:
        state.pop("challenge_pending", None)

    def _dict(pairs):
        # Dictionary<int, List<RewardItem>> -- one entry per reward, keyed by index.
        # The client iterates the keys and flattens, so the grouping is free.
        return json.dumps({str(i): [{"id": int(iid), "count": int(cnt)}]
                           for i, (iid, cnt) in enumerate(pairs)},
                          separators=(",", ":"))

    return ([int(p.get("pscore", 0)), int(p.get("gscore", 0))],
            [_dict(p.get("personal") or []), _dict(p.get("guild") or [])])


def challenge_reset_seconds(now=None):
    """Seconds to the next 4AM. The client stores `resetTime = utcNow + intargs[3]`,
    i.e. it wants a DURATION, not a timestamp -- which is also why the value does not
    have to be reconciled with the client's clock."""
    from .core import DAILY_RESET_HOUR
    t = now if now is not None else time.time()
    lt = time.localtime(t)
    secs = lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec
    reset = DAILY_RESET_HOUR * 3600
    return (reset - secs) if secs < reset else (86400 - secs + reset)


def challenge_sync_intargs(state, now=None):
    """intargs for the sync reply (784).

    **Exactly five, and exactly one strarg** -- SyncChallengeDataReply tests
    `intargs.Count == 5 && strargs.Count == 1` and silently logs-and-returns otherwise,
    so a six-int reply is not "extra", it is no reply at all.

      [0] rank - 1   (the client stores rank = intargs[0] + 1)
      [1] total      -> ChallengeInfo.total, the personal high score label
      [2] unused
      [3] seconds until reset
      [4] guildTotal -> the guild score label
    """
    ch = _challenge(state, now)
    best = int(ch.get("best", 0))
    # Rank 1 once there is a score at all; with one member there is nobody to be
    # second to. rank is only rendered when total != 0, so 0 here reads as unranked.
    rank = 1 if best else 0
    return [max(0, rank - 1), best, 0, challenge_reset_seconds(now), best]


def have_challenge_pass(state):
    return item_count(state, CHALLENGE_PASS_ITEM) > 0


def start_challenge(state, difficulty, now=None):
    """Charge a Weekly Guild Pass and resolve the stage for today's boss.

    -> (stage_id, error). The client has ALREADY resolved the same stage id to open
    the pre-battle panel; we re-derive it rather than trusting a value we were not
    sent (the fight request carries only the difficulty).
    """
    difficulty = max(1, min(int(difficulty or 1), CHALLENGE_DIFFICULTIES))
    weekday = challenge_weekday(now)
    stage_id = challenge_stage_id(weekday, difficulty)
    if not stage_id:
        return None, f"no book-{CHALLENGE_BOOK} stage for day {weekday} difficulty {difficulty}"
    if not spend_item(state, CHALLENGE_PASS_ITEM, 1):
        return None, "no Weekly Guild Pass (item 22)"
    ch = _challenge(state, now)
    ch["runs"] = int(ch.get("runs", 0)) + 1
    ch["pending"] = difficulty
    return stage_id, ""


def _reward_rows(reward_type, group):
    """challenge_reward rows for one bucket and group, ascending by threshold."""
    rows = [r for r in (bt.dd.rows("challenge_reward") or {}).values()
            if r.get("_rewardType") == reward_type and str(r.get("_group")) == str(group)]
    return sorted(rows, key=lambda r: int(r.get("_lower") or 0))


def _bracket_index(rows, score):
    """The index of the highest bracket `score` reaches, or -1 for none."""
    hit = -1
    for i, r in enumerate(rows):
        if score >= int(r.get("_lower") or 0):
            hit = i
    return hit


def finish_challenge(state, damage, now=None):
    """Bank a completed Guild Weekly run.

    -> (damage, bonus, total_damage, payouts) where the first three are exactly the
    three ints reply 786 wants -- ChallengeBattleRewardReply tests
    `intargs.Count == 3` -- and feed the score / bonus / total labels on the battle
    result panel's Guild page.

    Rewards come from the PersonalDaily (`_rewardType` 3) table for today's group, and
    are paid per bracket CLIMBED so that a better run tops up rather than paying the
    whole ladder again.
    """
    ch = _challenge(state, now)
    difficulty = int(ch.pop("pending", 1) or 1)
    weekday = challenge_weekday(now)
    damage = max(0, int(damage))

    # `bonus` is a separate label from `score` on the result panel. Higher difficulties
    # are the whole point of the difficulty selector, so that is what it rewards:
    # Easy adds nothing, Nightmare adds 75%.
    bonus = damage * (difficulty - 1) // 4
    total = damage + bonus

    # Append this try's score; the panel lists them in the order they were taken.
    ch.setdefault("today", []).append(total)
    prev_best = int(ch.get("best", 0))
    if total > prev_best:
        ch["best"] = total

    # **Nothing is paid here.** Both daily tables are END-OF-DAY settlements -- see
    # settle_day() and reply 785, which announces them as two item popups naming the
    # previous personal and guild scores. Paying per-run would double up against that
    # sweep and would also pay a ladder the player had not finished climbing.
    #
    # PersonalBest (rewardType 6) is never paid at all: all ten of its brackets pay
    # literally 1x Coin in this build, which is placeholder data, not a reward.
    payouts = []

    # Two fields on the GUILD member record are what the raid panel's leaderboard and
    # My Record actually read, and neither lives in `challenge`:
    #
    #   MongoMember.Contribution (`ctb`)  -> the "Raid pt." column. Both leaderboard
    #       tabs sort on it (LeaderboardPersonalComparer is IComparer<MemberData>) and
    #       UIWeeklyLeaderboardIcon iconType 0 prints
    #       `PlayerGuild.MembersDic[member.uid].MongoData.Contribution`.
    #   MongoMember.ChallengeTopScoreDic (`challengeTopScore`) -> the per-boss best,
    #       keyed by CHALLENGE GROUP (not weekday index, not stage id); iconTypes 2
    #       and 3 read `[_challengeGroup]` and print 0 when the key is absent.
    #
    # We were sending both, but nothing ever wrote them, so Raid pt. sat at 0 forever
    # and every history row read 0 no matter how well the fight went.
    guild = state.get("guild")
    if isinstance(guild, dict):
        guild["contribution"] = int(guild.get("contribution", 0)) + total
        # Group N is weekday N (WeekdayGroup is the identity map -- see
        # challenge_stages_json), and the dict is keyed by the group.
        top = guild.setdefault("challenge_top", {})
        key = str(weekday)
        if total > int(top.get(key, 0)):
            top[key] = total
    return damage, bonus, total, payouts


def challenge_rank_json(state, now=None):
    """strargs[0] of the guild-ranking reply (801): a JSON array of ChallengeRanking.

    Wire keys are uid/rank/hs; name/ptype/pid/iconID are NOT [JsonProperty] and are
    filled in client-side from the guild member list, so sending them is pointless.
    """
    from .core import uid
    ch = _challenge(state, now)
    best = int(ch.get("best", 0))
    if not best:
        return "[]"
    return json.dumps([{"uid": uid(state), "rank": 1, "hs": best}],
                      separators=(",", ":"))

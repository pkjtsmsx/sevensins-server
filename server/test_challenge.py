#!/usr/bin/env python3
"""Guild Weekly (PlayerChallenge), on the state layer.

The client-side traps these pin down, all of which fail SILENTLY or by throwing deep
inside NGUI rather than anywhere useful:

  * `StageSyncData.weekday` must be 1..7. `PanelGuildWeekly.OnChallengeClick` indexes
    `WeekdayGroup[weekday - 1]` behind an UNSIGNED bounds check, so the 0 the server
    used to send reads as 4294967295 and throws before the fight request is even built.
  * the sync reply is `intargs.Count == 5 && strargs.Count == 1` -- tested for equality,
    not minimum, and a mismatch is logged-and-dropped, not an error.
  * the battle-end reply is `intargs.Count == 3`, same deal.
  * every `g` (StagesDic) entry needs all four difficulties, because the client picks
    `[difficulty - 1]` out of it with no fallback.
  * server and client must independently resolve the SAME stage from
    (weekday, difficulty): the fight request carries the difficulty and nothing else.

    python3 test_challenge.py
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

_TMP = tempfile.mkdtemp(prefix="sevensins-challenge-test-")
os.environ["SEVENSINS_ACCOUNTS"] = _TMP

import battle as bt                                    # noqa: E402
import player_state as ps                              # noqa: E402
from player_state import challenge as ch                # noqa: E402
from player_state.core import _default, _seed_roster    # noqa: E402

_fail = 0


def check(name, cond, detail=""):
    global _fail
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        _fail += 1


def fresh():
    st = _default(1000001)
    _seed_roster(st)
    ps.grant_item(st, ch.CHALLENGE_PASS_ITEM, ch.CHALLENGE_MAX_TIMES)
    return st


def check_content_exists():
    """The 28 book-8 stages are the whole reason this subsystem is worth building."""
    rows = {sid: r for sid, r in bt.dd.rows("stage").items()
            if r.get("_book") == ch.CHALLENGE_BOOK}
    check("book 8 holds 28 Guild Weekly stages", len(rows) == 28, str(len(rows)))
    sorts = {int(r.get("_sort") or 0) for r in rows.values()}
    diffs = {int(r.get("_difficulty") or 0) for r in rows.values()}
    check("  ...seven bosses", sorts == set(range(1, 8)), str(sorted(sorts)))
    check("  ...times four difficulties", diffs == {1, 2, 3, 4}, str(sorted(diffs)))
    # A stage the battle engine cannot build is not content.
    for sid in sorted(rows):
        grp = rows[sid].get("_mobGroup_datas")
        if not bt.dd.row("mob_group", int(grp)):
            check(f"stage {sid} names a real mob group", False, str(grp))
            return
    check("every one names a mob group that exists", True)
    check("entry costs no stamina (the pass is the cost)",
          all(int(r.get("_ap") or 0) == 0 for r in rows.values()))


def check_weekday_wiring():
    """The single most load-bearing integer in this whole subsystem."""
    st = fresh()
    wd = json.loads(ps.stage_json(st))["weekday"]
    check("StageSyncData.weekday is in 1..7", 1 <= wd <= 7, str(wd))
    check("  ...and agrees with challenge_weekday()", wd == ch.challenge_weekday(),
          f"{wd} vs {ch.challenge_weekday()}")

    stages = json.loads(ch.challenge_stages_json(st))
    group = stages["weekday"][wd - 1]
    check("WeekdayGroup is long enough to index with it",
          len(stages["weekday"]) >= wd, str(stages["weekday"]))
    check("  ...and the group it names is in StagesDic",
          str(group) in stages["g"], f"{group} not in {sorted(stages['g'])}")

    # This is the client's own arithmetic, transcribed from OnChallengeClick.
    for difficulty in range(1, ch.CHALLENGE_DIFFICULTIES + 1):
        client_side = stages["g"][str(group)][difficulty - 1]
        server_side = ch.challenge_stage_id(wd, difficulty)
        check(f"server and client agree on the stage at difficulty {difficulty}",
              client_side == server_side, f"{client_side} vs {server_side}")


def check_stages_json():
    st = fresh()
    s = json.loads(ch.challenge_stages_json(st))
    check("ChallengeStages uses curr_key/weekday/g/scores",
          set(s) == {"curr_key", "weekday", "g", "scores"}, str(set(s)))
    # `scores` is one entry per TRY (the panel's 1st/2nd/3rd Try rows), not per
    # difficulty -- live footage of the Guild Raid screen settles it.
    check("an unplayed day reports no try scores", s["scores"] == [], str(s["scores"]))
    check("  ...and the list can never exceed the daily tryouts",
          len(s["scores"]) <= ps.CHALLENGE_MAX_TIMES, str(s["scores"]))
    check("all seven groups are present", len(s["g"]) == 7, str(sorted(s["g"])))
    # `[difficulty - 1]` with no fallback: a short list picks the wrong boss or throws.
    check("every group lists all four difficulties",
          all(len(v) == ch.CHALLENGE_DIFFICULTIES for v in s["g"].values()),
          str({k: len(v) for k, v in s["g"].items()}))
    check("  ...with no missing stage ids",
          all(all(i for i in v) for v in s["g"].values()))
    check("curr_key is a stable week name", s["curr_key"] == ch.challenge_key(),
          s["curr_key"])


def check_try_scores_accumulate():
    """My Record shows 1st / 2nd / 3rd Try, so `scores` grows one entry per run."""
    st = fresh()
    got = []
    for _ in range(ps.CHALLENGE_MAX_TIMES):
        ch.start_challenge(st, 2)
        _d, _b, total, _p = ch.finish_challenge(st, 100000)
        got.append(total)
        js = json.loads(ch.challenge_stages_json(st))
        check(f"after {len(got)} run(s) the panel has {len(got)} try score(s)",
              js["scores"] == got, f"{js['scores']} vs {got}")


def check_sync_shape():
    st = fresh()
    ints = ch.challenge_sync_intargs(st)
    # SyncChallengeDataReply: `intargs.Count == 5 && strargs.Count == 1`, equality.
    check("the sync reply has EXACTLY five ints", len(ints) == 5, str(ints))
    check("an unplayed day reports no rank", ints[0] == 0 and ints[1] == 0, str(ints))
    check("the reset countdown is a positive DURATION, not a timestamp",
          0 < ints[3] <= 86400, str(ints[3]))


def check_fight_and_score():
    st = fresh()
    check("a day starts with three passes",
          ps.item_count(st, ch.CHALLENGE_PASS_ITEM) == ch.CHALLENGE_MAX_TIMES,
          str(ps.item_count(st, ch.CHALLENGE_PASS_ITEM)))

    stage_id, why = ch.start_challenge(st, 2)
    check("a fight resolves a stage", bool(stage_id), why)
    check("  ...one of the 28", ch.is_challenge_stage(stage_id), str(stage_id))
    check("  ...and costs a pass",
          ps.item_count(st, ch.CHALLENGE_PASS_ITEM) == ch.CHALLENGE_MAX_TIMES - 1)

    gp_before = st["currency"]["64"]
    dmg, bonus, total, payouts = ch.finish_challenge(st, 600000)
    # ChallengeBattleRewardReply: intargs.Count == 3.
    check("the battle-end reply is three ints",
          len([dmg, bonus, total]) == 3)
    check("damage is carried through unchanged", dmg == 600000, str(dmg))
    check("the difficulty bonus is on top, not instead", total == dmg + bonus,
          f"{dmg}+{bonus} != {total}")
    check("Normal pays a smaller bonus than Nightmare",
          bonus < 600000 * 3 // 4, str(bonus))
    check("crossing brackets pays out", bool(payouts), str(payouts))
    check("  ...in Guild Pt", st["currency"]["64"] > gp_before,
          f"{gp_before} -> {st['currency']['64']}")

    ints = ch.challenge_sync_intargs(st)
    check("the score shows up as the personal best", ints[1] == total, str(ints))
    check("  ...and as the guild total (a guild of one)", ints[4] == total, str(ints))
    check("  ...and now carries a rank", ints[0] == 0 and ints[1] > 0, str(ints))

    # Brackets are cumulative: a better run tops up, it does not re-pay the ladder.
    ch.start_challenge(st, 2)
    gp_mid = st["currency"]["64"]
    _, _, _, again = ch.finish_challenge(st, 600001)
    check("an equal-ish rerun pays nothing new", again == [], str(again))
    check("  ...and leaves the balance alone", st["currency"]["64"] == gp_mid)

    ch.start_challenge(st, 4)
    _, _, big, topup = ch.finish_challenge(st, 4000000)
    check("a much better run pays the newly-climbed brackets", bool(topup), str(topup))
    check("  ...and raises the best", ch.challenge_sync_intargs(st)[1] == big)

    stage_id, why = ch.start_challenge(st, 1)
    check("a fourth run is refused once the passes are gone",
          stage_id is None and "Pass" in why, why)


def check_daily_roll():
    st = fresh()
    ch.start_challenge(st, 3)
    ch.finish_challenge(st, 900000)
    before = ch.challenge_sync_intargs(st)[1]
    check("the day has a score", before > 0, str(before))
    st["challenge"]["day"] = "1999-01-01"
    after = ch.challenge_sync_intargs(st)
    check("the 4AM rollover clears the day's score", after[1] == 0, str(after))
    check("  ...and re-arms the reward brackets", st["challenge"]["paid"] == 0)
    # The week key outlives the day; only a new week wipes it.
    check("the week key survives a day rollover",
          st["challenge"]["key"] == ch.challenge_key(), st["challenge"]["key"])


def check_rank_json():
    st = fresh()
    check("an unplayed account posts an empty leaderboard",
          ch.challenge_rank_json(st) == "[]", ch.challenge_rank_json(st))
    ch.start_challenge(st, 1)
    ch.finish_challenge(st, 500000)
    r = json.loads(ch.challenge_rank_json(st))
    check("after a run there is one entry", len(r) == 1, str(r))
    # name/ptype/pid/iconID are not [JsonProperty]; the client fills them from the
    # member list, so sending them would be dead weight.
    check("  ...carrying only uid/rank/hs", set(r[0]) == {"uid", "rank", "hs"},
          str(set(r[0])))


def main():
    for fn in (check_content_exists, check_weekday_wiring, check_stages_json,
               check_sync_shape, check_try_scores_accumulate,
               check_fight_and_score, check_daily_roll,
               check_rank_json):
        print(f"\n{fn.__name__}:")
        fn()
    print(f"\n{_fail} failure(s)")
    return 1 if _fail else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        import shutil
        shutil.rmtree(_TMP, ignore_errors=True)

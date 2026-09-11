#!/usr/bin/env python3
"""Daily / weekly / monthly missions (quest _type 3, groups 4/5/6).

These pay item 46 "Feel Lucky", whose only sink is the Premium Shop's Super Sales tab.
Two things were missing: the counters nothing bumped, and any notion of a window -- an
sp_quests entry kept its COMPLETE status forever, and bump_quest_counter deliberately
refuses to re-arm a completed entry, so every mission was once-per-account.
"""
import player_state as ps
from player_state import quests as q
from player_state import roulette as rl
from player_state.core import _default

FAILED = []


def check(cond, msg):
    if not cond:
        FAILED.append(msg)
        print(f"  FAIL {msg}")


def fresh():
    st = _default(1000001)
    st["sp_quests"] = {}
    return st


def cnt(st, qid):
    return int((st["sp_quests"].get(str(qid)) or {}).get("cnt", 0))


def test_groups_are_daily_weekly_monthly():
    for group, expect in ((4, 11), (5, 5), (6, 5)):
        ids = q.mission_quest_ids(group)
        check(len(ids) == expect,
              f"group {group} has {expect} missions, got {len(ids)}")
    check(10032 in q.mission_quest_ids(4), "the roulette daily is in group 4")
    check(10042 in q.mission_quest_ids(5), "the guild weekly is in group 5")


def test_daily_login_ticks():
    st = fresh()
    ps.reset_periodic_missions(st)
    ps.bump_mission_login(st)
    check(cnt(st, 10031) == 1, f"Daily Login counts, got {cnt(st, 10031)}")


def test_roulette_spin_ticks_and_resets():
    st = fresh()
    ok, _res, why, _bk = rl.roulette_draw(st, 101)
    check(ok, f"a free spin works: {why}")
    check(cnt(st, 10032) == 1, f"the spin credits the daily, got {cnt(st, 10032)}")
    # Spend the day's allowance, then confirm it is genuinely exhausted...
    for _ in range(rl.ROULETTE_DRAW_MAX):
        rl.roulette_draw(st, 101)
    ok, _res, why, _bk = rl.roulette_draw(st, 101)
    check(not ok, "the daily spin allowance runs out")
    # ...and comes back with the new day. This is the bug: `day` was never cleared.
    st["roulette_day"] = "1999-01-01"
    check(ps.expire_roulette_day(st), "a new day clears the spin counter")
    ok, _res, why, _bk = rl.roulette_draw(st, 101)
    check(ok, f"spins are available again: {why}")


def test_roulette_reports_every_bucket_it_pays():
    """Every wheel slot's sync bucket comes back from the grant, not from a list.

    The handler pushed currency + Normal storage unconditionally, so the two Stamina
    slots -- item 5, `_action 6`, which `grant_reward` routes to `energy` -- were paid
    into the save and never synced, and only showed up after a relog. Asserting the
    bucket SET over the whole wheel rather than "energy is in there somewhere" is the
    point: a slot added later that routes somewhere new fails this until its sync is
    wired up.
    """
    st = fresh()
    paid = set()
    for iid, amount in rl.ROULETTE_SLOTS + rl.ROULETTE_BONUS:
        paid.add(ps.grant_reward(st, iid, amount))
    check(paid == {"currency", "backpack", "energy"},
          f"the wheel pays exactly these buckets, got {sorted(paid)}")

    # And a real spin reports the bucket its own slot landed in -- every time, for
    # whichever slot the roll picked.
    st = fresh()
    for _ in range(rl.ROULETTE_DRAW_MAX):
        ok, res, why, buckets = rl.roulette_draw(st, 101)
        if not ok:
            break
        iid = res[0][1]
        check(buckets and buckets <= {"currency", "backpack", "energy"},
              f"spin reported {buckets}")
        check(ps.grant_reward(fresh(), iid, res[0][2]) in buckets,
              f"item {iid} lands in {buckets}")


def test_summon_counts_pulls_not_presses():
    """"[Daily] Summon 10 times" (case 13, cnt 10) must be satisfied by ONE ten-pull --
    the counter used to move by 1 per commit, so a ten-pull read 1/10."""
    from player_state import gacha as g
    st = fresh()
    st["gacha_count"] = 5                      # past the scripted tutorial roll
    # gacha_commit charges before it grants, and bails without bumping if it cannot.
    ps.grant_item(st, g.GACHA_COST_ITEM, 500)
    g.gacha_draw(st, count=10, box_id=3101)
    g.gacha_commit(st)
    check(cnt(st, 10035) == 10, f"a ten-pull counts 10 summons, got {cnt(st, 10035)}")
    g.gacha_draw(st, count=1, box_id=3101)
    g.gacha_commit(st)
    check(cnt(st, 10035) == 11, f"a single adds one more, got {cnt(st, 10035)}")


def test_stamina_and_diamond_counters():
    st = fresh()
    ps.bump_quest_counter(st, q.QUEST_CASE_SPEND_ITEM, 120,
                          case_v1=q.STAMINA_ITEM_ID)
    check(cnt(st, 10034) == 120, f"stamina spend accrues, got {cnt(st, 10034)}")
    st2 = fresh()
    st2["currency"]["1"] = 10000
    ps.spend_diamonds(st2, 300)
    check(cnt(st2, 10054) == 300, f"diamond spend accrues, got {cnt(st2, 10054)}")


def test_free_merchandise_cards_are_the_ones_named():
    """2003's `_case_v1` is a GOODS id -- 1101/1201/1301 must exist in our shop table,
    or those three missions can never be completed."""
    from player_state import shop as sh
    ids = {r[0] for r in sh.DEFAULT_SHOP_GOODS["1"]}
    for gid, qid in ((1101, 10033), (1201, 10041), (1301, 10051)):
        check(gid in ids, f"goods {gid} (mission {qid}) exists")


def test_claiming_a_daily_credits_the_weekly():
    st = fresh()
    ps.reset_periodic_missions(st)
    ps.bump_mission_login(st)
    ps.complete_quests(st, [10031])
    check(cnt(st, 10044) == 1,
          f"claiming a daily ticks 'complete 25 dailies', got {cnt(st, 10044)}")
    # A second press of the same row must not pay the weekly twice.
    ps.complete_quests(st, [10031])
    check(cnt(st, 10044) == 1, f"and only once, got {cnt(st, 10044)}")


def test_windows_re_arm_independently():
    st = fresh()
    ps.reset_periodic_missions(st)
    ps.bump_mission_login(st)
    ps.complete_quests(st, [10031])
    done = st["sp_quests"]["10031"]
    check(done["status"] == q.SP_QUEST_COMPLETE, "the daily is claimed")
    weekly_before = cnt(st, 10044)

    # A new DAY only re-arms group 4.
    st["mission_periods"]["4"] = "1999-01-01"
    reset = ps.reset_periodic_missions(st)
    check(10031 in reset, f"the daily re-arms, got {reset}")
    check(st["sp_quests"]["10031"]["cnt"] == 0, "and its count is back to 0")
    check(st["sp_quests"]["10031"]["status"] != q.SP_QUEST_COMPLETE,
          "and it is claimable again")
    check(cnt(st, 10044) == weekly_before,
          "while the weekly's progress is untouched")
    # It can then be earned again -- the whole point.
    ps.bump_mission_login(st)
    check(cnt(st, 10031) == 1, "and the new day's login credits it")

    # Same window twice is a no-op.
    check(ps.reset_periodic_missions(st) == [], "re-running in-window resets nothing")


def test_period_keys_use_the_shared_4am_boundary():
    import time
    from player_state.core import DAILY_RESET_HOUR
    check(DAILY_RESET_HOUR == 4, "the shared boundary is 4AM")
    # 03:00 local belongs to the PREVIOUS day, so a 3AM login must not re-arm.
    t = time.localtime()
    three_am = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 3, 0, 0, 0, 0, -1))
    five_am = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 5, 0, 0, 0, 0, -1))
    check(q._mission_period(4, three_am) != q._mission_period(4, five_am),
          "3AM and 5AM are different daily windows")
    check(q._mission_period(4, five_am) == q._mission_period(4, five_am + 3600),
          "5AM and 6AM are the same one")
    check(q._mission_period(6, five_am) == q._mission_period(6, five_am + 86400),
          "a day later is still the same month")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            print(name)
            fn()
    print("ALL PASSED" if not FAILED else f"{len(FAILED)} CHECK(S) FAILED")
    raise SystemExit(1 if FAILED else 0)

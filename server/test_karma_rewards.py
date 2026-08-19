#!/usr/bin/env python3
"""Karma (Kizuna) rank-ups pay their Rank Bonus.

Reported 2026-08-18: "when casts level up their karma ranking, we're not getting this
pop up nor the rank rewards". `grant_karma` advanced flv/fxp and stopped there -- it
never looked at the reward table, so every Rank Bonus row was silently skipped.

The rewards are in `char_flv`, one row per (cast, rank), discriminated by
`_unlock_type`:

    5 -> pays `_bonus_item_id` x `_bonus_item_cnt`   (2416 rows -- ours to hand over)
    2 -> unlocks a Kizuna Quest                      (the client's own gating)
    3 / 6 -> stat bonuses the client derives itself

Only type 5 is a payout. Screenshot of Lucifer's list, for the record: rank 1 Kizuna
Quest, rank 2 Diamond x50, rank 3 HP Increase, rank 4 Kizuna Quest, ranks 5/6
Diamond x50, rank 7 Holy Blood x35 -- which is exactly what the table yields.

**One grant can cross several ranks**, and every crossed rank pays.

    python3 test_karma_rewards.py
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

_TMP = tempfile.mkdtemp(prefix="sevensins-karma-test-")
os.environ["SEVENSINS_ACCOUNTS"] = _TMP

import battle as bt                                     # noqa: E402
import player_state as ps                               # noqa: E402
import titan_server as ts                               # noqa: E402

_fail = 0
LUCIFER = 10001


def check(name, cond, detail=""):
    global _fail
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        _fail += 1


def check_table_matches_the_screenshot():
    want = {2: [(1, 50)], 5: [(1, 50)], 6: [(1, 50)], 7: [(3, 35)],
            1: [], 3: [], 4: []}
    for rank, expect in sorted(want.items()):
        got = ps.karma_rank_rewards(LUCIFER, rank - 1, rank)
        check(f"Lucifer rank {rank} pays {expect or 'nothing'}", got == expect, str(got))
    check("only `_unlock_type` 5 rows are treated as payouts",
          ps.KARMA_UNLOCK_ITEM == 5, str(ps.KARMA_UNLOCK_ITEM))
    # Every cast with a table must have at least one paying rank, or the whole
    # mechanic is dead for them.
    chars = {int(r["_char_id"]) for r in bt.dd.rows("char_flv").values()
             if r.get("_char_id")}
    payless = [c for c in sorted(chars)[:40]
               if not ps.karma_rank_rewards(c, 0, 99)]
    check("casts with a Karma table have paying ranks", not payless,
          str(payless[:5]))


def check_a_single_rank_up_pays_once():
    st = ps.load(1000030)
    st["karma"] = {str(LUCIFER): {"flv": 1, "fxp": 0}}
    before = int(st["currency"]["1"])
    need = ps.char_flv_need_xp(
        (bt.dd.row("char", LUCIFER) or {}).get("_rarity"), 1)
    k = ps.grant_karma(st, LUCIFER, need)
    check("the rank advances by one", k["flv"] == 2, str(k["flv"]))
    check("  ...and pays only that rank's bonus", k["_paid"] == [(1, 50)], str(k["_paid"]))
    # **Mailed, not granted.** The rank-up splash says so itself -- text 115605,
    # "Sent to mailbox", printed beside the reward on the Karma RANK screen. So the
    # balance must NOT move; the items wait in the mail until claimed.
    check("  ...WITHOUT touching the diamond balance",
          int(st["currency"]["1"]) == before,
          f"{before} -> {st['currency']['1']}")
    mails = st.get("mail") or []
    check("  ...and arriving as mail instead", bool(mails), str(mails))
    check("  ...carrying the 50 diamonds",
          mails and mails[-1]["items"].get("1") == 50, str(mails[-1] if mails else None))
    check("  ...titled with the rank reached",
          mails and mails[-1]["custom"] == "Karma Rank 2", str(mails[-1]["custom"]))


def check_multi_rank_pays_every_rank_crossed():
    """The user's point: you can rank up several times at once."""
    st = ps.load(1000031)
    before = int(st["currency"]["1"])
    k = ps.grant_karma(st, LUCIFER, 10 ** 5)
    check("one grant can cross many ranks", k["flv"] > 5, str(k["flv"]))
    every = ps.karma_rank_rewards(LUCIFER, 0, k["flv"])
    check("  ...and pays every paying rank in the range",
          k["_paid"] == every, f"{len(k['_paid'])} vs {len(every)}")
    diamonds = sum(c for i, c in every if i == 1)
    # Every crossed rank rides in ONE mail -- the splash announces a rank, and several
    # ranks crossed at once should not bury the player in mail.
    mails = st.get("mail") or []
    check("  ...all in a single mail", len(mails) == 1, str(len(mails)))
    check("  ...for the full diamond total",
          mails and mails[-1]["items"].get("1") == diamonds,
          f'{mails[-1]["items"] if mails else None} vs {diamonds}')
    check("  ...which is more than a single rank would pay",
          diamonds > 50, str(diamonds))
    check("  ...and the balance still has not moved",
          int(st["currency"]["1"]) == before,
          f'{before} -> {st["currency"]["1"]}')

    # Ranking up again from there must not re-pay what was already banked.
    mid = len(st.get("mail") or [])
    again = ps.grant_karma(st, LUCIFER, 10 ** 5)
    check("re-granting at max pays nothing twice",
          len(st.get("mail") or []) == mid, str(again["_paid"]))


def check_no_rank_up_pays_nothing():
    st = ps.load(1000032)
    st["karma"] = {str(LUCIFER): {"flv": 2, "fxp": 0}}
    before = int(st["currency"]["1"])
    k = ps.grant_karma(st, LUCIFER, 1)
    check("xp that does not cross a rank pays nothing",
          k["_paid"] == [] and int(st["currency"]["1"]) == before, str(k))


def check_the_payout_is_pushed():
    """A grant the client is never told about may as well not have happened."""
    st = ps.load(1000033)
    k = ps.grant_karma(st, LUCIFER, 10 ** 5)
    msgs = ts.karma_reward_msgs(st, k)
    check("a paying rank-up produces push messages", bool(msgs), str(len(msgs)))
    check("nothing to push when nothing was paid",
          ts.karma_reward_msgs(st, {"_paid": []}) == [])


def check_gifts_report_the_bonus():
    st = ps.load(1000034)
    shape = ps.give_gifts(st, LUCIFER, [])
    check("give_gifts returns (ok, xp, used, rank_paid)", len(shape) == 4, str(shape))
    check("  ...and refuses cleanly with no items", shape[0] is False)


def main():
    for fn in (check_table_matches_the_screenshot, check_a_single_rank_up_pays_once,
               check_multi_rank_pays_every_rank_crossed, check_no_rank_up_pays_nothing,
               check_the_payout_is_pushed, check_gifts_report_the_bonus):
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

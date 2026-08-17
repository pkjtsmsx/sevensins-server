#!/usr/bin/env python3
"""Login-bonus ladder: rollover, payout and what the panel is told.

The panel is display-only (no claim RPC exists), so everything here is about the server
moving the ladder itself -- the bug this covers is a ladder that sat on day 1 forever
because login_day/login_claimed_day were READ but never written.
"""
import json

import player_state as ps
from player_state.loginbonus import advance_login_bonus, login_bonus_json, _ladder

DAY = 86400
FAILED = []


def check(cond, msg):
    if not cond:
        FAILED.append(msg)
        print(f"  FAIL {msg}")


def sync(state):
    return json.loads(login_bonus_json(state))["group_data"][0]


def test_first_login_pays_day_one():
    st = {}
    paid = advance_login_bonus(st, now=100 * DAY)
    check(len(paid) == 1 and paid[0][0] == 1, f"first login pays day 1, got {paid}")
    # day 1 of the 28-day ladder is 50000 coin (mail row 1001)
    check(paid[0][1:] == (2, 50000), f"day 1 is 50000 coin, got {paid[0][1:]}")
    check(len(st["mail"]) == 1, "the payout arrives as one mail")
    m = st["mail"][0]
    check(m["format_id"] == 1001 and m["items"] == {2: 50000},
          f"mail carries the row and the attachment, got {m}")
    check(str(m["custom"]) == "1", f"{{custom}} is the day number, got {m['custom']!r}")


def test_one_per_calendar_day():
    st = {}
    advance_login_bonus(st, now=100 * DAY)
    again = advance_login_bonus(st, now=100 * DAY + 3600)   # same UTC day
    check(again == [], f"a second login the same day pays nothing, got {again}")
    check(len(st["mail"]) == 1, "and files no second mail")
    nxt = advance_login_bonus(st, now=101 * DAY)
    check(len(nxt) == 1 and nxt[0][0] == 2, f"the next day pays day 2, got {nxt}")


def test_no_backpay_for_days_away():
    st = {}
    advance_login_bonus(st, now=100 * DAY)
    paid = advance_login_bonus(st, now=110 * DAY)           # gone for ten days
    check(len(paid) == 1 and paid[0][0] == 2,
          f"a gap resumes at the next rung, not seven mails, got {paid}")


def test_ladder_wraps_into_the_next_book():
    total = max(_ladder())
    st = {"login_claimed_day": total, "login_book": 1, "login_last_day": 0}
    paid = advance_login_bonus(st, now=200 * DAY)
    check(paid and paid[0][0] == 1, f"day {total} + 1 wraps to day 1, got {paid}")
    check(st["login_book"] == 2, f"and starts book 2, got {st.get('login_book')}")


def test_checkin_is_announced_once():
    """cin_day gates EnqueueBonusGroup, so a standing non-zero value re-opens the panel
    on every UI refresh -- the "it keeps popping up" bug."""
    st = {}
    advance_login_bonus(st, now=100 * DAY)
    check(sync(st)["cin_day"] == 1, "the sync after a rollover announces the check-in")
    # the dispatcher retires it once the sync has been sent
    st.pop("login_checkin_day", None)
    check(sync(st)["cin_day"] == 0, "every later sync reports no check-in")
    check(sync(st)["dcnt"] == 2, "while dcnt still shows day 1 as received")
    again = advance_login_bonus(st, now=100 * DAY + 3600)
    check(again == [] and sync(st)["cin_day"] == 0,
          "and a same-day re-login does not re-announce it")


def test_sync_leads_claimed_by_one():
    st = {}
    g = sync(st)
    check(g["dcnt"] == 1 and g["cin_day"] == 0,
          f"a fresh account is ON day 1 with nothing checked in, got {g}")
    advance_login_bonus(st, now=100 * DAY)
    g = sync(st)
    check(g["cin_day"] == 1, f"after paying day 1, cin_day is 1, got {g['cin_day']}")
    check(g["dcnt"] == 2, f"and dcnt moves to day 2, got {g['dcnt']}")
    # _nextBonusIndex = (dcnt % 7) - 1: with dcnt 2 the marker sits on the 2nd icon and
    # day 1 is drawn as received -- which is the whole visible symptom.
    check((g["dcnt"] % 7) - 1 == 1, "the NEXT! marker lands on day 2")


def test_dcnt_stays_inside_the_ladder():
    total = max(_ladder())
    st = {"login_claimed_day": total}
    g = sync(st)
    check(g["dcnt"] == total, f"dcnt is clamped to {total}, got {g['dcnt']}")
    check(g["tcnt"] == total, f"tcnt is the ladder length, got {g['tcnt']}")


def test_total_days_counts_lifetime_logins():
    st = {}
    for d in range(5):
        advance_login_bonus(st, now=(300 + d) * DAY)
    check(json.loads(login_bonus_json(st))["total_day"] == 5,
          "total_day counts every day logged in")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            print(name)
            fn()
    print("ALL PASSED" if not FAILED else f"{len(FAILED)} CHECK(S) FAILED")
    raise SystemExit(1 if FAILED else 0)

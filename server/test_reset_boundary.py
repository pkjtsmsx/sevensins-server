#!/usr/bin/env python3
"""Every reset in the economy lands on the 4AM boundary, in every timezone, all year.

The bug: `next_reset_time` walked forward in 86400-second blocks. A local day is 23 or 25
hours twice a year, so the answer slid off the boundary the whole economy is anchored to.
Caught by test_shops.py on 2026-10-22, which is the first run that happened to fall in the
week before Europe/Zurich's autumn transition:

    weekly  -> 2026-10-26 03:00:00   (should be 04:00)
    daily   -> 2026-10-25 03:00:00   on the transition day itself
    spring: -> 2027-03-29 05:00:00

`challenge_reset_seconds` had the same flaw in miniature -- it counted seconds since local
midnight, which disagrees with the 4AM anchor by an hour on a transition day.

This is deliberately a sweep rather than three hand-picked dates: the failure only shows on
the days around a transition, and which days those are depends on the zone, so a test that
checks "today" passes for 360 days a year and then fails on someone's phone. Zones are
chosen to cover both hemispheres (so spring-forward and fall-back land in different
months), a zone with no DST at all, and a 45-minute offset.

    python3 test_reset_boundary.py

Uses its own SEVENSINS_ACCOUNTS tempdir; touches no real save.
"""
import datetime
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

os.environ["SEVENSINS_ACCOUNTS"] = tempfile.mkdtemp(prefix="sevensins-reset-test-")

_fail = 0

# Southern and northern hemispheres transition in opposite months; Chatham is +12:45 and
# also observes DST; UTC never transitions at all.
ZONES = ("Europe/Zurich", "America/Los_Angeles", "Australia/Sydney",
         "Pacific/Chatham", "America/Sao_Paulo", "UTC")
DAYS = 800                      # > two years, so every transition is crossed twice


def check(name, cond, detail=""):
    global _fail
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))
    if not cond:
        _fail += 1


def _reload():
    """Re-import the modules that read the zone at import time."""
    for mod in [m for m in list(sys.modules)
                if m.startswith("player_state") or m == "battle"]:
        del sys.modules[mod]
    time.tzset()
    from player_state import challenge, shop, core
    return challenge, shop, core


def main():
    old_tz = os.environ.get("TZ")
    try:
        for zone in ZONES:
            os.environ["TZ"] = zone
            ch, sh, core = _reload()
            # Recompute the epoch anchor inside this zone so the sweep covers its own
            # local calendar rather than the previous zone's.
            base = int(time.mktime(time.strptime("2026-01-01 00:30", "%Y-%m-%d %H:%M")))

            off_boundary = not_monday = not_first = backwards = 0
            for day in range(DAYS):
                now = base + day * 86400 + 43200
                for cycle in (sh.RESET_DAILY, sh.RESET_WEEKLY, sh.RESET_MONTHLY):
                    at = sh.next_reset_time(cycle, now)
                    when = datetime.datetime.fromtimestamp(at)
                    if (when.hour, when.minute, when.second) != (4, 0, 0):
                        off_boundary += 1
                    if at <= now:
                        backwards += 1
                    if cycle == sh.RESET_WEEKLY and when.weekday() != 0:
                        not_monday += 1
                    if cycle == sh.RESET_MONTHLY and when.day != 1:
                        not_first += 1
            check(f"{zone}: every shop reset is at 04:00", off_boundary == 0,
                  f"{off_boundary} of {DAYS * 3} off the boundary")
            check(f"{zone}: every shop reset is in the future", backwards == 0)
            check(f"{zone}: weekly lands on a Monday", not_monday == 0)
            check(f"{zone}: monthly lands on the 1st", not_first == 0)

            # The challenge countdown is a DURATION, so check where it lands and that it
            # never exceeds one long day.
            bad_at = bad_len = 0
            for hour in range(0, DAYS * 24, 3):
                now = base + hour * 3600
                secs = ch.challenge_reset_seconds(now)
                when = datetime.datetime.fromtimestamp(now + secs)
                if (when.hour, when.minute, when.second) != (4, 0, 0):
                    bad_at += 1
                if not 0 < secs <= 25 * 3600:
                    bad_len += 1
            check(f"{zone}: the challenge countdown ends at 04:00", bad_at == 0,
                  f"{bad_at} wrong")
            check(f"{zone}: the challenge countdown is a sane length", bad_len == 0)

        # The fix has to be the CALENDAR walk, not a bigger constant. Prove the old
        # arithmetic really does fail, so this suite cannot pass against a reverted fix.
        os.environ["TZ"] = "Europe/Zurich"
        ch, sh, core = _reload()
        eve = int(time.mktime(time.strptime("2026-10-22 10:00", "%Y-%m-%d %H:%M")))
        day_start = core.at_reset_hour(eve, 0)
        naive = datetime.datetime.fromtimestamp(day_start + 4 * 86400)   # the old way
        fixed = datetime.datetime.fromtimestamp(sh.next_reset_time(sh.RESET_WEEKLY, eve))
        check("the 86400-block walk really does drift (regression guard)",
              naive.hour == 3 and fixed.hour == 4,
              f"old {naive}, new {fixed}")

        # And the shared helper is genuinely shared -- one boundary, not two.
        check("shop and challenge agree on the next 4AM",
              core.at_reset_hour(eve, 1) == eve + ch.challenge_reset_seconds(eve))
    finally:
        if old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old_tz
        time.tzset()

    print(f"\n{'all OK' if not _fail else str(_fail) + ' failure(s)'}")
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""A lethal DoT tick always tells the client. A non-fatal one is a switch.

    python3 test_dot_tick_switch.py

`TELL_NON_FATAL_DOT` exists because the non-fatal message is the one untested beat on the
wire, in db552c1's own words: an extra Perform in front of a unit that has not acted yet,
on every tick in the game. If a device says that is what hangs fights, the switch folds
those ticks back into `sync` -- the timing the client has always handled.

WHAT MUST NOT BE SWITCHABLE is the lethal tick. Without its message the client keeps a
dead unit in its ActionOrderList and waits forever for a turn the server will never hand
out; that is the soft-lock this project has already fixed twice. So the flag is asserted
in both positions, and the lethal case in both.

Driven through `_start_of_turn` rather than by queueing rows by hand, so what is being
checked is the real tick path.
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["SEVENSINS_ACCOUNTS"] = tempfile.mkdtemp(prefix="sevensins-dottick-test-")

import battle as bt                                            # noqa: E402
from engine import status as S                                  # noqa: E402

FAILURES = []


def check(cond, msg):
    if not cond:
        FAILURES.append(msg)


def _tick(tell, lethal):
    """Put one DoT on an enemy, run its turn start, and count wire messages."""
    bt.TELL_NON_FATAL_DOT = tell
    try:
        b = bt.Battle(1101, [10401])
        victim = next(u for u in b.units.values() if u.team == 2)
        victim.max_hp = 100_000
        victim.hp = 2_000 if lethal else 100_000
        victim.statuses = [S.Active(
            status_id=1004, name="Plague", kind="dot", category="debuff",
            magnitude=5_000.0, sign=1, remaining=3, source_atk=0, tick_whose="caster")]
        b.turn_order = [victim.order]
        b._pending_dot_deaths = []
        b._start_of_turn()
        return victim, b.dot_death_cmds_json()
    finally:
        bt.TELL_NON_FATAL_DOT = True


def a_lethal_tick_always_sends():
    for tell in (True, False):
        victim, msgs = _tick(tell=tell, lethal=True)
        check(not victim.alive, f"the fixture did not kill the unit (tell={tell})")
        check(len(msgs) == 1,
              f"a LETHAL tick sent {len(msgs)} messages with TELL_NON_FATAL_DOT={tell} "
              "-- the client would hold a dead unit in its action list")


def a_non_fatal_tick_follows_the_switch():
    victim, on = _tick(tell=True, lethal=False)
    check(victim.alive, "the fixture killed the unit -- not a non-fatal case")
    check(len(on) == 1, f"TELL_NON_FATAL_DOT=True sent {len(on)} messages, wanted 1")
    victim, off = _tick(tell=False, lethal=False)
    check(not off, f"TELL_NON_FATAL_DOT=False still sent {len(off)} messages")


def the_damage_is_the_same_either_way():
    """The switch costs the animation, not the HP."""
    on, _ = _tick(tell=True, lethal=False)
    off, _ = _tick(tell=False, lethal=False)
    check(on.hp == off.hp,
          f"the tick dealt {100_000 - on.hp} with the flag on and "
          f"{100_000 - off.hp} with it off -- it must only change what is SENT")


def main():
    for fn in (a_lethal_tick_always_sends, a_non_fatal_tick_follows_the_switch,
               the_damage_is_the_same_either_way):
        fn()
    for f in FAILURES:
        print("  FAIL", f)
    print(f"\n{'all OK' if not FAILURES else str(len(FAILURES)) + ' failure(s)'}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())

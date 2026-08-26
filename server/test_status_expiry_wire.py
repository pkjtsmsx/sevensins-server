#!/usr/bin/env python3
"""When a status expires on the server, the client must be told -- with a round-0 row.

    python3 test_status_expiry_wire.py

Seen on a phone 2026-08-26: the Guild Weekly boss stood "frozen, 1 turn remaining" for
five turns while the server's saved battle showed she had no Freeze at all. The client
does not count a server-round status down by itself; it keeps the icon until a row
with rounds=0 removes it (docs: status wire, 0 = remove, -1 = permanent). All three
expiry paths -- the actor's own tick in end_turn, the skipped-turn tick in
_start_of_turn, and _age_gauge_blocks -- were discarding tick_duration's result, so no
expiry ever produced a row. A control status can ONLY expire on the skipped-turn path
(its holder never acts), which is why this showed up as a permanently frozen boss.

The assertions are on the queued rows and on what _drain_pending_status attaches to
an outgoing attack, because that attachment is the whole of what the phone sees.

Uses its own SEVENSINS_ACCOUNTS tempdir so it never touches real save data.
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["SEVENSINS_ACCOUNTS"] = tempfile.mkdtemp(prefix="sevensins-expiry-test-")

import battle as bt                                            # noqa: E402
import player_state as ps                                      # noqa: E402
from engine import core as C                                   # noqa: E402
from engine import status as S                                 # noqa: E402

_fail = 0
FREEZE, HEADWIND = 602, 4105


def check(name, cond, detail=""):
    global _fail
    if not cond:
        _fail += 1
    print(f"  {'ok  ' if cond else 'FAIL'}  {name}" + (f"   [{detail}]" if detail else ""))


def a_battle():
    state = ps.load("expiry-tester")
    return bt.Battle(1101, ps.battle_team(state), 1, None, 0, 0)


def _ev(unit, status_id, name, duration):
    return C.StatusEvent(target=unit.order, status_id=status_id, name=name, applied=True,
                         duration=duration, magnitude=None, stacks=None,
                         unknown_duration=False, permanent=False)


def _rows_for(b, unit, status_id):
    return [r for r in b._pending_status_rows
            if r.get("target") == unit.order and r.get("status_id") == status_id]


def _drained(b):
    """Attach the queue to a fake outgoing attack -> the [target, sid, rounds] rows."""
    combo = {"data": [[{"target": "x"}]]}
    b._drain_pending_status(combo)
    return combo["data"][0][0].get("status", [])


def check_skipped_turn_expiry_is_sent():
    """The path a Freeze actually expires on: the holder's skipped turn."""
    b = a_battle()
    enemy = next(u for u in b.units.values() if u.team == bt.TEAM_ENEMY)
    S.apply_event(enemy, _ev(enemy, FREEZE, "Freeze", 1))
    check("Freeze is on the enemy", any(s.status_id == FREEZE for s in enemy.statuses))
    b._pending_status_rows = []
    # Put the frozen unit at the front of the queue and start its turn.
    for u in b.units.values():
        u.scv = 0.0
    enemy.scv = float(bt.SCV_FULL)
    b._roll_turn_order()
    check("the frozen enemy is next to act", b.acting_unit() is enemy,
          str(getattr(b.acting_unit(), "order", None)))
    b._start_of_turn()
    check("the skipped turn expired the 1-turn Freeze",
          not any(s.status_id == FREEZE for s in enemy.statuses))
    rows = _rows_for(b, enemy, FREEZE)
    check("a removal row was queued for it", len(rows) == 1 and rows[0]["applied"] is False,
          str(rows))
    wire = _drained(b)
    sid = S.wire_status_id(FREEZE)
    check("the next attack carries [enemy, Freeze, 0]", [enemy.order, sid, 0] in wire,
          str(wire))


def check_gauge_block_expiry_is_sent():
    """Headwind ages on the battle's clock, off its own path -- same rule applies."""
    b = a_battle()
    unit = next(u for u in b.units.values() if u.team != bt.TEAM_ENEMY)
    S.apply_event(unit, _ev(unit, HEADWIND, "Headwind", 1))
    check("Headwind is on the unit and blocks gauge gain",
          S.blocks_gauge_gain(unit))
    b._pending_status_rows = []
    b._age_gauge_blocks()
    check("the battle clock expired the 1-turn Headwind", not S.blocks_gauge_gain(unit))
    rows = _rows_for(b, unit, HEADWIND)
    check("a removal row was queued for it", len(rows) == 1 and rows[0]["applied"] is False,
          str(rows))


def check_a_surviving_status_sends_nothing():
    """Only EXPIRY sends: a 3-turn status ticking to 2 must not spam a row."""
    b = a_battle()
    enemy = next(u for u in b.units.values() if u.team == bt.TEAM_ENEMY)
    S.apply_event(enemy, _ev(enemy, FREEZE, "Freeze", 3))
    b._pending_status_rows = []
    for u in b.units.values():
        u.scv = 0.0
    enemy.scv = float(bt.SCV_FULL)
    b._roll_turn_order()
    b._start_of_turn()
    still = [s for s in enemy.statuses if s.status_id == FREEZE]
    check("the 3-turn Freeze is still held with 2 left",
          len(still) == 1 and still[0].remaining == 2, str(still))
    check("no removal row was queued", not _rows_for(b, enemy, FREEZE))


def main():
    for fn in (check_skipped_turn_expiry_is_sent,
               check_gauge_block_expiry_is_sent,
               check_a_surviving_status_sends_nothing):
        print(f"\n{fn.__name__}:")
        fn()
    print(f"\n{_fail} failure(s)")
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(main())

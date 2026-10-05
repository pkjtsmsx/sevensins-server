#!/usr/bin/env python3
"""A status applied AND expired between two attacks must send neither row.

    python3 test_status_pair_drop.py

`_queue_status_rows` collapses to the latest state per (unit, status), which is right for a
marker re-granting itself every turn. It was wrong for one case: a status granted and then
expired inside the gap between two attack replies collapsed to a BARE REMOVAL for something
the client was never told about -- and `removeStatusDataByID` (0x197C3D0) on an id it does
not hold calls `ServerRPCReportError`, which is where the fight stops.

That is why a hang got likelier the longer a wave ran: every turn is another chance for a
short status to be granted and expire inside one gap, and one pair is enough.

The fix must NOT over-reach. A removal for a status the client IS drawing is exactly what it
needs; dropping that would swap a hang for a stale icon. `_status_told` is what separates
the two, and it is fed from every status row that goes out -- the engine's own in-attack
rows included, since those are told the same way.
"""
import os
import random
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
os.environ["SEVENSINS_ACCOUNTS"] = tempfile.mkdtemp(prefix="sevensins-pair-")

import ai_arena as A                                           # noqa: E402
import battle as bt                                            # noqa: E402

FAILURES = []


def check(cond, msg):
    if not cond:
        FAILURES.append(msg)


def _fresh():
    b = A.stage_battle(1101, [10001] * 5, 60, 1)
    b._pending_status_rows = []
    b._status_told = set()
    return b


def _q(b):
    return [(q["target"], q["status_id"], q["applied"]) for q in b._pending_status_rows]


def an_untold_pair_sends_nothing():
    b = _fresh()
    b._queue_status_rows([{"target": "201", "status_id": 2007,
                           "applied": True, "duration": 1}])
    check(_q(b) == [("201", 2007, True)], f"the apply did not queue: {_q(b)}")
    b._queue_status_rows([{"target": "201", "status_id": 2007, "applied": False}])
    check(_q(b) == [],
          f"a BARE REMOVAL survived for a status the client never heard of: {_q(b)} -- "
          f"removeStatusDataByID on an unknown id is ServerRPCReportError")


def a_told_status_keeps_its_removal():
    b = _fresh()
    b._status_told.add(("201", 2007))
    b._queue_status_rows([{"target": "201", "status_id": 2007,
                           "applied": True, "duration": 1}])
    b._queue_status_rows([{"target": "201", "status_id": 2007, "applied": False}])
    check(_q(b) == [("201", 2007, False)],
          f"the removal was dropped for a status the client IS drawing: {_q(b)} -- that "
          f"trades the hang for a stale icon")


def the_other_shapes_are_untouched():
    # an ordinary apply
    b = _fresh()
    b._queue_status_rows([{"target": "202", "status_id": 2009,
                           "applied": True, "duration": 3}])
    check(_q(b) == [("202", 2009, True)], f"an ordinary apply was dropped: {_q(b)}")
    # a bare removal with NO prior queued apply -- the frozen-boss icon case, where the
    # client is drawing a status it could not decrement because its turn was skipped.
    b = _fresh()
    b._queue_status_rows([{"target": "203", "status_id": 2007, "applied": False}])
    check(_q(b) == [("203", 2007, False)],
          f"the skipped-unit expiry row was dropped: {_q(b)} -- that is the frozen-boss "
          f"icon coming back")
    # repeats still collapse
    b = _fresh()
    for _ in range(4):
        b._queue_status_rows([{"target": "204", "status_id": 2009,
                               "applied": True, "duration": 2}])
    check(len(b._pending_status_rows) == 1,
          f"repeats stopped collapsing: {len(b._pending_status_rows)} rows queued")


def an_attacks_own_rows_count_as_told():
    """Most statuses are applied INSIDE an attack and ride the engine's own rows.

    If only out-of-band applies were remembered, a status granted in an attack and then
    re-granted and expired inside one gap would look un-told and lose its removal.
    """
    b = A.mirror_battle((10051, 10051, 10051, 10051, 10051), 200, party=None)
    b._status_told = set()
    b.attack_cmd_json("101", "201", 1005125, rng=random.Random(9))
    check(b._status_told,
          "an attack that applies statuses recorded nothing as told -- the in-attack rows "
          "are not being read")
    key = sorted(b._status_told)[0]
    b._pending_status_rows = []
    b._queue_status_rows([{"target": key[0], "status_id": key[1],
                           "applied": True, "duration": 1}])
    b._queue_status_rows([{"target": key[0], "status_id": key[1], "applied": False}])
    check(_q(b) == [(key[0], key[1], False)],
          f"a status the ATTACK told the client about lost its removal: {_q(b)}")


def a_wave_advance_forgets_recycled_orders():
    """`_spawn_wave` reuses orders, so wave N's bookkeeping must not outlive it."""
    b = _fresh()
    b._status_told.add(("201", 2007))
    if not b.has_next_wave():
        check(True, "")
        return
    b.advance_wave()
    check(not b._status_told,
          f"wave {b.wave} inherited the previous wave's told-set {sorted(b._status_told)} "
          f"-- order 201 is a different unit now")


def main():
    an_untold_pair_sends_nothing()
    a_told_status_keeps_its_removal()
    the_other_shapes_are_untouched()
    an_attacks_own_rows_count_as_told()
    a_wave_advance_forgets_recycled_orders()
    for f in FAILURES:
        if f:
            print("FAIL:", f)
    real = [f for f in FAILURES if f]
    print(f"{len(real)} failure(s)")
    return 1 if real else 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""The wave-1 -> wave-2 order-aliasing soft-lock (UserContrib wave-boundary-test).

Enemy orders are RECYCLED: wave 2's front-row enemy has the same order string wave 1's
had. A death or status row queued on the turn that clears a wave used to drain onto the
first attack of the NEXT one, landing on whichever fresh unit inherited the order --
the client killed a living unit and the fight hung. The wave stamp refuses those rows.

Run against the pre-fix tree, both checks fail.
"""
import json
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="sevensins-waveb-test-")
os.environ["SEVENSINS_ACCOUNTS"] = _TMP
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import battle as bt                                     # noqa: E402
import player_state as ps                               # noqa: E402

_fail = 0


def check(name, cond, detail=""):
    global _fail
    if cond:
        print(f"  [PASS] {name}")
    else:
        _fail += 1
        print(f"  [FAIL] {name}   {detail}")


def main():
    state = ps.load("wave-boundary")
    b = bt.Battle(11, ps.battle_team(state), 1, None, 0, 0)
    victims = sorted(o for o, u in b.units.items() if u.team == bt.TEAM_ENEMY)
    check("stage 11 fields multiple waves", b.wave_max >= 2, str(b.wave_max))
    victim = victims[0]
    # What the turn that clears a wave leaves behind: a queued death (DoT tick or an
    # after-action passive kill) and a queued status row.
    b._pending_dot_deaths.append({"order": victim, "dmg": 4242, "wave": b.wave})
    b._queue_status_rows([{"target": victim, "status_id": 701,
                           "applied": True, "duration": 3}])
    b.advance_wave()
    fresh = b.units.get(victim)
    check("the recycled order names a living fresh enemy",
          fresh is not None and fresh.alive)
    msgs = b.dot_death_cmds_json()
    check("no death from the previous wave reaches the new one", msgs == [],
          str([json.loads(m)["combo"][0]["caster"] for m in msgs]))
    combo = {"caster": "101", "skill": 1,
             "data": [[{"c": victim, "md": 1, "cg": 0, "dmg": 0, "cri": 0, "die": 0,
                        "status": [], "extra": [], "picons": [], "pskill_id": 0}]]}
    b._drain_pending_status(combo)
    stale = combo["data"][0][0]["status"]
    check("no stale status row rides the next wave's first attack", not stale,
          str(stale))
    print(f"\n{_fail} failure(s)")
    return 1 if _fail else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        import shutil
        shutil.rmtree(_TMP, ignore_errors=True)

#!/usr/bin/env python3
"""A nested script that fires on its holder's OWN action, not once per global turn.

    python3 test_nested_own_action.py

THE BUG. `status.run_nested` fires every living unit's nested scripts once per global
turn. That reading is right for most of them -- Lucifer's The Fallen says "at EACH START
OF A TURN" -- and it was applied to all 428, which is the one-observation-generalised
trap. 邪眼詛咒 (407, "The Curse") states something different:

    自身行動結束時，造成狀態擁有者本身攻擊力150%傷害，並50%固定機率消除自身全傷害激減、
    50%固定機率對自身附加逆風，50%固定機率對自身附加石化

"WHEN THE HOLDER'S OWN ACTION ENDS ... 50% chance to inflict Headwind on itself."

Fired once per global turn instead, it cannot unwind: the Headwind blocks the holder's
move gauge, the holder never acts, and the script renews the Headwind anyway. A unit sat
out a 44-turn fight at FULL HP -- caught by the fuzzer's starvation invariant, and
reproducible at seed 910980.

Anchored to behaviour: the checks drive real turns and count them. A gate that exists in
data but is not consulted passes a table assertion and fails these.
"""
import os
import random
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
os.environ["SEVENSINS_ACCOUNTS"] = tempfile.mkdtemp(prefix="sevensins-nested-")

import battle as bt                                            # noqa: E402
import design_data as dd                                       # noqa: E402
from engine import status as est                                # noqa: E402

FAILURES = []


def check(cond, msg):
    if not cond:
        FAILURES.append(msg)


class _U:
    def __init__(self, order="p1", team=1):
        self.statuses, self.order, self.team = [], order, team
        self.alive, self.hp, self.max_hp = True, 1000, 1000
        self.atk = 1000


def _hold(unit, sid):
    row = est._registry(sid) or {}
    unit.statuses.append(est.Active(status_id=sid, name=row.get("name"), remaining=5,
                                    kind=row.get("kind"), category=row.get("category")))


def the_curse_fires_only_on_its_holders_action():
    check(407 in est.OWN_ACTION_NESTED, "407 is not gated on the holder's own action")

    # The global per-turn sweep must do NOTHING for it.
    u = _U()
    _hold(u, 407)
    before = {s.status_id for s in u.statuses}
    for _ in range(20):
        est.run_nested(u)                     # own_action defaults False
    check({s.status_id for s in u.statuses} == before,
          f"the per-turn sweep fired The Curse: "
          f"{sorted({s.status_id for s in u.statuses} - before)}")

    # The holder's own action DOES fire it. 50% chances, so drive it until it lands.
    v = _U()
    _hold(v, 407)
    landed = set()
    for _ in range(60):
        est.run_nested(v, own_action=True)
        landed |= {s.status_id for s in v.statuses}
    check(4284 in landed, "The Curse never inflicted Headwind on its own action")
    check(613 in landed, "The Curse never inflicted Petrify on its own action")


def an_each_turn_script_is_untouched():
    """The 30 statuses that DO say 回合開始時 must keep firing on the global sweep."""
    from engine import specs
    rows = specs.statuses() or {}
    each = sorted(int(sid) for sid, row in rows.items()
                  if (row.get("nested") or [])
                  and int(sid) not in est.OWN_ACTION_NESTED)
    check(each, "no recurring nested script found to compare against")
    fired = 0
    for sid in each[:40]:
        u = _U()
        _hold(u, sid)
        before = {s.status_id for s in u.statuses}
        for _ in range(4):
            est.run_nested(u)
        if {s.status_id for s in u.statuses} != before:
            fired += 1
    check(fired, "no recurring nested script fired on the per-turn sweep any more -- "
                 "the gate has inverted the default")


def nothing_fires_twice_in_one_turn():
    """The two call sites must partition the scripts, not overlap."""
    for sid in list(est.OWN_ACTION_NESTED):
        u = _U()
        _hold(u, sid)
        n_sweep = len(est.run_nested(u))
        check(n_sweep == 0, f"status {sid} fired on BOTH call sites (sweep produced "
                            f"{n_sweep} rows)")


def the_prose_still_says_own_action():
    """Re-read the pack, so a change to 407's wording fails here rather than silently."""
    for sid in list(est.OWN_ACTION_NESTED):
        zh = (dd.row("skill", sid) or {}).get("_note1") or ""
        check(re.search(r"自身行動結束時|自身行動後|行動結束時", zh),
              f"status {sid} no longer states the holder's own action: {zh[:60]}")


def the_starved_unit_now_acts():
    """The fight the fuzzer found, end to end."""
    import ai_arena as A
    team = (10531, 11191, 10081, 20921, 10121)
    party = A.loadout_party(team, 400, random.Random(980517066), 1.0)
    check(party, "the loadout for the repro could not be built")
    b = A.mirror_battle(team, 400, party=party)
    rng = random.Random(910980)
    acted = {}
    for _turn in range(50):
        if not (b.team_alive(1) and b.team_alive(2)):
            break
        actor = b.acting_unit()
        if actor is None:
            break
        move = A.CHOOSERS["tier1-enemy"](b, 2 if actor.team == 1 else 1)
        if not move:
            break
        att, dfn, skill_id, slot = move
        acted[att] = acted.get(att, 0) + 1
        at, tg = b.units.get(att), b.units.get(dfn)
        if at is not None and tg is not None:
            tg = b._forced_target(at) or tg
        b.attack_cmd_json(att, tg.order if tg else dfn, skill_id, rng=rng)
        b.spend_skill(att, slot)
        b.end_turn()
    u = b.units.get("201")
    check(u is not None, "unit 201 vanished from the repro")
    check(acted.get("201", 0) > 0,
          f"unit 201 still never acts (it holds "
          f"{[s.name for s in getattr(u, 'statuses', [])]})")
    check(not est.blocks_gauge_gain(u),
          "unit 201's move gauge is still permanently blocked")


def main():
    the_curse_fires_only_on_its_holders_action()
    an_each_turn_script_is_untouched()
    nothing_fires_twice_in_one_turn()
    the_prose_still_says_own_action()
    the_starved_unit_now_acts()
    for f in FAILURES:
        print("FAIL:", f)
    print(f"{len(FAILURES)} failure(s)")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())

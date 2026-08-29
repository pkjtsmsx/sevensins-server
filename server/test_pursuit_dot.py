#!/usr/bin/env python3
"""Pursuit damage, and the start-of-turn DoT kill that soft-locked the client.

    python3 test_pursuit_dot.py

Both reported from a phone 2026-08-29.

**DoT.** A unit whose own turn opened with lethal poison died server-side and the client
was never told: `_start_of_turn` moved HP directly and emitted nothing, and `die` is set
nowhere on the wire except the attack-combo builder. The client runs its own action order
(BattleUnitManager.GetNextAction re-derives it from `sync`), so it sat waiting for a turn
the dead unit would never take. Retail played the tick on that unit's own turn -- damage,
death, then the next character.

**Pursuit.** A pursuit sub-skill's design row carries NO numbers (100000341 is
`_note1_jp` "脊砕き 追加技能" and zeroed columns), so `formula.strike` got a None
coefficient and skipped: every pursuit fired and dealt nothing, 1,190 of 1,282 corpus-wide.
The figures are in the PARENT's prose -- 追擊(造成120%攻擊力傷害), and 以50%機率追擊 for
the odds, which nothing read either, so 240 casts pursued on every single cast.

Anchored to behaviour, not to constants: the assertions are on damage actually dealt, on
the wire rows the client would receive, and on a roll's observed rate.

Uses its own SEVENSINS_ACCOUNTS tempdir so it never touches real save data.
"""
import os
import random
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["SEVENSINS_ACCOUNTS"] = tempfile.mkdtemp(prefix="sevensins-pursuit-test-")

import json                                                    # noqa: E402
import battle as bt                                            # noqa: E402
import player_state as ps                                      # noqa: E402
from engine import core as C                                   # noqa: E402
from engine import specs, status as S, wire as W               # noqa: E402

_fail = 0
SPINE_BREAK = 100000301          # 若此攻擊暴擊時，以50%機率追擊(造成120%攻擊力傷害)


def check(name, cond, detail=""):
    global _fail
    if not cond:
        _fail += 1
    print(f"  {'ok  ' if cond else 'FAIL'}  {name}" + (f"   [{detail}]" if detail else ""))


def a_battle(who="pursuit-tester"):
    state = ps.load(who)
    return bt.Battle(1101, ps.battle_team(state), 1, None, 0, 0)


def poison(unit, magnitude, source_atk):
    unit.statuses.append(S.Active(status_id=901, name="Poison", kind="dot",
                                  category="debuff", remaining=5,
                                  magnitude=float(magnitude), source_atk=source_atk))


# -- DoT ---------------------------------------------------------------------------

def check_dot_death_reaches_the_wire():
    b = a_battle("dot-wire")
    victim = b.units["101"]
    hp0 = victim.hp
    poison(victim, 40.0, 99999)                 # far more than the pool
    b.attack_cmd_json("102", "103", 0)          # 102 acts; 101 is next up
    b.end_turn()                                # ...and dies on its own turn start

    check("the lethal tick kills the unit", not victim.alive)
    msgs = b.dot_death_cmds_json()
    check("a death produces exactly one wire message", len(msgs) == 1, f"{len(msgs)}")
    if not msgs:
        return
    cmd = json.loads(msgs[0])
    rows = cmd["combo"][0]["data"][0]
    check("the dying unit is the combo's caster", cmd["combo"][0]["caster"] == "101")
    check("the row names the dying unit", rows[0]["c"] == "101")
    # THE assertion: without this the client never learns the unit died, keeps it in its
    # own action order, and waits for a turn that never comes.
    check("the row carries die=1", rows[0]["die"] == 1)
    check("damage is what was actually removed, not the raw tick",
          rows[0]["dmg"] == hp0, f"{rows[0]['dmg']} vs pool {hp0}")
    check("the dead unit is gone from the turn line", "101" not in cmd["line"])
    try:
        W._assert_invariants(cmd["combo"][0]["data"], 1)
        check("the payload satisfies the wire invariants", True)
    except W.WireError as exc:
        check("the payload satisfies the wire invariants", False, str(exc))
    check("the queue drains, so a death is never sent twice",
          b.dot_death_cmds_json() == [])


def check_survivable_dot_sends_nothing():
    """A tick the unit survives must NOT produce a message -- it would put an extra
    Perform in front of a unit that has not acted yet."""
    b = a_battle("dot-survive")
    victim = b.units["101"]
    poison(victim, 1.0, 10)                     # a scratch
    b.attack_cmd_json("102", "103", 0)
    b.end_turn()
    check("a survived tick queues no message",
          victim.alive and b.dot_death_cmds_json() == [])


# -- pursuit -----------------------------------------------------------------------

def check_pursuit_has_its_numbers():
    spec = specs.skill(SPINE_BREAK)
    fu = [e for e in (spec.get("effects") or []) if e["op"] == "follow_up"]
    check("Spine Break compiles a follow_up", len(fu) == 1)
    if not fu:
        return
    e = fu[0]
    # Read from the parent's Chinese: 追擊(造成120%攻擊力傷害) and 以50%機率.
    check("the pursuit carries the coefficient from the parent's prose",
          e.get("coefficient") == 1.2, repr(e.get("coefficient")))
    check("the pursuit carries the stated odds",
          e.get("chance_pct") == 50.0, repr(e.get("chance_pct")))


def check_pursuit_actually_deals_damage():
    """The bug was not that the pursuit failed to fire -- it always fired. It dealt
    nothing, because the sub-skill had no coefficient and strike() returned None."""
    b = a_battle("pursuit-dmg")
    units = list(b.units.values())
    caster = b.units["101"]
    caster.atk = 1000
    spec = specs.skill(SPINE_BREAK)
    r = random.Random(11)
    amounts = []
    for _ in range(300):
        out = C.execute(caster, spec, units, r, chosen="103", apply_damage=False)
        for child in out.children:
            amounts.extend(s.amount for s in child.strikes)
    check("the pursuit lands real damage", bool(amounts) and all(a > 0 for a in amounts),
          f"{len(amounts)} strikes")
    if amounts:
        # 120% of ATK 1000 before mitigation and variance. A wide band on purpose: the
        # point is that it is ~1200-scaled, not that the formula is pinned here.
        avg = sum(amounts) / len(amounts)
        check("it scales as ~120% ATK, not as some default", 600 <= avg <= 2600,
              f"avg {avg:.0f}")


def check_pursuit_rolls_its_chance():
    b = a_battle("pursuit-roll")
    units = list(b.units.values())
    caster = b.units["101"]
    caster.atk = 1000
    spec = specs.skill(SPINE_BREAK)
    r = random.Random(5)
    casts = 600
    fired = sum(1 for _ in range(casts)
                if C.execute(caster, spec, units, r, chosen="103",
                             apply_damage=False).children)
    rate = fired / casts
    # Was 1.0 before the fix: nothing read the odds and the branch never rolled.
    check("a 50% pursuit fires about half the time", 0.35 <= rate <= 0.65,
          f"{fired}/{casts} = {rate:.0%}")


def main():
    print("pursuit + start-of-turn DoT")
    check_dot_death_reaches_the_wire()
    check_survivable_dot_sends_nothing()
    check_pursuit_has_its_numbers()
    check_pursuit_actually_deals_damage()
    check_pursuit_rolls_its_chance()
    print(f"\n{_fail} failure(s)")
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(main())

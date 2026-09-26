#!/usr/bin/env python3
"""Recovered pursuit damage: the follow-up must actually land a number.

    python3 test_pursuit_values.py

608 parent skills reached a `follow_up` whose coefficient was stated in NEITHER the
parent's compiled effect nor the sub-skill's row, so `formula.strike` got None and the
pursuit played its animation for 0 damage. engine/pursuit_values.py carries the figures
the Chinese prose states; 439 of the contributed 488 survived re-verification.

Anchored to BEHAVIOUR: each check runs the engine and asserts the child Outcome carries a
non-zero strike, and that the strike scales with the basis the table names -- a DEF
pursuit read as ATK is the failure this is really guarding, and it cannot be seen by
asserting the table's own contents.
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["SEVENSINS_ACCOUNTS"] = tempfile.mkdtemp(prefix="sevensins-pursuit-test-")

import battle as bt                                            # noqa: E402
from engine import core as ecore                                # noqa: E402
from engine import pursuit_values as pv                         # noqa: E402
from engine import specs                                        # noqa: E402

FAILURES = []


def check(cond, msg):
    if not cond:
        FAILURES.append(msg)


def _pursuit_parents():
    """Parents that reach a follow_up with no coefficient of their own, by basis."""
    sk = specs.skills()
    out = {}
    for sid, spec in sk.items():
        got = pv.lookup(sid)
        if not got:
            continue
        for e in spec.get("effects") or []:
            if e.get("op") == "follow_up" and e.get("coefficient") is None:
                sub = sk.get(e.get("skill")) or {}
                coefs = [x.get("coefficient") for x in (sub.get("effects") or [])
                         if x.get("op") == "damage"]
                if coefs and all(c is None for c in coefs):
                    out.setdefault(got[1], []).append(sid)
                break
    return out


def _fight(parent_id, atk, defence):
    """Run one skill against one target. -> total damage dealt by the CHILD outcomes."""
    caster = bt.Unit(order="p1", char_id=10001, team=1, index=0, lv=50)
    target = bt.Unit(order="e1", char_id=10001, team=2, index=0, lv=50)
    caster.atk, caster.defence = atk, defence
    caster.hp = caster.max_hp = 500000
    target.hp = target.max_hp = 50_000_000      # survive, so nothing is clamped by death
    target.defence = 0
    spec = specs.skills()[parent_id]
    import random
    out = ecore.execute(caster, spec, [caster, target], rng=random.Random(1),
                        chosen="e1", round_no=1)

    def _walk(o):
        total = 0
        for s in o.strikes:
            total += s.amount or 0
        for c in o.children:
            total += _walk(c)
        return total
    child = sum(_walk(c) for c in out.children)
    return child


def pursuits_land_a_number():
    by_basis = _pursuit_parents()
    check(by_basis, "no parent skill matches a recovered pursuit -- wiring is dead")
    for basis in ("ATK", "DEF"):
        parents = by_basis.get(basis) or []
        check(parents, f"no recovered pursuit scales off {basis}")
        landed = 0
        for pid in parents[:40]:
            if _fight(pid, atk=10_000, defence=10_000) > 0:
                landed += 1
        check(landed, f"every {basis} pursuit sampled still landed 0 damage")


def the_basis_travels_with_the_figure():
    """A DEF pursuit must move when DEF moves and NOT when only ATK moves."""
    by_basis = _pursuit_parents()
    for basis, other in (("DEF", "ATK"), ("ATK", "DEF")):
        for pid in (by_basis.get(basis) or [])[:25]:
            lo = _fight(pid, atk=10_000, defence=10_000)
            if lo <= 0:
                continue
            kw = {"atk": 10_000, "defence": 10_000}
            kw[{"DEF": "defence", "ATK": "atk"}[basis]] = 40_000
            hi = _fight(pid, **kw)
            kw2 = {"atk": 10_000, "defence": 10_000}
            kw2[{"DEF": "defence", "ATK": "atk"}[other]] = 40_000
            flat = _fight(pid, **kw2)
            check(hi > lo,
                  f"{pid}: pursuit is {basis}-scaled but raising {basis} changed nothing")
            check(flat == lo,
                  f"{pid}: pursuit is {basis}-scaled but raising {other} moved it "
                  f"({lo} -> {flat}) -- the basis is being read as {other}")
            break
        else:
            FAILURES.append(f"no usable {basis} pursuit to scale-test")


def only_fills_a_gap():
    """A parent that states its own figure must keep it."""
    sk = specs.skills()
    for sid, spec in sk.items():
        for e in spec.get("effects") or []:
            if e.get("op") == "follow_up" and e.get("coefficient") is not None:
                check(True, "")
                return
    FAILURES.append("no follow_up states its own coefficient -- premise stale")


def two_pursuits_are_told_apart():
    """A parent with two different pursuit figures must pay each its own.

    Judge's Mercy pursues twice -- 35% ATK via Blazing Arcanum, then 90% via Moon Trice.
    The contributed table was keyed by parent, so both were paid 90%: the first pursuit
    ran 2.57x hot. Caught by measuring pursuit vs main-hit damage before shipping.
    """
    for (pid, sub), (coef, _b) in pv.PURSUIT_BY_SUB.items():
        check(pid not in pv.PURSUIT,
              f"{pid} is in BOTH tables; the parent-keyed figure would win for some subs")
        check(pv.lookup(pid, sub) == (coef, _b), f"{pid}/{sub} lookup lost its override")
    # The two figures really do differ, or this whole table is pointless.
    pairs = {}
    for (pid, sub), val in pv.PURSUIT_BY_SUB.items():
        pairs.setdefault(pid, set()).add(val)
    for pid, vals in pairs.items():
        check(len(vals) > 1, f"{pid} has identical overrides -- it belongs in PURSUIT")
    # And the structural guard must flag a parent-keyed entry for such a skill.
    sys.path.insert(0, os.path.join(HERE, "..", "tools"))
    import verify_pursuit_values as vp
    for pid in pairs:
        check(len(vp.coefficientless_subskills(pid)) > 1,
              f"{pid} no longer drives two pursuits -- PURSUIT_BY_SUB entry is stale")


def table_is_still_verified():
    """The table must stay derivable from the pack, not just present."""
    sys.path.insert(0, os.path.join(HERE, "..", "tools"))
    import verify_pursuit_values as vp
    bad = [(sid, c, b) for sid, (c, b) in pv.PURSUIT.items()
           if not vp.verify(sid, c, b)[0]]
    check(not bad, f"{len(bad)} table entries no longer verify against the Chinese prose: "
                   f"{bad[:5]}")


def main():
    pursuits_land_a_number()
    two_pursuits_are_told_apart()
    the_basis_travels_with_the_figure()
    only_fills_a_gap()
    table_is_still_verified()
    for f in FAILURES:
        if f:
            print("FAIL:", f)
    real = [f for f in FAILURES if f]
    print(f"{len(real)} failure(s)")
    return 1 if real else 0


if __name__ == "__main__":
    sys.exit(main())

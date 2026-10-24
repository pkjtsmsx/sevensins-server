#!/usr/bin/env python3
"""A passive shaped around its OWN holder being downed has to fire.

    python3 test_own_death_passives.py

`fire_all` skipped every dead holder, which is right for every trigger but one: 65
passives key on 被擊倒時 -- Gabriel's Purity First grants the party All DMG Reduction when
she falls, Astaroth's Emotional Patchwork drains the enemy gauge -- and the holder is
necessarily dead by the time `core` fires ON_DEATH for it. The rules compiled, built and
were dropped one line before being read.

Anchored to BEHAVIOUR, and the second check is the one that matters: the exemption must be
the VICTIM's alone. A corpse answering someone else's death would be a worse bug than the
one being fixed, so that case is asserted directly rather than assumed from the code.
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["SEVENSINS_ACCOUNTS"] = tempfile.mkdtemp(prefix="sevensins-ondeath-test-")

import battle as bt                                            # noqa: E402
from engine import passives as P                                # noqa: E402

GABRIEL, ESMIRA = 10161, 10401
FAILURES = []


def check(cond, msg):
    if not cond:
        FAILURES.append(msg)


def _fire(victim_is_holder):
    """-> what ON_DEATH applied, with Gabriel dead either way.

    `victim_is_holder` picks whose death is being announced: hers, or her ally's.
    """
    b = bt.Battle(1101, [GABRIEL, ESMIRA])
    gabriel, ally = b.units["101"], b.units["102"]
    gabriel.hp = 0
    victim = gabriel if victim_is_holder else ally
    units = list(b.units.values())
    applied = P.fire_all(P.ON_DEATH, units, units, ctx={"victim": victim})
    return [getattr(x, "name", x) for _u, *rest in applied for x in rest]


def _passive_names(char_id):
    """The ON_DEATH statuses this cast's passives grant, read from the compiled rules."""
    b = bt.Battle(1101, [char_id])
    unit = b.units["101"]
    out = set()
    for sid in unit.skills or []:
        for rule in P.rules_for(sid):
            if rule.trigger == P.ON_DEATH and rule.status:
                out.add(rule.status)
    return out


def her_own_death_fires_her_passive():
    expected = _passive_names(GABRIEL)
    check(expected, "Gabriel has no ON_DEATH status rule at all -- wrong fixture")
    got = _fire(victim_is_holder=True)
    for name in expected:
        check(name in got,
              f"Gabriel died and her own {name!r} did not fire: {got}")


def someone_elses_death_does_not():
    """The exemption is the victim's. A corpse must not answer an ally's death."""
    expected = _passive_names(GABRIEL)
    got = _fire(victim_is_holder=False)
    for name in expected:
        check(name not in got,
              f"a dead Gabriel fired {name!r} on an ALLY's death: {got}")


def the_living_still_fire_either_way():
    """Guard against fixing this by dropping the alive check altogether."""
    on_hers = _fire(victim_is_holder=True)
    on_allys = _fire(victim_is_holder=False)
    check(on_allys, "no passive fired at all for an ally's death")
    check(len(on_hers) > len(on_allys),
          f"her own death added nothing over an ally's: {on_hers} vs {on_allys}")


def main():
    for fn in (her_own_death_fires_her_passive,
               someone_elses_death_does_not,
               the_living_still_fire_either_way):
        fn()
    for f in FAILURES:
        print("  FAIL", f)
    print(f"\n{'all OK' if not FAILURES else str(len(FAILURES)) + ' failure(s)'}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""The engine handles every effect the compiled artifact carries -- or says which it does not.

`tools/effect_coverage.py` probes each (op, trigger) cell by running the real engine on a
synthetic one-effect spec and diffing the Outcome against a run without it. This holds the
result to a line, so the gap set cannot grow quietly.

**The bug class this exists for.** The compiled vocabulary is dispatched in two
independent places -- `core.execute` for a skill being USED, `passives._compiled_rules`
for one being HELD -- and nothing kept them in step. `core.execute` handled all nine ops;
the passive path handled five, and `damage` on one trigger out of five. 261 compiled
passive damage effects, every counterattack in the game among them, were read off disk,
carried through the spec and dropped. That was found by accident. This is the net.

    python3 test_effect_coverage.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tools"))

import effect_coverage as ec                 # noqa: E402

# The gaps we KNOW about, as (path, op, trigger). Every one is the passive path failing
# to express something `core.execute` does fine.
#
# This is a ratchet, and it is asserted EXACTLY rather than as an upper bound: closing a
# gap should fail this test too, so that whoever closes one updates the record here and
# in their commit. Shrinking this set is the goal; growing it silently is the thing being
# prevented.
#
#   remove_status -- `Rule` has no removal effect at all. 361 effects. A passive that
#     cleanses on a trigger ("at the start of the battle, remove all debuffs") does
#     nothing today.
#   damage -- handled for `on_damage_taken` only, which is the counterattack shape. The
#     other four triggers carry 223 effects and are NOT a missing branch so much as a
#     missing decision: every compiled damage effect has `target: null`, and only a
#     counter's prose says who to hit. Picking a recipient for the rest would be
#     inventing one. See engine/passives.py.
KNOWN_GAPS = {
    ("passive", "remove_status", "battle_start"),
    ("passive", "remove_status", "turn_start"),
    ("passive", "remove_status", "after_action"),
    ("passive", "remove_status", "on_damage_dealt"),
    ("passive", "remove_status", "on_damage_taken"),
    ("passive", "remove_status", "on_death"),
    ("passive", "damage", "battle_start"),
    ("passive", "damage", "turn_start"),
    ("passive", "damage", "after_action"),
    ("passive", "damage", "on_damage_dealt"),
}

_fail = 0


def check(label, ok, detail=""):
    global _fail
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f" -- {detail}"))
    if not ok:
        _fail += 1


def main():
    rows, gaps, starved, total = ec.coverage()
    found = {(path, op, trigger) for path, op, trigger, _n, _s in gaps}

    print("\ncheck_no_active_effect_is_dropped:")
    # The strong one. Every op the artifact carries is dispatched when a skill is USED,
    # and that must stay true -- it is the path every fight in the game runs.
    active = {g for g in found if g[0] == "active"}
    check("core.execute handles every complete effect it is given",
          not active, str(sorted(active)))

    print("\ncheck_the_passive_gap_set_is_exactly_what_we_recorded:")
    new = found - KNOWN_GAPS
    closed = KNOWN_GAPS - found
    check("no NEW gap has appeared", not new, f"undocumented: {sorted(new)}")
    check("  ...and none of the recorded ones closed without the record moving",
          not closed, f"fixed but still listed: {sorted(closed)} -- update KNOWN_GAPS")

    print("\ncheck_the_matrix_itself_still_looks_sane:")
    # Guards against the probe silently breaking and reporting a clean sheet. It has
    # done exactly that once: the first version called a function that does not exist
    # inside a bare `except`, so every passive cell came back "not handled" and the tool
    # reported 20,028 gaps. A probe that cannot fail is a probe that is not running.
    check("the artifact still has effects to probe", total > 40000, str(total))
    check("  ...across the expected number of cells", len(rows) >= 35, str(len(rows)))
    check("  ...and at least one cell is handled on each path",
          any(r[3] for r in rows) and any(r[4] for r in rows))
    check("starved effects are reported separately, not as engine gaps",
          sum(starved.values()) > 0 and not any(
              g[1] in starved and g[3] >= starved[g[1]] for g in gaps),
          str(dict(starved)))

    print(f"\n{_fail} failure(s)")
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(main())

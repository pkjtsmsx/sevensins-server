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

# The gaps we KNOW about, as (path, op, trigger), plus a ceiling on how many effects
# they cost. Both are asserted, and for different reasons:
#
#   * the SET catches a whole cell going dark -- a dispatch branch deleted or a trigger
#     renamed;
#   * the CEILING catches a cell quietly getting worse. Most cells here are PARTIAL: the
#     op is handled and some members still produce nothing, so the set alone would not
#     notice 3% becoming 30%.
#
# Asserted exactly rather than as an upper bound, so closing a gap fails this too and
# whoever closes one updates the record. Shrinking both numbers is the goal.
#
#   damage, four triggers -- handled for `on_damage_taken` (counterattacks) and for any
#     trigger where the effect's own `select` states a recipient. What is left states
#     none, and every compiled damage effect carries `target: null`, so paying them
#     would mean inventing who they hit.
#   apply_status / follow_up / attack_rider / heal, partial -- a minority of members
#     produce nothing. Not yet triaged one by one; the sampling is deterministic, so
#     these numbers are reproducible rather than noisy.
#
# CLOSED, kept as the record this file exists to be:
#   remove_status, all six passive triggers, 361 effects -- `Rule` had no removal effect
#     at all. Its recipient WAS answerable where damage's is not: the category settles
#     it, because clearing a debuff is a self-cleanse and clearing a buff is a strip.
KNOWN_GAPS = {
    ("active", "apply_status", None),
    ("active", "follow_up", None),
    ("active", "attack_rider", None),
    ("active", "heal", None),
    ("passive", "apply_status", "battle_start"),
    ("passive", "apply_status", "turn_start"),
    ("passive", "damage", "battle_start"),
    ("passive", "damage", "turn_start"),
    ("passive", "damage", "after_action"),
    ("passive", "damage", "on_damage_dealt"),
}

# A ceiling on MISSED SAMPLES, not on the extrapolated effect count.
#
# The gap list scales a cell's sample miss-rate onto the whole cell, which is right for
# RANKING and wrong for a ratchet: one extra miss in a 60-sample draw on the 15,700-strong
# `apply_status/None` cell moves the headline by ~260 effects. Recompiling the skills
# changed cell membership and swung that number 525 -> 1050 with nothing broken, which
# tripped an effects-based ceiling on pure noise.
#
# Missed samples are bounded (60 per cell, 40 cells) and move only when coverage really
# changes, so this is the number that can hold a line.
MAX_MISSED_SAMPLES = 210

_fail = 0


def check(label, ok, detail=""):
    global _fail
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f" -- {detail}"))
    if not ok:
        _fail += 1


def main():
    rows, gaps, starved, untargetable, total = ec.coverage()
    found = {(path, op, trigger) for path, op, trigger, _m, _n, _s in gaps}
    cost = sum(g[3] for g in gaps)

    print("\ncheck_no_whole_op_is_dark_on_the_active_path:")
    # Every op the artifact carries is DISPATCHED when a skill is used. The active gaps
    # recorded above are partial -- a minority of members of a cell -- so an op going
    # fully dark is a different and much worse thing, and it is what this catches.
    dark = {(op, trig) for op, trig, tot, act_ok, _p, _n, _s in rows
            if trig is None and act_ok == 0}
    check("core.execute produces something for every op", not dark, str(sorted(dark)))

    print("\ncheck_the_gap_set_is_exactly_what_we_recorded:")
    new = found - KNOWN_GAPS
    closed = KNOWN_GAPS - found
    check("no NEW gap has appeared", not new, f"undocumented: {sorted(new)}")
    check("  ...and none of the recorded ones closed without the record moving",
          not closed, f"fixed but still listed: {sorted(closed)} -- update KNOWN_GAPS")

    print("\ncheck_the_gaps_are_not_quietly_getting_worse:")
    # The set cannot see a PARTIAL cell degrading, which is most of them.
    missed = 0
    for _op, trig, _tot, act_ok, pas_ok, n, _sid in rows:
        if trig is None:
            missed += n - act_ok
        if pas_ok is not None:
            missed += n - pas_ok
    check(f"at most {MAX_MISSED_SAMPLES} probe samples miss",
          missed <= MAX_MISSED_SAMPLES, f"now {missed} (gap list reads {cost} effects)")

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
          sum(starved.values()) > 0, str(dict(starved)))
    check("  ...and so are specs the engine cannot target by design",
          sum(untargetable.values()) > 0, str(dict(untargetable)))

    print(f"\n{_fail} failure(s)")
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""A status whose own note says it changes crit has to change crit.

    python3 test_crit_statuses.py

The pack spells crit rate **CRT**, and `compile_statuses`' stat vocabulary had only
cri/crit -- so 63 rows that state a crit change in their own description classified as
`kind: other`. `status.crit_rate_bonus` reads ONLY `kind: stat_mod`, deliberately (the
`other` bucket holds 146 rows whose named stat means something else, like Fear Nothing's
HP being pursuit damage), so Critical (47 skills), Penetrate (75), Malefics (56) and Keen
(127) all applied, drew their icon, counted down and moved nothing.

THE RISK THIS GUARDS is the conflation, not the gap: three different crit stats share one
spelling. "Increases Crit Damage Bonus" and "Increases CRT Avoidance" are not crit rate,
and feeding either to `crit_rate_bonus` would be worse than the silence it replaced. So
the checks run in both directions -- the right rows moved, and the lookalikes did not.
"""
import json
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["SEVENSINS_ACCOUNTS"] = tempfile.mkdtemp(prefix="sevensins-crit-test-")

import battle as bt                                            # noqa: E402
from engine import status as S                                  # noqa: E402

ROWS = json.load(open(os.path.join(HERE, "battle_data", "statuses.json")))
CRIT_DMG = re.compile(r"crit\w*\s*(?:hit\s*)?dam|crit dmg", re.I)
AVOID = re.compile(r"avoid", re.I)
FAILURES = []


def check(cond, msg):
    if not cond:
        FAILURES.append(msg)


def _unit():
    return bt.Battle(1101, [10401]).units["101"]


def _hold(unit, row_id, magnitude, stacks=1, sign=None):
    """Put one status on `unit` exactly as the registry classifies it."""
    row = ROWS[str(row_id)]
    unit.statuses = [S.Active(
        status_id=int(row_id), name=row["name"], kind=row.get("kind"),
        category=row.get("category"), stat=row.get("stat"), magnitude=magnitude,
        sign=sign, stacks=stacks, stack_cap=row.get("stack_cap"), remaining=3)]
    return row


def a_stated_crit_buff_raises_crit():
    """Critical: 暴擊率+5%，可疊加5次 -- 5% a stack, to +25% at its stated cap."""
    u = _unit()
    _hold(u, 3003, 5.0, stacks=1, sign=1)
    one = S.crit_rate_bonus(u)
    _hold(u, 3003, 5.0, stacks=5, sign=1)
    five = S.crit_rate_bonus(u)
    check(abs(one - 0.05) < 1e-9, f"Critical at 1 stack gave {one}, want +0.05")
    check(abs(five - 0.25) < 1e-9, f"Critical at its 5-stack cap gave {five}, want +0.25")


def a_stated_crit_debuff_lowers_it():
    """Gloom reduces the caster's CRT -- the sign has to come out negative."""
    u = _unit()
    _hold(u, 7017, 10.0)
    got = S.crit_rate_bonus(u)
    check(got < 0, f"Gloom, a crit DEBUFF, moved crit by {got} -- not downward")


def crit_damage_is_not_crit_rate():
    """Every row whose own note says Crit Damage must stay off the crit-RATE channel."""
    dmg = [k for k, r in ROWS.items() if CRIT_DMG.search(r.get("description") or "")]
    check(dmg, "no Crit Damage rows found at all -- wrong fixture")
    for k in dmg:
        check(ROWS[k].get("stat") != "CRI",
              f"row {k} {ROWS[k]['name']!r} says Crit Damage but carries stat CRI")
    u = _unit()
    for k in dmg[:12]:
        _hold(u, k, 30.0, sign=1)
        got = S.crit_rate_bonus(u)
        check(got == 0.0,
              f"row {k} {ROWS[k]['name']!r} is Crit Damage and moved crit RATE by {got}")


def crit_avoidance_is_not_crit_rate():
    """CRT Avoidance is anti-crit (formula's `tgn`), not a negative crit rate."""
    av = [k for k, r in ROWS.items()
          if AVOID.search(r.get("description") or "")
          and re.search(r"\b(?:crt|cri|crit)\b", r.get("description") or "", re.I)]
    check(av, "no CRT Avoidance rows found at all -- wrong fixture")
    u = _unit()
    for k in av:
        check(ROWS[k].get("stat") != "CRI",
              f"row {k} {ROWS[k]['name']!r} is CRT Avoidance but carries stat CRI")
        _hold(u, k, 20.0, sign=1)
        check(S.crit_rate_bonus(u) == 0.0,
              f"row {k} {ROWS[k]['name']!r} is CRT Avoidance and moved crit rate")


def the_vocabulary_actually_reaches_the_pack():
    """Guard the gap itself: the rows this was written for must be live, by count.

    Pinned as a floor rather than an exact number so adding a crit status later does not
    fail the suite, but reverting the CRT vocabulary does.
    """
    live = [k for k, r in ROWS.items()
            if r.get("kind") == "stat_mod" and r.get("stat") == "CRI"]
    check(len(live) >= 40,
          f"only {len(live)} crit-rate statuses are stat_mod -- the CRT vocabulary is gone")
    for name in ("Critical", "Penetrate", "Malefics", "Keen"):
        hit = [k for k in live if ROWS[k]["name"] == name]
        check(hit, f"{name} is not a live crit-rate status")


def main():
    for fn in (a_stated_crit_buff_raises_crit, a_stated_crit_debuff_lowers_it,
               crit_damage_is_not_crit_rate, crit_avoidance_is_not_crit_rate,
               the_vocabulary_actually_reaches_the_pack):
        fn()
    for f in FAILURES:
        print("  FAIL", f)
    print(f"\n{'all OK' if not FAILURES else str(len(FAILURES)) + ' failure(s)'}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())

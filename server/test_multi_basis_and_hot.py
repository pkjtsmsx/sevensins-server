#!/usr/bin/env python3
"""Two halves the engine was dropping: a second damage basis, and a HoT's own pool.

    python3 test_multi_basis_and_hot.py

MULTI-BASIS. "20%攻擊力+50%防禦力的3段傷害" is one hit driven by TWO stats. The compiler
keeps only the first, so a DEF-built cast lands its small ATK component and drops the
large DEF one. 153 skills state two bases and all 153 compile with one; 141 are
recoverable, and in 66 of those the DROPPED half is the bigger.

HOLDER-POOL HoTs. Five heal statuses are a share of the pool of whoever CARRIES them, and
every HoT read `source_atk` instead -- so a 20% regen on a 100k pool healed a few hundred.
The Chinese settles what the English "HP%" means: 回復自身體力最大值25%, MAXIMUM.

Both are anchored to the pack: the multi-basis table is re-derived here from the prose
rather than compared against itself, and the HoT set is re-read from the status rows.
"""
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["SEVENSINS_ACCOUNTS"] = tempfile.mkdtemp(prefix="sevensins-mb-")

import battle as bt                                            # noqa: E402
import design_data as dd                                       # noqa: E402
from engine import multi_basis as mb                            # noqa: E402
from engine import specs                                        # noqa: E402
from engine import status as est                                # noqa: E402

FAILURES = []


def check(cond, msg):
    if not cond:
        FAILURES.append(msg)


TWO = re.compile(r"(\d+(?:\.\d+)?)\s*%\s*(攻擊力|防禦力|最大體力)\s*\+\s*"
                 r"(\d+(?:\.\d+)?)\s*%\s*(攻擊力|防禦力|最大體力)")
BAS = {"攻擊力": "ATK", "防禦力": "DEF", "最大體力": "MAX_HP"}


def the_table_is_derivable_from_the_prose():
    """Every entry must be the OTHER figure the prose states -- never an invention."""
    rows = dd.rows("skill") or {}
    checked = 0
    for sid, (basis, coef) in mb.SECOND_COMPONENT.items():
        zh = (rows.get(sid) or {}).get("_note1") or ""
        m = TWO.search(zh)
        check(m, f"{sid}: its prose no longer states two bases")
        if not m:
            continue
        pair = {(BAS[m.group(2)], round(float(m.group(1)) / 100.0, 4)),
                (BAS[m.group(4)], round(float(m.group(3)) / 100.0, 4))}
        check((basis, round(coef, 4)) in pair,
              f"{sid}: recovered {(basis, coef)} is neither figure the prose states {pair}")
        # ...and the COMPILED component must be the other member of that pair.
        dmg = [e for e in (specs.skills()[sid].get("effects") or [])
               if e.get("op") == "damage"]
        check(len(dmg) == 1, f"{sid}: now compiles {len(dmg)} damage components, not 1")
        if len(dmg) == 1 and dmg[0].get("coefficient") is not None:
            compiled = ((dmg[0].get("basis") or "ATK").upper(),
                        round(dmg[0]["coefficient"], 4))
            check(compiled in pair,
                  f"{sid}: compiled {compiled} is not one of the prose figures {pair}")
            check(compiled != (basis, round(coef, 4)),
                  f"{sid}: the recovered half is the SAME as the compiled one -- "
                  f"the damage would be double-counted")
        checked += 1
    check(checked > 100, f"only {checked} entries checked -- the table shrank")


def the_ambiguous_max_hp_rows_are_held_back():
    """12 ATK+Max HP rows state no possessor, and the engine's MAX_HP reads the TARGET.

    If 最大體力 means the caster's pool and we bill the target's, a 9% component against a
    boss is catastrophic rather than merely wrong. Their context points at the caster
    (70020111 heals allies for 25%最大體力值; 70020121 sets 自己的體力 to max), so they wait
    for footage. This asserts they stay out and that DEF is all that ships.
    """
    check(all(b == "DEF" for b, _c in mb.SECOND_COMPONENT.values()),
          "a non-DEF second component is in the table; the MAX_HP possessor is still "
          "unresolved and the basis reads the target's pool")
    rows = dd.rows("skill") or {}
    for sid in (70020101, 70020111, 70020121):
        check(mb.second_component(sid) is None, f"{sid} is back in the table")
        zh = (rows.get(sid) or {}).get("_note1") or ""
        check("最大體力" in zh, f"{sid} no longer states a Max HP component")
        check("自身最大體力" not in zh and "目標最大體力" not in zh,
              f"{sid} now names a possessor -- re-read it; the row may be shippable")


def the_dropped_half_is_often_the_bigger_one():
    bigger = 0
    for sid, (_b, coef) in mb.SECOND_COMPONENT.items():
        dmg = [e for e in (specs.skills()[sid].get("effects") or [])
               if e.get("op") == "damage"]
        if dmg and dmg[0].get("coefficient") and coef > dmg[0]["coefficient"]:
            bigger += 1
    check(bigger > 25,
          f"only {bigger} entries drop the larger half; the motivation may have changed")


def a_second_basis_actually_lands():
    """Behaviour, not table contents: a DEF component must move with DEF."""
    from engine import core as ecore
    import random
    target = None
    for sid, (basis, _c) in mb.SECOND_COMPONENT.items():
        if basis == "DEF" and sid in specs.skills():
            target = sid
            break
    check(target, "no DEF second component to test with")
    if not target:
        return

    def hit(defence):
        c = bt.Unit(order="p1", char_id=10001, team=1, index=0, lv=50)
        t = bt.Unit(order="e1", char_id=10001, team=2, index=0, lv=50)
        c.atk, c.defence = 10_000, defence
        c.hp = c.max_hp = 500_000
        t.hp = t.max_hp = 50_000_000
        t.defence = 0
        out = ecore.execute(c, specs.skills()[target], [c, t],
                           rng=random.Random(1), chosen="e1", round_no=1)
        return sum(s.amount or 0 for s in out.strikes)

    lo, hi = hit(5_000), hit(40_000)
    check(lo > 0, f"skill {target} deals no damage at all")
    check(hi > lo,
          f"skill {target}: raising the caster's DEF did not raise its damage "
          f"({lo} -> {hi}); the DEF half is not landing")
    # ...and the strike detail says which half was added.
    check(any("second_basis" in (s.detail or {}) for s in
              ecore.execute(bt_unit_pair(target)[0], specs.skills()[target],
                            list(bt_unit_pair(target)), rng=random.Random(2),
                            chosen="e1", round_no=1).strikes),
          f"skill {target}: no strike reports second_basis")


def bt_unit_pair(_sid):
    c = bt.Unit(order="p1", char_id=10001, team=1, index=0, lv=50)
    t = bt.Unit(order="e1", char_id=10001, team=2, index=0, lv=50)
    c.atk = c.defence = 10_000
    c.hp = c.max_hp = 500_000
    t.hp = t.max_hp = 50_000_000
    t.defence = 0
    return c, t


def a_holder_pool_hot_heals_off_the_holder():
    u = bt.Unit(order="p1", char_id=10001, team=1, index=0, lv=50)
    u.max_hp = u.hp = 100_000
    u.atk = 3_000

    def tick(sid, mag):
        u.statuses = []
        row = specs.statuses().get(int(sid)) or {}
        a = est.Active(status_id=int(sid), name=row.get("name"), remaining=3,
                       kind=row.get("kind"), category=row.get("category"),
                       magnitude=mag)
        a.source_atk = 3_000
        u.statuses.append(a)
        return est.tick_damage(u)[1]

    # The rows whose own text says the holder receives it.
    check(tick(1005, 20.0) == 20_000,
          f"Regeneration 20% healed {tick(1005, 20.0)}, not 20% of the holder's 100k pool")
    check(tick(1009, 15.0) == 15_000, "Heartwarming is not off the holder's pool")
    check(tick(1008, 25.0) == 25_000, "Sleep Well is not off the holder's pool")
    # The ones that must NOT move.
    check(tick(1001, 75.0) == 2_250,
          f"Heal moved off the caster's ATK ({tick(1001, 75.0)}); its prose says "
          f"以施術者的75%攻擊力")
    check(tick(1002, 15.0) == 450,
          "Regen (1002) moved; its wording is ambiguous and it was left alone on purpose")


def the_holder_set_comes_from_the_rows():
    """Re-read the registry, so a pack change fails here rather than silently."""
    est._HOT_HOLDER_IDS = None
    u = bt.Unit(order="p1", char_id=10001, team=1, index=0, lv=50)
    u.statuses = [est.Active(status_id=1005, name="Regeneration", remaining=2,
                             kind="heal", category="buff", magnitude=10.0)]
    est.tick_damage(u)                       # populates the cache
    got = set(est._HOT_HOLDER_IDS or ())
    expect = {int(sid) for sid, row in (specs.statuses() or {}).items()
              if row.get("kind") == "heal"
              and "receives this status" in str(row.get("description") or "").lower()
              and "according to the caster's atk" not in
              str(row.get("description") or "").lower()}
    check(got == expect, f"holder-pool set {sorted(got)} != rows say {sorted(expect)}")
    check(1001 not in got, "Heal (caster's ATK) is in the holder-pool set")
    check(got, "no heal row says 'receives this status' any more -- re-read before "
               "trusting this")


def main():
    the_table_is_derivable_from_the_prose()
    the_dropped_half_is_often_the_bigger_one()
    a_second_basis_actually_lands()
    a_holder_pool_hot_heals_off_the_holder()
    the_holder_set_comes_from_the_rows()
    for f in FAILURES:
        print("FAIL:", f)
    print(f"{len(FAILURES)} failure(s)")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())

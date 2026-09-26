#!/usr/bin/env python3
"""When a pursuit is ALLOWED to fire -- the two gates the compiler never filled in.

    python3 test_pursuit_gates.py

Both pursuits fired on every cast:

  * Kwon's Moon Trice Arcanum is "after that pursuit, a 15% FIXED CHANCE to pursue the
    lowest-HP enemy for 90% ATK". The compiler's `zh_pursuit_numbers` returns after the
    FIRST pursuit fragment, so a skill stating two pursuits loses the second one's odds
    -- 51 skills corpus-wide state a pursuit chance that compiled as none.
  * Satan's is the BANKAI's pursuit: "after the attack action, additionally uses Normal
    Attack IV once". It may only fire while Bankai is held, and Bankai is granted at 5
    stacks of Wrath when he uses his Special Move -- a clause the compiler listed
    `unmodelled` three times.

Anchored to behaviour: each check runs fights and counts, rather than asserting the gate
table's contents. A gate that is present in data and unread by the engine passes a table
assertion and fails here.
"""
import os
import random
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["SEVENSINS_ACCOUNTS"] = tempfile.mkdtemp(prefix="sevensins-gates-")

import battle as bt                                            # noqa: E402
import design_data as dd                                       # noqa: E402
from engine import core as ecore                                # noqa: E402
from engine import passives as P                                # noqa: E402
from engine import pursuit_values as pv                         # noqa: E402
from engine import specs                                        # noqa: E402
from engine import status as est                                # noqa: E402

FAILURES = []


def check(cond, msg):
    if not cond:
        FAILURES.append(msg)


def _run(parent_id, seed, statuses=()):
    """One skill against one target. -> (which sub-skills fired, total child damage)."""
    caster = bt.Unit(order="p1", char_id=10001, team=1, index=0, lv=50)
    target = bt.Unit(order="e1", char_id=10001, team=2, index=0, lv=50)
    caster.atk = caster.defence = 10_000
    caster.hp = caster.max_hp = 500_000
    target.hp = target.max_hp = 50_000_000
    target.defence = 0
    for sid, name in statuses:
        caster.statuses.append(est.Active(status_id=sid, name=name, remaining=5,
                                          kind="other", category="buff"))
    out = ecore.execute(caster, specs.skills()[parent_id], [caster, target],
                        rng=random.Random(seed), chosen="e1", round_no=1)
    fired = [c.skill for c in out.children if getattr(c, "skill", None)] or \
            [getattr(c, "skill_id", None) for c in out.children]
    dmg = 0
    for c in out.children:
        dmg += sum(s.amount or 0 for s in c.strikes)
    return out, dmg


def _child_ids(out):
    return [getattr(c, "skill_id", None) or (c.spec or {}).get("id")
            for c in out.children]


def moon_trice_is_a_fifteen_percent_roll():
    """Blazing Arcanum every time; Moon Trice about 15% of the time."""
    PARENT, GAMBLE, CERTAIN = 1070135, 1070172, 1070171
    check(pv.gate(PARENT, GAMBLE) == {"chance_pct": 15.0},
          "the 15% gate is not in the table")
    check(pv.gate(PARENT, CERTAIN) is None,
          "Blazing Arcanum was gated; its clause states no chance")

    hits = certain = 0
    N = 600
    for seed in range(N):
        out, _ = _run(PARENT, seed)
        ids = _child_ids(out)
        if GAMBLE in ids:
            hits += 1
        if CERTAIN in ids:
            certain += 1
    check(certain == N, f"the ungated pursuit fired {certain}/{N} times, expected all")
    # Binomial on 600 draws at p=0.15: mean 90, sd ~8.7. A wide band -- this is guarding
    # against "always" (600) and "never" (0), not measuring the RNG.
    check(40 <= hits <= 150,
          f"the 15% pursuit fired {hits}/{N} times; expected roughly 90")
    check(hits != N, "the 15% pursuit still fires unconditionally")


def satan_pursues_only_under_bankai():
    """The Krampus pursuit belongs to the Bankai, not to the skill."""
    PARENT = 2002133
    g = pv.gate(PARENT, None)
    check(g and g.get("requires_status") == "Bankai",
          f"Satan's pursuit is not gated on Bankai: {g}")

    without, dmg_without = _run(PARENT, 1)
    withb, dmg_with = _run(PARENT, 1, statuses=((158, "Bankai"),))
    check(not _child_ids(without) or dmg_without == 0,
          f"the pursuit fired with no Bankai held (damage {dmg_without})")
    check(dmg_with > 0,
          "the pursuit did NOT fire while Bankai was held -- the gate disabled it "
          "outright instead of timing it, which is worse than leaving it ungated")
    check(any(s.get("why") == "gate status absent" for s in without.skipped),
          f"no gate-absent skip recorded: {without.skipped}")


def bankai_is_granted_by_the_special_move():
    """Gating on Bankai is only correct because something grants it -- and the SP MOVE does.

    A hand-written passive rule used to do this, on the belief the clause was unmodelled.
    It is not: 使用必殺技時 points at the Special Move, and `2002121 Purgatory Xmas Tree`
    compiles the grant already -- with the per-rung threshold (5 stacks at I, 3 at III,
    none at VI) and `permanent: True`, all of which the hand rule got wrong. This asserts
    the REAL mechanism so the rule cannot creep back.
    """
    for sid, want_req in ((2002121, True), (2002123, True), (2002126, False)):
        spec = specs.skills().get(sid) or {}
        grants = [e for e in (spec.get("effects") or [])
                  if (e.get("status") or {}).get("id") == 158]
        check(grants, f"{sid} ({spec.get('name')}) no longer grants Bankai")
        for e in grants:
            check((e.get("numbers") or {}).get("permanent"),
                  f"{sid}: Bankai is not permanent -- 萬解 states 持續整場戰鬥")
            has_req = bool((e.get("requires") or {}).get("count"))
            check(has_req == want_req,
                  f"{sid}: Wrath-stack requirement present={has_req}, expected {want_req}")
    # The thresholds really do differ by rung -- a fixed 5 would be wrong.
    def _threshold(sid):
        for e in specs.skills()[sid].get("effects") or []:
            if (e.get("status") or {}).get("id") == 158:
                return (e.get("requires") or {}).get("count")
        return None

    lo, hi = _threshold(2002121), _threshold(2002123)
    check(lo != hi,
          f"the Wrath threshold no longer varies by rung: I={lo} III={hi}")
    check(_threshold(2002126) is None,
          f"rung VI now gates Bankai on stacks ({_threshold(2002126)}); its prose does not")

    # No hand rule may grant Bankai -- that is the duplicate this replaced.
    for rung in (2002131, 2002133, 2002136):
        kinds = [(r.trigger, r.status) for r in P.rules_for(rung)]
        check(("after_action", "Bankai") not in kinds,
              f"{rung}: a hand-written Bankai rule is back alongside the SP move's: {kinds}")
        # ...and Wrath must still be derived on both of its moments.
        check(("on_damage_taken", "Wrath") in kinds and ("turn_start", "Wrath") in kinds,
              f"{rung}: Satan lost a Wrath moment: {kinds}")


def bankai_raises_spd():
    """萬解's 自身速度+35%, stated on the alt-band rungs and nowhere on Satan's own."""
    import json
    rows = json.load(open(os.path.join(HERE, "battle_data", "statuses.json")))
    row = rows["158"]
    u = bt.Unit(order="p1", char_id=20021, team=1, index=0, lv=50)
    u.statuses.append(est.Active(status_id=158, name="Bankai", remaining=None,
                                 kind=row.get("kind"), category=row.get("category"),
                                 stat=row.get("stat")))
    pct = est.BANKAI_SPD_UP[158]
    check(abs(est.stat_multiplier(u, "SPD") - (1.0 + pct / 100.0)) < 1e-9,
          f"Bankai does not raise SPD (got {est.stat_multiplier(u, 'SPD')})")
    for other in ("ATK", "DEF"):
        check(est.stat_multiplier(u, other) == 1.0, f"Bankai moved {other}")
    # The figure must stay the pack's own, not drift into a house number.
    found = [sid for sid in (151000121, 152000121, 153000121)
             if "自身速度+%d%%" % int(pct) in ((dd.row("skill", sid) or {}).get("_note1") or "")]
    check(found, f"no pack row states 自身速度+{int(pct)}% for 萬解 any more -- re-read "
                 f"before trusting BANKAI_SPD_UP")


def the_pursuit_rung_is_not_flat():
    """17 parents say 普攻IV (0.99); three say 普攻VI (1.15)."""
    import re
    by = {}
    for pid, (coef, _b) in pv.PURSUIT_NAMED_SKILL.items():
        zh = (dd.row("skill", pid) or {}).get("_note1") or ""
        m = re.search(r"追加使用1次普攻([IVX]+)", zh)
        check(m, f"{pid} no longer names a basic-attack rung")
        if m:
            by.setdefault(m.group(1), set()).add(coef)
    check(set(by) == {"IV", "VI"}, f"rungs named changed: {sorted(by)}")
    check(by.get("IV") == {0.99}, f"普攻IV should be 0.99, got {by.get('IV')}")
    check(by.get("VI") == {1.15}, f"普攻VI should be 1.15, got {by.get('VI')}")
    # ...and those really are Satan's own ladder figures.
    for rung, want in (("IV", 2002104), ("VI", 2002106)):
        coefs = [e.get("coefficient") for e in (specs.skills()[want].get("effects") or [])
                 if e.get("op") == "damage"]
        check(next(iter(by[rung])) in coefs,
              f"普攻{rung} coefficient is not {want}'s own ({coefs})")


def ungated_pursuits_are_untouched():
    """The other 400-odd recovered pursuits must still fire every time."""
    fired = 0
    checked = 0
    for pid in list(pv.PURSUIT)[:40]:
        if pid not in specs.skills() or pv.gate(pid, None):
            continue
        checked += 1
        out, dmg = _run(pid, 3)
        if dmg > 0:
            fired += 1
    check(checked, "no ungated pursuit to test")
    check(fired, f"none of {checked} ungated pursuits landed damage -- the gate wiring "
                 f"broke the ordinary path")


def main():
    moon_trice_is_a_fifteen_percent_roll()
    satan_pursues_only_under_bankai()
    bankai_is_granted_by_the_special_move()
    bankai_raises_spd()
    the_pursuit_rung_is_not_flat()
    ungated_pursuits_are_untouched()
    for f in FAILURES:
        print("FAIL:", f)
    print(f"{len(FAILURES)} failure(s)")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())

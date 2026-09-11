#!/usr/bin/env python3
"""Counterattacks, the passive ladder cache, and crit from statuses.

Three defects, all in the path between what `tools/compile_skills.py` already resolved
and what the engine actually did with it. None of them was a data problem.

  1. **Nothing in the game countered.** `passives._derived_rules` had branches for
     apply_status / modify_gauge / heal / revive and none for `op: "damage"`, so every
     compiled passive damage effect fell off the end of the loop -- 261 of them, across
     five triggers, including all 38 counters. `rules_for()` over all 14,410 skills
     returned ZERO on_damage_taken damage rules while the compiler had been emitting
     Jealousy Vortex V as coefficient 3.5 / basis ATK / trigger on_damage_taken the
     whole time.

  2. **The derived-rule cache was keyed by GROUP**, so every level of a passive shared
     whichever rank was asked for first -- and "first asked", not "lowest", so the answer
     depended on call order. Requesting Jealousy Vortex V before I gave the entire ladder
     350%; requesting I first gave it 175%. 173 of 978 passive groups were affected.

  3. **Crit statuses were inert in both directions.** `stat_multiplier` covers
     ATK/DEF/SPD/HP only, and `formula.strike` read crit off the unit and never from a
     status. Underneath that, `Active.sign` was never populated: the compiler emits
     `numbers.magnitude_sign` and nothing in the server read it, so a status whose
     category is outside buff/debuff had no direction and its magnitude was unusable.
     All nine CRI stat_mods are `category: passive_grant`.

    python3 test_counter_passives.py
"""
import os
import sys

os.environ.setdefault("SEVENSINS_ACCOUNTS", "/tmp/sevensins-counter-test")

from engine import formula, passives as P, specs, status as est   # noqa: E402

_fail = 0


def check(label, ok, detail=""):
    global _fail
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f" -- {detail}"))
    if not ok:
        _fail += 1


# Leviathan's Jealousy Vortex (ATK basis) and Raphael's Serene Way of Harmony (DEF).
# Both ladders are the pack's own coefficients, read straight off the compiled spec.
LEVIATHAN = {1001131: 175.0, 1001133: 225.0, 1001135: 350.0, 1001136: 350.0}
RAPHAEL = {2041131: 140.0, 2041133: 150.0, 2041135: 160.0, 2041136: 180.0}


def _counters(skill_id):
    return [r for r in (P.rules_for(skill_id) or [])
            if r.trigger == P.ON_DAMAGE_TAKEN and r.effect == P.DAMAGE]


def check_counters_exist_at_all():
    print("\ncheck_counters_exist_at_all:")
    rules = _counters(1001135)
    check("Jealousy Vortex V produces a counter rule", len(rules) == 1, str(rules))
    if not rules:
        return
    r = rules[0]
    # The whole bug: this list was empty for every skill in the game.
    check("  ...aimed at the ATTACKER", r.to is P.ATTACKER, str(r.to))
    check("  ...off the holder's own ATK", r.basis == P.OF_SELF_ATK, r.basis)
    check("  ...for the coefficient the pack states", r.magnitude == 350.0,
          str(r.magnitude))


def check_defence_basis():
    print("\ncheck_defence_basis:")
    # 防壁反射：受到傷害時，以140%防禦力進行反擊 -- DEF, not ATK. 19 of the 38 compiled
    # counters are DEF-based, so paying them off ATK would be wrong for half the set.
    rules = _counters(2041131)
    check("Raphael's counter exists", len(rules) == 1, str(rules))
    if not rules:
        return
    check("  ...and scales off DEFENCE", rules[0].basis == P.OF_SELF_DEF,
          rules[0].basis)

    # And _amount honours it: 140% of 2000 DEF is 2800, and must NOT read 2800 off ATK.
    holder = type("U", (), {})()
    holder.atk, holder.defence, holder.max_hp = 9999, 2000, 50000
    amount = P._amount(rules[0], holder, holder, {})
    check("  ...so 140% of 2000 DEF pays 2800, not a slice of ATK", amount == 2800,
          f"got {amount} (atk was {holder.atk})")


def check_ladder_is_per_level_and_order_independent():
    print("\ncheck_ladder_is_per_level_and_order_independent:")
    # Ask HIGH first, deliberately: with the old group-keyed cache this poisoned every
    # lower rank with V's 350%, and asking low-first poisoned it the other way. The
    # assertion is that BOTH orders give each level its own number.
    P._COMPILED.clear()
    high_first = {sid: [r.magnitude for r in _counters(sid)]
                  for sid in sorted(LEVIATHAN, reverse=True)}
    P._COMPILED.clear()
    low_first = {sid: [r.magnitude for r in _counters(sid)]
                 for sid in sorted(LEVIATHAN)}
    check("every rank keeps its own coefficient",
          all(high_first[sid] == [want] for sid, want in LEVIATHAN.items()),
          str(high_first))
    check("  ...and the answer does not depend on call order",
          high_first == low_first, f"{high_first} vs {low_first}")
    check("Raphael's ladder too",
          all([r.magnitude for r in _counters(sid)] == [want]
              for sid, want in RAPHAEL.items()),
          str({s: [r.magnitude for r in _counters(s)] for s in RAPHAEL}))


def check_crit_from_statuses():
    print("\ncheck_crit_from_statuses:")
    unit = type("U", (), {})()
    unit.statuses = []
    check("no statuses, no bonus", est.crit_rate_bonus(unit) == 0.0)

    # Critical Surge I is `category: passive_grant`, which is in neither RAISING nor
    # LOWERING -- so without the prose's own sign its magnitude has no direction and
    # signed_magnitude() returns None. That is why these were inert.
    surge = est.Active(status_id=8100, name="Critical Surge I", kind="stat_mod",
                       category="passive_grant", stat="CRI", magnitude=4.0, sign=1)
    unit.statuses.append(surge)
    check("a CRI stat_mod with the prose's sign is a +4% rate",
          abs(est.crit_rate_bonus(unit) - 0.04) < 1e-9, str(est.crit_rate_bonus(unit)))

    unsigned = est.Active(status_id=8098, name="Critical Surge III", kind="stat_mod",
                          category="passive_grant", stat="CRI", magnitude=8.0)
    unit.statuses = [unsigned]
    check("  ...and WITHOUT a sign it stays unusable rather than being guessed at",
          est.crit_rate_bonus(unit) == 0.0, str(est.crit_rate_bonus(unit)))

    # ADDITIVE, because crit is a rate: +4 and +8 is +12 points, not a product.
    unit.statuses = [surge, est.Active(status_id=8098, name="Critical Surge III",
                                       kind="stat_mod", category="passive_grant",
                                       stat="CRI", magnitude=8.0, sign=1)]
    check("two of them stack additively",
          abs(est.crit_rate_bonus(unit) - 0.12) < 1e-9, str(est.crit_rate_bonus(unit)))

    # Only stat_mod. `other` holds 146 statuses that name a stat and are not stat
    # changes at all -- Fear Nothing (HP) is pursuit damage, Grand Feast (HP) a heal.
    unit.statuses = [est.Active(status_id=1, name="Fear Nothing", kind="other",
                                category="misc", stat="CRI", magnitude=180.0, sign=1)]
    check("a kind=other status is NOT read as a crit change",
          est.crit_rate_bonus(unit) == 0.0, str(est.crit_rate_bonus(unit)))


def check_strike_uses_it():
    print("\ncheck_strike_uses_it:")
    # The end of the chain: formula.strike never consulted a status for crit, so this
    # is what actually makes Critical Surge do something in a fight.
    import random

    def _unit(cri, statuses):
        u = type("U", (), {})()
        u.atk, u.defence, u.max_hp, u.hp = 1000, 0, 9999, 9999
        u.attribute, u.order, u.cri = None, "a", cri
        u.cdi = u.cdr = u.prc = 0.0
        u.statuses = statuses
        return u

    # A 0% base with a +100% status must crit every time; without the status, never.
    boosted = _unit(0.0, [est.Active(status_id=8100, name="Critical Surge I",
                                     kind="stat_mod", category="passive_grant",
                                     stat="CRI", magnitude=100.0, sign=1)])
    plain = _unit(0.0, [])
    target = _unit(0.0, [])
    crits = sum(1 for i in range(60)
                if formula.strike(boosted, target, 1.0, rng=random.Random(i))[1]["crit"])
    none = sum(1 for i in range(60)
               if formula.strike(plain, target, 1.0, rng=random.Random(i))[1]["crit"])
    check("a +100% crit status crits every swing", crits == 60, f"{crits}/60")
    check("  ...and without it, none of them do", none == 0, f"{none}/60")


def check_a_counter_kill_reaches_the_client():
    print("\ncheck_a_counter_kill_reaches_the_client:")
    # `_flag_deaths` took its candidates from the skill's TARGETS, and a counter kills
    # the ATTACKER -- who is never one. So a counter that finished off the attacker put
    # `die: 0` on the wire and the client kept that unit in its own ActionOrderList,
    # waiting for a turn the server would never hand out. Same shape as the DoT
    # soft-lock, reached from the other side.
    import json
    import tempfile
    os.environ["SEVENSINS_ACCOUNTS"] = tempfile.mkdtemp()
    import battle as bt
    import player_state as ps
    state = ps.load("counter_die_test")
    b = bt.Battle(1101, ps.battle_team(state, 0), 60, None, 0, 0)
    foe = next(u for u in b.units.values() if u.team == bt.TEAM_ENEMY)
    me = next(u for u in b.units.values() if u.team == bt.TEAM_PLAYER)
    foe.skills = list(foe.skills or []) + [1001135]     # Jealousy Vortex V, 350% ATK
    foe.max_hp = foe.hp = 10 ** 8
    foe.atk = 4000                                       # -> a 14,000 counter
    cmd = json.loads(b.attack_cmd_json(me.order, foe.order, me.skills[0]))
    rows = [r for g in cmd["combo"][0]["data"] for r in g if r["c"] == me.order]
    check("the counter reaches the wire at all", len(rows) == 1, json.dumps(rows))
    if not rows:
        return
    check("  ...for 350% of the holder's 4000 ATK", rows[0]["dmg"] == -14000,
          str(rows[0]["dmg"]))
    check("  ...and the attacker it killed is flagged dead",
          not me.alive and rows[0]["die"] == 1,
          f"alive={me.alive} die={rows[0]['die']}")


if __name__ == "__main__":
    check_counters_exist_at_all()
    check_defence_basis()
    check_ladder_is_per_level_and_order_independent()
    check_crit_from_statuses()
    check_strike_uses_it()
    check_a_counter_kill_reaches_the_client()
    print(f"\n{_fail} failure(s)")
    sys.exit(1 if _fail else 0)

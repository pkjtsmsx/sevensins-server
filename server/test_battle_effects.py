#!/usr/bin/env python3
"""Regression harness for the skill-effect engine. Run after ANY change to the engine,
the parser, or skill_effects.json:  ../.venv/bin/python test_battle_effects.py

Guards the invariants from docs/BATTLE_SKILL_PLAN.md:
  1. LOCK-STEP: every op the parser emits has a registered engine handler. A missing
     handler means a `complete` (== trusted == live) skill silently does nothing.
  2. NO-CRASH: every `complete` skill runs through execute_skill and every trigger phase
     without throwing -- a live battle must never crash on some obscure mob's skill.
  3. STARTER CASTS: the 8 hand-validated tutorial skills still produce their known effects.
  4. Full multi-wave battles still run to a clear.

Stdlib only; exits non-zero on failure so it can gate a commit.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import battle as bt          # noqa: E402
import battle_effects as fx  # noqa: E402

SKILL_EFFECTS = os.path.join(HERE, "battle_data", "skill_effects.json")
_fail = 0


def check(name, cond, detail=""):
    global _fail
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        _fail += 1


class MockUnit:
    def __init__(self, team, order, atk=1000, hp=5000, spd=300, defense=200):
        self.atk = atk; self.hp = hp; self.max_hp = hp; self.team = team
        self.spd = spd; self.defense = defense; self.statuses = []; self.order = order

    @property
    def alive(self):
        return self.hp > 0


def test_lockstep(skills):
    emitted = set()
    for rec in skills.values():
        for b in rec.get("blocks", []):
            for e in b.get("effects", []):
                if e.get("op"):
                    emitted.add(e["op"])
    missing = emitted - fx.registered_ops()
    check("lock-step: every emitted op has a handler", not missing,
          f"unhandled ops: {sorted(missing)}")


def test_no_crash(skills):
    complete = [sid for sid, r in skills.items() if r.get("complete")]
    crashes = []
    for sid in complete:
        a = MockUnit(0, "101"); allies = [a, MockUnit(0, "102")]
        enemies = [MockUnit(1, "201"), MockUnit(1, "202"), MockUnit(1, "203")]
        try:
            fx.execute_skill(a, enemies[0], allies, enemies, int(sid),
                             damage_reduce=lambda u: 0.1)
            for ph in ("battle_start", "on_counter", "after_action", "after_attack",
                       "conditional"):
                fx.run_phase(int(sid), ph, a, enemies[0], allies, enemies,
                             damage_reduce=lambda u: 0.1)
        except Exception as e:                                    # noqa: BLE001
            crashes.append((sid, type(e).__name__, str(e)))
    check(f"no-crash across {len(complete)} complete skills", not crashes,
          f"{len(crashes)} crashed, first: {crashes[:3]}")


def test_starters(skills):
    # 8 hand-validated tutorial casts must stay complete.
    starters = (1000101, 1000111, 1000121, 1001101, 1001111, 1001121, 1001131)
    check("8 starter casts complete",
          all(skills.get(str(s), {}).get("complete") for s in starters))

    # Blink Slash (Lucifer basic) = 110% x2 + Fracture(target) + Keen(self).
    a = MockUnit(0, "101", atk=100); d = MockUnit(1, "201", hp=100000)
    out = fx.execute_skill(a, d, [a], [d], 1000101)
    dmg = out["hits"][0]["damage"] if out["hits"] else 0
    check("Blink Slash 110%x2 = 220 dmg", dmg == 220, f"got {dmg}")
    check("Blink Slash inflicts Fracture on target",
          any("Fracture" in h["statuses"] for h in out["hits"]))
    check("Blink Slash grants Keen on self", "Keen" in out["self"]["statuses"])


def test_battle_start_and_counter():
    # Leviathan's Jealousy Vortex passive: battle_start immunity+team buffs, on_counter dmg.
    b = bt.Battle(1101, [{"id": 10001}, {"id": 10011}], team_level=10)
    levi = next(u for u in b.units.values() if u.char_id == 10011)
    check("Leviathan gains Freeze immunity at battle start",
          any(s.name == "Immune:Freeze" for s in levi.statuses))
    check("battle-start immunity actually blocks Freeze",
          fx.apply_status(levi, "Freeze") is None)
    enemies = [u for u in b.units.values() if u.team == bt.TEAM_ENEMY]
    e0 = enemies[0]; hp0 = e0.hp
    b.attack_cmd_json(e0.order, levi.order, e0.skills[0])   # enemy hits Leviathan
    check("on_counter damages the attacker", e0.hp < hp0, f"{hp0} -> {e0.hp}")


def test_full_battles():
    for lvl, star in ((10, None), (100, 6)):
        b = bt.Battle(1101, [{"id": 10001}, {"id": 10011}], team_level=lvl, team_star=star)
        turns = 0
        while turns < 300:
            turns += 1
            u = b.acting_unit()
            if u is None:
                break
            foes = [x for x in b.units.values() if x.team != u.team and x.alive]
            if not foes:
                if b.wave_cleared() and b.has_next_wave():
                    b.advance_wave(); continue
                break
            slot = 2 if (len(u.skills) > 2 and u.ultimate_ready()) else 0
            json.loads(b.attack_cmd_json(u.order, foes[0].order, u.skills[slot]))
            b.end_turn()
            if b.party_wiped():
                break
        check(f"full 3-wave clear at level {lvl}", b.wave_cleared() and not b.party_wiped(),
              f"wave {b.wave}, cleared={b.wave_cleared()}, wiped={b.party_wiped()}")


def main():
    with open(SKILL_EFFECTS, encoding="utf-8") as f:
        skills = json.load(f)
    complete = sum(1 for r in skills.values() if r.get("complete"))
    print(f"skill_effects.json: {len(skills)} skills, {complete} complete")
    print("engine registered ops:", sorted(fx.registered_ops()))
    test_lockstep(skills)
    test_no_crash(skills)
    test_starters(skills)
    test_battle_start_and_counter()
    test_full_battles()
    print(f"\n{'ALL PASSED' if not _fail else f'{_fail} CHECK(S) FAILED'}")
    sys.exit(1 if _fail else 0)


if __name__ == "__main__":
    main()

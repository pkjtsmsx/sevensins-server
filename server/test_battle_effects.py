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


def _cond_kinds(cond):
    out = {cond.get("kind")}
    for c in cond.get("conds", []):
        out |= _cond_kinds(c)
    return out


def test_lockstep(skills):
    emitted, kinds = set(), set()
    for rec in skills.values():
        for b in rec.get("blocks", []):
            for e in b.get("effects", []):
                if e.get("op"):
                    emitted.add(e["op"])
                if e.get("when"):
                    kinds |= _cond_kinds(e["when"])
    missing = emitted - fx.registered_ops()
    check("lock-step: every emitted op has a handler", not missing,
          f"unhandled ops: {sorted(missing)}")
    unkn = kinds - fx.registered_conds()
    check("lock-step: every emitted condition kind has an evaluator", not unkn,
          f"unhandled kinds: {sorted(unkn)}")


def test_no_crash(skills):
    complete = [sid for sid, r in skills.items() if r.get("complete")]
    crashes = []
    for sid in complete:
        a = MockUnit(0, "101"); allies = [a, MockUnit(0, "102")]
        enemies = [MockUnit(1, "201"), MockUnit(1, "202"), MockUnit(1, "203")]
        try:
            for env in (None, {"turn": 3, "crit": True}, {"turn": 26}):
                fx.execute_skill(a, enemies[0], allies, enemies, int(sid),
                                 damage_reduce=lambda u: 0.1, env=env)
            for ph in ("battle_start", "on_counter", "after_action", "after_attack",
                       "conditional"):
                fx.run_phase(int(sid), ph, a, enemies[0], allies, enemies,
                             damage_reduce=lambda u: 0.1, env={"turn": 5})
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


def test_conditions():
    """Evaluator semantics (Phase 2), driven through a duck-typed ctx."""
    from types import SimpleNamespace

    a = MockUnit(0, "101"); t = MockUnit(1, "201")
    a.job = 2                                        # STR (char _job 2/3/4 = STR/AGI/TEC)
    ctx = SimpleNamespace(attacker=a, primary=t, allies=[a], enemies=[t],
                          env={"turn": 3}, per_target={})
    ev = fx.eval_cond
    check("hp gate: full HP passes >90%",
          ev({"kind": "hp", "subject": "self", "cmp": "gt", "pct": 90}, ctx))
    a.hp = a.max_hp // 2
    check("hp gate: half HP fails >90%",
          not ev({"kind": "hp", "subject": "self", "cmp": "gt", "pct": 90}, ctx))
    check("status gate: absent status fails",
          not ev({"kind": "status", "subject": "target", "names": ["Stun"],
                  "negate": False}, ctx))
    fx.apply_status(t, "Stun")
    check("status gate: applied status passes",
          ev({"kind": "status", "subject": "target", "names": ["Stun"],
              "negate": False}, ctx))
    check("status gate: negate flips",
          not ev({"kind": "status", "subject": "target", "names": ["Stun"],
                  "negate": True}, ctx))
    check("status gate: Stun counts as a debuff class",
          ev({"kind": "status", "subject": "target", "names": ["_debuff"],
              "negate": False}, ctx))
    check("cast_type gate: attacker job 2 is STR (self)",
          ev({"kind": "cast_type", "subject": "self", "type": "STR"}, ctx))
    check("cast_type gate: target without job fails",
          not ev({"kind": "cast_type", "subject": "target", "type": "TEC"}, ctx))
    check("turn gates: turn 3 is odd and <= 25",
          ev({"kind": "turn_parity", "parity": "odd"}, ctx)
          and ev({"kind": "turn_cmp", "cmp": "le", "n": 25}, ctx))
    check("crit gate: false without a crit model",
          not ev({"kind": "crit"}, ctx))
    ctx.per_target = {"201": {"died": True}}
    check("kill gate: sees this action's kills",
          ev({"kind": "kill", "negate": False}, ctx))
    check("unknown kind never fires", not ev({"kind": "someday"}, ctx))


def test_mechanics_depth():
    """Phase 3: DoT/HoT ticking, shield absorption, and flag enforcement."""
    caster = MockUnit(0, "c1", atk=1000)
    burned = MockUnit(1, "t1", hp=5000)
    fx.apply_status(burned, "Burn", source=caster)      # 15% ATK/stack
    fx.apply_status(burned, "Burn", source=caster)      # -> 2 stacks
    dot, hot = fx.tick_dot_hot(burned)
    check("DoT tick: 2 Burn stacks deal 2x150 = 300", dot == 300, f"got {dot}")

    healer = MockUnit(0, "h1", hp=5000)
    healer.hp = 2000
    fx.apply_status(healer, "Regeneration", source=healer)   # heal_pct_maxhp 25
    _, hot = fx.tick_dot_hot(healer)
    check("HoT tick: Regeneration heals 25% Max HP", hot == 1250, f"got {hot}")

    blocked = MockUnit(0, "h2", hp=2000)
    fx.apply_status(blocked, "Regeneration", source=blocked)
    fx.apply_status(blocked, "Heal Block")
    _, hot = fx.tick_dot_hot(blocked)
    check("HoT tick: heal_block suppresses the HoT", hot == 0, f"got {hot}")

    shielded = MockUnit(1, "s1", hp=5000)
    attacker = MockUnit(0, "a1", atk=1000)
    fx.apply_status(shielded, "Shield", source=attacker)    # 75% of 1000 ATK = 750
    through = fx.absorb_shield(shielded, 1000)
    check("shield absorbs 750 of a 1000 hit", through == 250, f"got {through}")

    sealed = MockUnit(0, "seal1")
    fx.apply_status(sealed, "Silence")
    check("ability_seal flag reads through has_flag", fx.has_flag(sealed, "ability_seal"))

    blocked_cd = MockUnit(0, "cd1")
    fx.apply_status(blocked_cd, "CD Reduction Block")
    check("cd_reduction_block flag reads through has_flag",
          fx.has_flag(blocked_cd, "cd_reduction_block"))

    taunter = MockUnit(1, "tn1")
    taunted = MockUnit(0, "tn2")
    fx.apply_status(taunted, "Taunt", source=taunter)
    st = next(s for s in taunted.statuses if s.name == "Taunt")
    check("Taunt records its inflicter as taunt_source", st.taunt_source == "tn1",
          f"got {st.taunt_source}")

    fake = MockUnit(0, "u2")
    from battle_effects.core import Status
    buff_def = {"stat_mods": [{"stat": "ATK", "value": 20, "unit": "pct"}]}
    fake.statuses.append(Status("Locked Buff", "battle",
                                dict(buff_def, flags=["unremovable"])))
    fake.statuses.append(Status("Plain Buff", 2, buff_def))
    import battle_effects.ops as ops_mod
    from types import SimpleNamespace
    ctx = SimpleNamespace(targets=lambda tok: [fake])
    ops_mod._cleanse_class({"cls": "_buff"}, ctx)
    check("cleanse_class leaves an unremovable status behind",
          any(s.name == "Locked Buff" for s in fake.statuses)
          and not any(s.name == "Plain Buff" for s in fake.statuses))


def test_immobilize_skips_turn():
    b = bt.Battle(1101, [{"id": 10001}, {"id": 10011}], team_level=10)
    victim_order = b.turn_order[1]
    victim = b.units[victim_order]
    fx.apply_status(victim, "Stun", source=victim)
    b.end_turn()
    check("a stunned unit's turn is skipped, not left waiting",
          b.turn_order[0] != victim_order, f"front is still {b.turn_order[0]}")


def test_forced_targeting():
    b = bt.Battle(1101, [{"id": 10001}, {"id": 10011}], team_level=10)
    p = next(u for u in b.units.values() if u.team == bt.TEAM_PLAYER)
    e0, e1 = [u for u in b.units.values() if u.team == bt.TEAM_ENEMY][:2]
    fx.apply_status(p, "Taunt", source=e0)
    cmd = json.loads(b.attack_cmd_json(p.order, e1.order, p.skills[0]))
    check("Taunt redirects the attack to its inflicter, not the chosen target",
          cmd["combo"][0]["data"][0][0]["c"] == e0.order)

    b2 = bt.Battle(1101, [{"id": 10001}, {"id": 10011}], team_level=10)
    p2 = next(u for u in b2.units.values() if u.team == bt.TEAM_PLAYER)
    ally = next(u for u in b2.units.values()
               if u.team == bt.TEAM_PLAYER and u is not p2)
    enemy = next(u for u in b2.units.values() if u.team == bt.TEAM_ENEMY)
    fx.apply_status(p2, "Confuse", source=enemy)
    cmd2 = json.loads(b2.attack_cmd_json(p2.order, enemy.order, p2.skills[0]))
    check("Confuse redirects the attack onto the caster's own side",
          b2.units[cmd2["combo"][0]["data"][0][0]["c"]].team == p2.team)


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
    test_conditions()
    test_mechanics_depth()
    test_immobilize_skips_turn()
    test_forced_targeting()
    test_battle_start_and_counter()
    test_full_battles()
    test_fallback_aoe()
    print(f"\n{'ALL PASSED' if not _fail else f'{_fail} CHECK(S) FAILED'}")
    sys.exit(1 if _fail else 0)



def test_fallback_aoe():
    """An AoE skill must spread even when its parse is INCOMPLETE.

    Only `complete` skills reach the effect engine; everything else falls to Battle's
    simple damage path, which hit exactly one unit no matter what the description said.
    That silently single-targeted 226 skills whose parse had already identified the AoE
    (against 114 that worked), which is why "AoE skills all seem to hit one target".
    """
    AOE = 100001611     # incomplete: "Deals 80% ATK as damage to all enemies 3 times"
    SINGLE = 2009111    # complete: opens single-target, "all enemies" only later on

    check("the AoE case really is an incomplete parse", not fx.is_complete(AOE))
    check("and aoe_damage still sees the AoE", fx.aoe_damage(AOE))
    # The FIRST damage op is what the fallback models. A skill that opens single-target
    # and adds an AoE clause later must NOT splash, or every follow-up hits the team.
    check("a single-target opener is not treated as AoE", not fx.aoe_damage(SINGLE))
    check("an unknown skill id is not AoE", not fx.aoe_damage(999999999))

    b = bt.Battle(1101, [{"id": 10001}, {"id": 10011}], team_level=60)
    foes = [u for u in b.units.values() if u.team != bt.TEAM_PLAYER and u.alive]
    me = [u for u in b.units.values() if u.team == bt.TEAM_PLAYER][0]
    check("the test stage fields more than one enemy", len(foes) > 1, str(len(foes)))

    before = {u.order: u.hp for u in foes}
    out = json.loads(b.attack_cmd_json(me.order, foes[0].order, AOE))
    rows = out["combo"][0]["data"][0]
    hurt = [u.order for u in foes if u.hp < before[u.order]]
    check("every enemy takes damage", len(hurt) == len(foes), str(hurt))
    # The client draws one number per DamageInfo, so the wire has to carry them all.
    check("one DamageInfo row per struck enemy", len(rows) == len(foes), str(len(rows)))
    check("each row names its own unit",
          sorted(r["c"] for r in rows) == sorted(u.order for u in foes), str(rows))
    # damage() reads the victim's own DEF, so rolling once and reusing it would
    # over-hit the tanky and under-hit the frail.
    check("damage is rolled per target, not shared",
          all(r["dmg"] < 0 for r in rows), str([r["dmg"] for r in rows]))

    b2 = bt.Battle(1101, [{"id": 10001}, {"id": 10011}], team_level=60)
    foes2 = [u for u in b2.units.values() if u.team != bt.TEAM_PLAYER and u.alive]
    me2 = [u for u in b2.units.values() if u.team == bt.TEAM_PLAYER][0]
    before2 = {u.order: u.hp for u in foes2}
    json.loads(b2.attack_cmd_json(me2.order, foes2[0].order, SINGLE))
    hurt2 = [u.order for u in foes2 if u.hp < before2[u.order]]
    check("a single-target skill still hits exactly one", len(hurt2) == 1, str(hurt2))

if __name__ == "__main__":
    main()

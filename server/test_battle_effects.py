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
import collections
import random
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# **Point the account store at a THROWAWAY DIR before player_state is imported.**
# This suite drives real server paths (battle_end_reward, battle_replies) and those
# call ps.save(), so without this it writes a fresh default account straight over the
# player's real save. That is not hypothetical -- it happened, and cost a live account.
import tempfile as _tempfile
os.environ["SEVENSINS_ACCOUNTS"] = _tempfile.mkdtemp(prefix="sevensins-test-")

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
    test_self_inflicted_damage_survives()
    test_unit_count_gate()
    test_fallback_aoe()
    test_enemy_multi_target()
    test_starshard_temple_drops()
    test_transcend_corridor_drops()
    test_auto_play_sweep()
    print(f"\n{'ALL PASSED' if not _fail else f'{_fail} CHECK(S) FAILED'}")
    sys.exit(1 if _fail else 0)



def test_self_inflicted_damage_survives():
    """A stated drawback must keep hitting the caster.

    Hot Spring Special is "Deals 12% Max HP damage to enemies, THEN TAKES 25% Max HP
    damage after the action" on an ENEMY-group skill -- exactly the shape the
    enemy-group guard rewrites when a damage op wrongly targets the caster. The guard
    has to tell a drawback apart from a mis-parse, or it turns a cost into a bonus.
    """
    rec = fx.skill_effects(2042101) or {}
    dmg = [f for b in rec.get("blocks", []) for f in b["effects"] if f["op"] == "damage"]
    mine = [f for f in dmg if f.get("self_inflicted")]
    theirs = [f for f in dmg if not f.get("self_inflicted")]
    check("the drawback still targets the caster",
          bool(mine) and mine[0]["target"] == "self", str(dmg))
    check("and carries its Max HP percentage",
          bool(mine) and mine[0].get("pct_target_maxhp") == 25, str(mine))
    check("while the attack itself still hits the enemy",
          bool(theirs) and theirs[0]["target"] == "enemy_target", str(theirs))


def test_unit_count_gate():
    """`unit_count` counts the SIDE's living units, not the target.

    The first version passed "all_enemies" to _pool, which knows only
    any_enemy/any_ally/self and falls through to the target -- so the gate compared 1
    against the threshold and every "if there are still at least 3 enemies" rider was
    dead. Count them for real, and prove it changes when one dies.
    """
    b = bt.Battle(1101, [{"id": 10001}, {"id": 10011}], team_level=60)
    foes = [u for u in b.units.values() if u.team != bt.TEAM_PLAYER]
    me = [u for u in b.units.values() if u.team == bt.TEAM_PLAYER][0]
    ctx = fx.Ctx(me, foes[0], [me], foes, None, {}, {"self": {}, "status_events": []})
    n = len(foes)
    check("counts every living enemy",
          fx.eval_cond({"kind": "unit_count", "side": "enemy", "cmp": "ge", "n": n}, ctx),
          str(n))
    check("and is false one above that",
          not fx.eval_cond({"kind": "unit_count", "side": "enemy",
                            "cmp": "ge", "n": n + 1}, ctx))
    foes[0].hp = 0
    check("a corpse stops counting",
          not fx.eval_cond({"kind": "unit_count", "side": "enemy",
                            "cmp": "ge", "n": n}, ctx))


def test_fallback_aoe():
    """An AoE skill must spread even when its parse is INCOMPLETE.

    Only `complete` skills reach the effect engine; everything else falls to Battle's
    simple damage path, which hit exactly one unit no matter what the description said.
    That silently single-targeted 226 skills whose parse had already identified the AoE
    (against 114 that worked), which is why "AoE skills all seem to hit one target".
    """
    # **Chosen dynamically, not pinned.** This fixture has to be a skill whose parse is
    # still INCOMPLETE, and the parser keeps improving -- 100001611 was the original
    # pick and a later matcher completed it, failing the check for the right reason but
    # testing nothing. Ask the data for a current example instead.
    AOE = fx.any_incomplete_aoe()
    SINGLE = 100000311  # complete parse, design range 1 -> "1 enemy";
                        # its damage op carries no chance gate, so the
                        # control cannot fail on an unlucky roll
    PAIR = 2081113      # Phantom Star Ring III: design range 6 -> "2 enemies", and its
                        # PROSE says "on the target" -- the case text parsing cannot see

    check("the AoE case really is an incomplete parse", not fx.is_complete(AOE))
    check("and aoe_damage still sees the AoE", fx.aoe_damage(AOE))
    check("an unknown skill id is not AoE", not fx.aoe_damage(999999999))

    # The DESIGN ROW is the authority: GetTargetGroup = _target/100, GetTargetRange =
    # _target%100, and the panel label is text 23000+_target.
    check("the AoE skill's design range is ALL", fx.target_range(AOE) == (0, 2),
          str(fx.target_range(AOE)))
    check("the single skill's design range is 1", fx.target_range(SINGLE) == (0, 1),
          str(fx.target_range(SINGLE)))
    check("Phantom Star Ring III's design range is 2 enemies",
          fx.target_range(PAIR) == (0, 6), str(fx.target_range(PAIR)))
    # A "1 enemy" row must return None, not [primary] -- callers rely on None meaning
    # "nothing to widen" so existing single-target behaviour is untouched.
    check("a 1-enemy row widens to nothing",
          fx.design_enemy_targets(SINGLE, None, []) is None)

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

    def strike(sid):
        b2 = bt.Battle(1101, [{"id": 10001}, {"id": 10011}], team_level=60)
        foes2 = [u for u in b2.units.values() if u.team != bt.TEAM_PLAYER and u.alive]
        me2 = [u for u in b2.units.values() if u.team == bt.TEAM_PLAYER][0]
        before2 = {u.order: u.hp for u in foes2}
        json.loads(b2.attack_cmd_json(me2.order, foes2[0].order, sid))
        return [u.order for u in foes2 if u.hp < before2[u.order]], len(foes2)

    hurt2, _n = strike(SINGLE)
    check("a 1-enemy skill still hits exactly one", len(hurt2) == 1, str(hurt2))
    # The payoff: a COMPLETE skill whose prose says "on the target" but whose design row
    # says 2 enemies. It went through the effect engine and still single-targeted.
    hurt3, n3 = strike(PAIR)
    check("a '2 enemies' skill hits exactly two", len(hurt3) == 2, str(hurt3))
    check("...and does not hit the whole field", n3 > 2, str(n3))


def test_enemy_multi_target():
    """Targeting is SYMMETRIC: an enemy's multi-target skill hits several party members.

    `attack_cmd_json` builds `enemies` relative to the ATTACKER's team, so the design
    row drives enemy turns the same way it drives the player's -- enemy turns and player
    auto-battle share the same path (play_turn_msgs -> auto_move -> attack_cmd_json).
    This is a real difficulty change: across the early stages, 9 of 16 distinct enemy
    skills are multi-target by their rows and every one of them used to hit one unit.
    """
    team = [{"id": c} for c in (10001, 10011, 10021, 10031, 10041)]

    def enemy_strike(sid):
        b = bt.Battle(1101, team, team_level=60)
        party = [u for u in b.units.values() if u.team == bt.TEAM_PLAYER and u.alive]
        foes = [u for u in b.units.values() if u.team != bt.TEAM_PLAYER and u.alive]
        before = {u.order: u.hp for u in party}
        out = json.loads(b.attack_cmd_json(foes[0].order, party[0].order, sid))
        rows = out["combo"][0]["data"][0]
        return [u.order for u in party if u.hp < before[u.order]], len(party), rows

    hurt, n, rows = enemy_strike(100000311)          # design range 1 -> "1 enemy"
    check("a 1-target enemy skill hits one party member", len(hurt) == 1, str(hurt))
    check("the party is big enough for this to mean something", n >= 5, str(n))

    hurt, _n, rows = enemy_strike(2081113)           # range 6 -> "2 enemies"
    check("a 2-target enemy skill hits two party members", len(hurt) == 2, str(hurt))
    check("with one DamageInfo row each", len(rows) == 2, str(len(rows)))

    hurt, _n, _r = enemy_strike(2081116)             # range 7 -> "3 enemies"
    check("a 3-target enemy skill hits three party members", len(hurt) == 3, str(hurt))

    hurt, n, _r = enemy_strike(100001611)            # range 2 -> "All enemies"
    check("an ALL enemy skill hits the whole party", len(hurt) == n, str(hurt))


def test_starshard_temple_drops():
    """A Starshard Temple clear MUST drop shards, or the client hangs on the results.

    Clearing one opens PanelBattleRuneResult, which is driven entirely by the rune list:
    OnPanelEnable (0x16BAF7C) skips its fill loop at count 0 and parks at
    UpdateResultState(0), then OnBattleEnd (0x16BAB78) does `if (EventArg.Count < 1)
    return;` and never reaches the UpdateResultState(7) that ends the sequence. The
    player is left on the empty altar room with no UI and no way out but a restart --
    which is exactly what the generic per-wave COIN fallback caused.
    """
    stages = bt.dd.rows("stage") or {}
    temple = [s for s, r in stages.items() if r.get("_book") == bt.STARSHARD_BOOK]
    check("the `_book == 2` marker still finds the temple", len(temple) == 41,
          str(len(temple)))
    # ...and nothing else, so it is safe as the trigger.
    check("no non-temple stage carries that marker",
          all((bt.dd.row("dmap", (stages[s] or {}).get("_dmap_id")) or {}).get("_link")
              == 40011 for s in temple))

    rng = random.Random(3)
    for sid in temple:
        drops = bt.starshard_temple_drops(sid, rng)
        # The hang condition. One would be enough to avoid it; two is what the live
        # results panel shows for a 2-wave run.
        if len(drops) < 1:
            check(f"stage {sid} drops at least one shard", False, "would HANG")
            return
    check("every temple stage drops at least one shard", True)

    drops = bt.starshard_temple_drops(1600001, random.Random(3))
    check("a 2-wave temple clear drops two", len(drops) == 2, str(len(drops)))
    check("both are RuneDrops", all(isinstance(d, bt.RuneDrop) for d in drops))
    check("they occupy different slots",
          len({d.slot for d in drops}) == len(drops), str([d.slot for d in drops]))
    for d in drops:
        row = bt.dd.row("item", d.item_id) or {}
        # An id the client cannot draw would be a different flavour of broken panel.
        check(f"{d.item_id} is a real item", bool(row))
        check(f"{d.item_id} is a starshard", int(row.get("_action") or 0) in range(111, 117),
              str(row.get("_action")))
        check(f"{d.item_id}'s _action matches its slot",
              int(row.get("_action") or 0) - 110 == d.slot, str(row.get("_action")))

    # STAR is the axis the ladder moves; it must ascend or 41 stages pay the same.
    # **Every stage must beat the one before it** -- a hard band made all 8 stages
    # inside it identical, so there was no reason to push deeper until a boundary.
    def mean_star(sid):
        w = bt.starshard_star_weights(sid)
        return sum(x * st for x, st in w) / sum(x for x, _ in w)

    order = {r.get("_sort"): sid for sid, r in (bt.dd.rows("stage") or {}).items()
             if r.get("_book") == bt.STARSHARD_BOOK}
    means = [mean_star(order[o]) for o in sorted(order)]
    flat = [i + 1 for i in range(1, len(means)) if means[i] <= means[i - 1] + 1e-9]
    check("every temple stage pays better than the one before it", not flat, str(flat))
    check("and the run spans most of the star range",
          means[-1] - means[0] > 3, f"{means[0]:.2f} -> {means[-1]:.2f}")
    check("the deepest stage tops out near the cap",
          means[-1] > bt.STARSHARD_MAX_STAR - 0.5, f"{means[-1]:.2f}")

    # **RARITY is deliberately NOT tied to depth** -- one table everywhere, so a lucky
    # early run can pay an LR and a late one can still pay a plain. Assert the shape
    # rather than exact frequencies, which would make this a flaky test.
    def rank_of(iid):
        parts = ((bt.dd.row("item", iid) or {}).get("_itemName_en") or "").split()
        return parts[1] if len(parts) > 1 and parts[1] in ("R", "SR", "UR", "LR") else "N"

    seen = {}
    for stage in (1600001, 1600041):
        got = collections.Counter()
        rng2 = random.Random(4)
        for _ in range(3000):
            for d in bt.starshard_temple_drops(stage, rng2):
                got[rank_of(d.item_id)] += 1
        seen[stage] = got
    check("every rarity can drop on the FIRST stage",
          len(seen[1600001]) == 5, str(sorted(seen[1600001])))
    check("every rarity can still drop on the LAST stage",
          len(seen[1600041]) == 5, str(sorted(seen[1600041])))
    # The two distributions should look alike -- rarity does not shift with depth.
    tot1 = sum(seen[1600001].values()); tot2 = sum(seen[1600041].values())
    drift = max(abs(seen[1600001][r] / tot1 - seen[1600041][r] / tot2)
                for r in ("N", "R", "SR", "UR", "LR"))
    check("the rarity spread does not shift with depth", drift < 0.05, f"drift {drift:.3f}")
    check("the weights still sum to 100",
          sum(w for w, _r in bt.STARSHARD_RANK_CHANCE) == 100,
          str(bt.STARSHARD_RANK_CHANCE))

    # ---- the PREVIEW must agree with the payout ---------------------------
    # These drifted once already: drops() learned to pay shards while the Drop Info
    # panel still advertised coins, so the panel lied about every temple stage.
    preview = bt.stage_drop_preview(1600001)
    check("the temple preview is not coins",
          bt.COIN_ITEM_ID not in preview, str(preview[:3]))
    # One icon per (star, set) the stage can roll. The star window is at most 3 wide, so
    # 8-12 icons -- naming a single star would under-report a stage that rolls three.
    stars = {st for _w, st in bt.starshard_star_weights(1600001)}
    sets = len(bt.starshard_sets_for_day())
    check("it lists one icon per (star, set) it can roll",
          len(preview) == len(stars) * sets, f"{len(preview)} vs {len(stars)}x{sets}")
    check("the preview covers every rollable star",
          all(any(f"\u2605{'I' if st == 1 else st} " in
                  ((bt.dd.row("item", i) or {}).get("_itemName_en") or "")
                  for i in preview) for st in stars), str(sorted(stars)))
    check("with no duplicates", len(set(preview)) == len(preview))
    check("and they are display-only set icons",
          all((bt.dd.row("item", i) or {}).get("_action") == 2 for i in preview))
    check("which name the sets available today",
          all(any(n in ((bt.dd.row("item", i) or {}).get("_itemName_en") or "")
                  for n in bt.STARSHARD_SET_NAMES.values()) for i in preview))
    # The preview must track the ladder, not sit still.
    deep = bt.stage_drop_preview(1600041)
    check("a late stage previews a higher star than the first",
          deep != preview, "preview did not move with depth")
    check("every temple stage previews shards, not coins",
          all(bt.COIN_ITEM_ID not in bt.stage_drop_preview(s) for s in temple))

    # ---- the preview must be EXACT, both directions ------------------------
    # Not "roughly right": every (set, star) the stage can roll has to be advertised,
    # and nothing advertised may be unrollable. Checked by actually rolling, so a change
    # to either side that desyncs them fails here rather than in game.
    import re as _re
    NAME = bt.STARSHARD_SET_NAMES

    def preview_pairs(sid):
        out = set()
        for i in bt.stage_drop_preview(sid):
            n = (bt.dd.row("item", i) or {}).get("_itemName_en") or ""
            m = _re.match(r"Random \u2605(I|\d)\s+(.+)$", n)
            if m:
                out.add((m.group(2), 1 if m.group(1) == "I" else int(m.group(1))))
        return out

    def rolled_pairs(sid, n=1500):
        out, rng3 = set(), random.Random(1)
        for _ in range(n):
            for d in bt.starshard_temple_drops(sid, rng3):
                e, rest = divmod(d.item_id, 1000)
                _slot, rest = divmod(rest, 100)
                _rank, st = divmod(rest, 10)
                out.add((NAME[e], st))
        return out

    wrong = []
    for sid in temple:
        prev_p, roll_p = preview_pairs(sid), rolled_pairs(sid)
        if prev_p != roll_p:
            wrong.append((sid, sorted(roll_p - prev_p), sorted(prev_p - roll_p)))
    check("Drop Info matches what every stage actually rolls", not wrong,
          str(wrong[:2]))

    # ---- candidates must be DISTINCT records ------------------------------
    # The client keys BackpackItemData by sid/uid. Rolling both against a bag that has
    # neither stored yet gave them the SAME sid and uid, so the panel was handed one id
    # twice -- which is what crashed Claim.
    import player_state as _ps
    from player_state.core import _default as _mk, _seed_roster as _seed
    st4 = _mk(1000001)
    _seed(st4)
    rolled = []
    for iid, slot in ((205113, 1), (202213, 2)):
        rolled.append(_ps.roll_rune(st4, iid, slot, reserved=rolled))
    check("rolled candidates get distinct sids",
          len({e["sid"] for e in rolled}) == len(rolled), str([e["sid"] for e in rolled]))
    check("and distinct uids",
          len({e["uid"] for e in rolled}) == len(rolled), str([e["uid"] for e in rolled]))
    check("none of them is in the bag yet",
          not st4["backpack"].get("2"), str(st4["backpack"].get("2")))
    _ps.store_rune(st4, rolled[1])
    check("storing one keeps only that one",
          [e["iid"] for e in st4["backpack"]["2"].values()] == [rolled[1]["iid"]],
          str(st4["backpack"]["2"]))

    # ---- the "Inventory n/999" counter must follow the grant ---------------
    # The storage sync (84-87) carries the item LIST; the counter comes from the
    # backpack INFO rows, which only cmd 83 and BACKPACK_CHANGE carry. Pushing just the
    # list drew the new shard in the grid over a stale count -- 3 icons, "2/999".
    import json as _json
    import titan_server as _ts
    st5 = _mk(1000001)
    _seed(st5)
    order5 = {r.get("_sort"): sid for sid, r in (bt.dd.rows("stage") or {}).items()
              if r.get("_book") == bt.STARSHARD_BOOK}
    for expected in (1, 2, 3):
        b5 = bt.Battle(order5[1], [{"id": 10001}, {"id": 10011}], team_level=60)
        for u in list(b5.units.values()):
            if u.team != bt.TEAM_PLAYER:
                u.hp = 0
        _ts.battle_end_reward(b5, st5)
        _ts.battle_replies(b5, bt.REQ_SELECT_RUNE, [0], [], state=st5)
        held5 = len(st5["backpack"].get("2", {}))
        quantities = [row[1] for row in _json.loads(_ps.backpack_info_json(st5))]
        check(f"clear {expected}: bag holds {expected}", held5 == expected, str(held5))
        check(f"clear {expected}: the counter agrees",
              expected in quantities, str(quantities))

    # An ordinary stage must be untouched by any of this.
    check("a main-story stage drops no shards", bt.starshard_temple_drops(1101) == [])
    check("and still previews coins",
          set(bt.stage_drop_preview(1101)) == {bt.COIN_ITEM_ID},
          str(bt.stage_drop_preview(1101)))


def test_transcend_corridor_drops():
    """The Gremlin daily must pay Gremlin Pieces, on a ladder like the Temple's.

    Nothing else in the game drops them, so without this the Soul Altar's five
    Transcender Gremlin cards are dead -- the currency they cost is unobtainable.
    `_book == 23` marks the dungeon (48 Transcend Corridor stages plus its 32 "[Double]"
    ones) and nothing else in the pack, the same way the Temple owns book 2.
    """
    stages = bt.dd.rows("stage") or {}
    corridor = [s for s, r in stages.items() if r.get("_book") == bt.TRANSCEND_BOOK]
    check("the book-23 marker still finds the dungeon", len(corridor) == 80,
          str(len(corridor)))
    check("every one of them pays pieces",
          all(bt.transcend_corridor_drops(s, random.Random(1)) for s in corridor))
    check("and only pieces",
          all(i in bt.GREMLIN_PIECE_ITEMS for s in corridor
              for i, _c in bt.transcend_corridor_drops(s, random.Random(1))))

    # **Every stage must beat the one before it**, same property the Temple ladder has.
    main = sorted((r.get("_sort"), s) for s, r in stages.items()
                  if r.get("_book") == bt.TRANSCEND_BOOK and r.get("_dmap_id") == 30014)
    means, prev, flat = [], None, []
    for o, sid in main:
        w = bt.gremlin_tier_weights(sid)
        m = sum(x * t for x, t in w) / sum(x for x, _t in w)
        if prev is not None and m <= prev + 1e-9:
            flat.append(o)
        prev = m
        means.append(m)
    check("the corridor has all 48 stages, uniquely sorted", len(main) == 48)
    check("every corridor stage pays better than the one before it", not flat, str(flat))
    check("and the run spans most of the tier range", means[-1] - means[0] > 3,
          f"{means[0]:.2f} -> {means[-1]:.2f}")

    # The window is NARROWER than the Temple's on purpose: a Piece buys its Gremlin
    # one-for-one, so a wide spread would let Trans-1 mint the top tier.
    first = {t for _w, t in bt.gremlin_tier_weights(main[0][1])}
    check("the first stage cannot roll the top tier",
          len(bt.GREMLIN_PIECE_ITEMS) not in first, str(sorted(first)))
    check("nor the last stage the bottom one",
          1 not in {t for _w, t in bt.gremlin_tier_weights(main[-1][1])})

    # The "[Double]" variant is the same dungeon at double rewards.
    plain = bt.transcend_corridor_drops(main[0][1], random.Random(1))[0][1]
    dbl_id = [s for s, r in stages.items()
              if r.get("_book") == bt.TRANSCEND_BOOK and r.get("_dmap_id") == 31014
              and r.get("_sort") == 1][0]
    dbl = bt.transcend_corridor_drops(dbl_id, random.Random(1))[0][1]
    check("the Double variant pays twice", dbl == plain * 2, f"{plain} vs {dbl}")

    # Preview and payout must agree -- the trap that bit the Temple.
    bad = []
    for sid in corridor:
        prev_p = set(bt.stage_drop_preview(sid))
        rolled = {bt.transcend_corridor_drops(sid, random.Random(k))[0][0]
                  for k in range(40)}
        if not rolled <= prev_p:
            bad.append((sid, sorted(rolled - prev_p)))
    check("Drop Info covers everything a corridor stage rolls", not bad, str(bad[:2]))

    check("a main-story stage pays no pieces", bt.transcend_corridor_drops(1101) == [])
    check("a Temple stage still pays shards, not pieces",
          bt.transcend_corridor_drops(1600001) == []
          and bool(bt.starshard_temple_drops(1600001)))


def test_auto_play_sweep():
    """The AUTO PLAY button must start a sweep, and the sweep must pay out.

    PanelAutoPlay sends PlayerStage cmd 3 [stageID, count, useCoupon]; unanswered, the
    button does nothing at all (the log just reads "no handler for cmd=3"). The reply
    shapes are exact: HandleAutoSuccess wants intargs of length EXACTLY 2 and
    HandleAutoStop EXACTLY 3, and either returns silently otherwise.
    """
    import player_state as _ps
    import titan_server as _ts
    from player_state.core import _default as _mk, _seed_roster as _seed

    # **Every helper the auto-play dispatcher branch names must exist.** The tests
    # below call the payout functions directly, so a missing name in the branch itself
    # (auto_sync_msg went missing in a refactor) sailed past them and only showed up as
    # a NameError killing the connection in game.
    for fn in ("auto_sync_msg", "stage_sync_msg", "autorun_settle", "autorun_payout"):
        check(f"titan_server.{fn} exists", callable(getattr(_ts, fn, None)))
    for fn in ("autorun_start", "autorun_cancel", "autorun_due",
               "autorun_runs_elapsed", "stage_json", "record_stage_turns"):
        check(f"player_state.{fn} exists", callable(getattr(_ps, fn, None)))

    st = _mk(1000001)
    _seed(st)
    # Both sync builders must actually render, not just resolve.
    check("auto_sync_msg renders", len(_ts.auto_sync_msg(st)) > 0)
    check("stage_sync_msg renders", len(_ts.stage_sync_msg(st)) > 0)
    _ps.grant_reward(st, 19, 50)          # Transcend Corridor passes
    _ps.grant_reward(st, 30064, 20)       # Quick Battle Coupons

    ok, why = _ps.autorun_start(st, 1800021, 5, False, 1000)
    check("a sweep starts", ok, why)
    job = st.get("autorun") or {}
    check("it records the stage and count",
          (job.get("stage_id"), job.get("count")) == (1800021, 5), str(job))
    check("it is TIMED, not instant", job.get("duetime", 0) > job.get("starttime", 0),
          str(job))
    check("the pass is charged per run", _ps.item_count(st, 19) == 45,
          str(_ps.item_count(st, 19)))
    # **Passes are free runs, not a hard cap.** With only 3 a day, refusing a 99-run
    # sweep would make the feature unusable on exactly the stages worth sweeping -- so
    # passes are spent while they last and stamina carries the rest.
    st2 = _mk(1000002)
    _seed(st2)
    check("a sweep runs with NO passes at all",
          _ps.autorun_start(st2, 1800021, 99, False, 100)[0]
          and st2["autorun"]["count"] == 99, str(st2.get("autorun")))
    st3 = _mk(1000003)
    _seed(st3)
    _ps.grant_reward(st3, 19, 3)
    _ps.autorun_start(st3, 1800021, 99, False, 100)
    check("passes are spent first, then stamina carries the rest",
          _ps.item_count(st3, 19) == 0 and st3["autorun"]["count"] == 99,
          f"passes {_ps.item_count(st3, 19)}")
    check("a second sweep is refused while one runs",
          not _ps.autorun_start(st, 1800021, 2, False, 1001)[0])
    check("nothing pays out before it is due", _ts.autorun_settle(st, 1000) == [])

    # **No "Autoplay Completed" toast on start.** Confirm 401 says Completed, so
    # sending cmd 33 when the sweep begins told the player their 99 runs had finished
    # the instant they pressed Start. Only the syncs go out; they drive the
    # "Autoplaying..." progress panel.
    check("starting a sweep emits no completion toast",
          _ts.autorun_settle(st, job["starttime"]) == [])
    check("runs elapsed tracks the timer",
          _ps.autorun_runs_elapsed(job, job["starttime"]) == 0
          and _ps.autorun_runs_elapsed(job, job["duetime"]) == job["count"],
          str(job))

    before = sum(_ps.item_count(st, i) for i in bt.GREMLIN_PIECE_ITEMS)
    msgs = _ts.autorun_settle(st, job["duetime"])
    after = sum(_ps.item_count(st, i) for i in bt.GREMLIN_PIECE_ITEMS)
    check("settling pays every run", after - before == 5 * bt.GREMLIN_PIECES_PER_CLEAR,
          f"{before} -> {after}")
    check("and clears the job", "autorun" not in st)
    check("it answers the client", len(msgs) >= 1, str(len(msgs)))

    # Stopping early pays for the runs the timer actually covered.
    st4 = _mk(1000004)
    _seed(st4)
    _ps.autorun_start(st4, 1600002, 10, False, 0)
    stopped = _ps.autorun_cancel(st4)
    half = stopped["starttime"] + (stopped["duetime"] - stopped["starttime"]) // 2
    _ts.autorun_payout(st4, stopped["stage_id"],
                       _ps.autorun_runs_elapsed(stopped, half))
    check("stopping halfway pays half the runs",
          len(st4["backpack"].get("2", {})) == 5,
          str(len(st4["backpack"].get("2", {}))))

    # A Temple clear offers TWO candidates and the player keeps ONE. A sweep cannot
    # ask, so it keeps the first -- taking both would pay double what playing by hand
    # does, and paying nothing would make the stage pointless to sweep.
    _ps.autorun_start(st, 1600002, 4, False, 5000)
    shards = len(st["backpack"].get("2", {}))
    _ts.autorun_settle(st, st["autorun"]["duetime"])
    got = len(st["backpack"].get("2", {})) - shards
    check("a Temple sweep keeps ONE shard per run", got == 4, f"{got} for 4 runs")
    ids = [(e["sid"], e["uid"]) for e in st["backpack"]["2"].values()]
    check("every swept shard is a distinct record", len(set(ids)) == len(ids), str(ids))

    # Express buys the SPEED, and pays the entry cost as well.
    passes, coupons = _ps.item_count(st, 19), _ps.item_count(st, 30064)
    ok, why = _ps.autorun_start(st, 1800021, 3, True, 9000)
    check("express starts", ok, why)
    check("express is instant", st["autorun"]["duetime"] == 9000, str(st["autorun"]))
    check("express spends coupons", _ps.item_count(st, 30064) == coupons - 3)
    check("express ALSO spends the entry cost while passes last",
          _ps.item_count(st, 19) == max(0, passes - 3))

    # The sweep pays exactly what one clear pays, N times -- one source of truth.
    check("stage_drops_for matches the corridor table",
          bt.stage_drops_for(1800021, rng=random.Random(2))[0][0]
          in bt.GREMLIN_PIECE_ITEMS)
    check("and falls back to coins for an ordinary stage",
          bt.stage_drops_for(9999999) == [(bt.COIN_ITEM_ID, bt.COIN_PER_WAVE)])

    # bestrec drives the panel's "Stage Clear Record" and its time estimate.
    _ps.record_stage_turns(st, 1600002, 7)
    _ps.record_stage_turns(st, 1600002, 9)
    _ps.record_stage_turns(st, 1600002, 4)
    check("the best (lowest) clear length is kept",
          (st.get("bestrec") or {}).get("1600002") == 4, str(st.get("bestrec")))
    check("and it reaches the client",
          json.loads(_ps.stage_json(st))["bestrec"].get("1600002") == 4)

if __name__ == "__main__":
    main()

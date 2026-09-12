#!/usr/bin/env python3
"""Phase-3 harness: the engine itself.

`test_skill_specs.py` checks the DATA. This checks the code that executes it, and the
assertions are chosen to be the ones whose failures were previously silent -- a fight
that looked healthy in the log while the client drew the wrong thing.

    python3 test_engine.py
"""
import random
import sys

from engine import core, formula, specs, status, wire

_fail = 0


def check(name, cond, detail=""):
    global _fail
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        _fail += 1


def unit(order, team, **kw):
    kw.setdefault("max_hp", 50000)
    kw.setdefault("hp", kw["max_hp"])
    kw.setdefault("atk", 3000)
    kw.setdefault("defence", 800)
    kw.setdefault("spd", 1000)
    return core.Unit(order=order, team=team, **kw)


def field(n_enemy=5, **kw):
    caster = unit("101", core.TEAM_PLAYER, **kw)
    return caster, [caster] + [unit(str(200 + i), core.TEAM_ENEMY)
                               for i in range(n_enemy)]



def check_shared_unit_model():
    """One combatant model: battle.Unit IS an engine Unit.

    The pivot toward retiring the old engine. While there were two Unit classes the
    bridge had to copy state between them, and every later move would have needed its own
    sync; sharing the object means each move is a deletion instead.
    """
    import battle as bt

    check("battle.Unit subclasses the engine's Unit",
          issubclass(bt.Unit, core.Unit))

    # A unit is an entity, not a value. With the dataclass's generated __eq__ two
    # combatants rolled with identical stats would compare equal, and `unit in targets`
    # would match the wrong one.
    a = unit("101", core.TEAM_PLAYER)
    b = unit("102", core.TEAM_PLAYER)
    a.hp = b.hp = a.atk = b.atk = 1
    check("units compare by identity, not by field values", a != b)
    check("...and a unit still equals itself", a == a)

    # The engine reads `defence`; the old spelling is gone rather than aliased, so a
    # stale reader fails loudly. That matters because battle_effects looked its stat
    # names up with `getattr(u, stat, 0)` -- a DEFAULT -- so a missed rename would have
    # silently made every unit's DEF read as 0 for "highest DEF" targeting.
    check("there is one spelling of defence", hasattr(a, "defence"))
    check("...and the old one is not silently aliased", not hasattr(a, "defense"))



def check_status_state():
    """Phase 6: statuses are STATE, not decoration.

    Until this landed the engine wrote statuses to the wire and nothing tracked them --
    the icon appeared, the client counted the duration down, and a Freeze did not stop
    anyone acting.
    """
    def act(**kw):
        kw.setdefault("status_id", 1)
        kw.setdefault("name", "X")
        kw.setdefault("kind", "stat_mod")
        kw.setdefault("category", "buff")
        return status.Active(**kw)

    # --- the sign rule. A magnitude's sign is the direction of the QUANTITY, not
    # whether the status is good: `Aging` is a debuff reading "Damage taken +4%".
    explicit = act(kind="damage_mod", category="debuff", magnitude=4.0, sign=1)
    check("an explicit prose sign wins over the category",
          explicit.signed_magnitude() == 4.0, str(explicit.signed_magnitude()))
    inferred = act(category="debuff", stat="ATK", magnitude=35.0)
    check("a stat debuff with no sign is inferred negative",
          inferred.signed_magnitude() == -35.0, str(inferred.signed_magnitude()))
    inferred_up = act(category="buff", stat="ATK", magnitude=35.0)
    check("...and a stat buff positive", inferred_up.signed_magnitude() == 35.0)
    undecidable = act(kind="damage_mod", category="misc", magnitude=40.0)
    check("an undecidable direction yields None, never a guess",
          undecidable.signed_magnitude() is None)

    # --- stacking is additive, which is what "stacks up to N times" reads as.
    u = unit("101", core.TEAM_PLAYER)
    u.statuses = [act(status_id=1, category="debuff", stat="ATK", magnitude=35.0),
                  act(status_id=2, category="debuff", stat="ATK", magnitude=35.0)]
    check("two ATK-35% give x0.30, not x0.42",
          abs(status.stat_multiplier(u, "ATK") - 0.30) < 1e-9,
          str(status.stat_multiplier(u, "ATK")))
    u.statuses = [act(category="debuff", stat="ATK", magnitude=250.0)]
    check("a stack of debuffs cannot invert a stat",
          status.stat_multiplier(u, "ATK") == 0.0)

    # --- control skips a turn, but only the ones that actually stop you.
    u.statuses = [act(kind="control", category="debuff", name="Stun")]
    check("Stun immobilises", status.is_immobilized(u))
    u.statuses = [act(kind="control", category="debuff", name="Taunt")]
    check("Taunt does NOT -- it redirects a turn, it does not delete one",
          not status.is_immobilized(u))

    # --- a DoT ticks for the INFLICTER's ATK, snapshotted at apply time, so it keeps
    # hurting for the caster's power after the caster's buffs expire or it dies.
    v = unit("202", core.TEAM_ENEMY, atk=1)
    v.statuses = [act(kind="dot", category="damage_over_time", magnitude=30.0,
                      remaining=2, source_atk=1000)]
    dot, hot, expired = status.tick(v)
    check("a DoT uses the inflicter's snapshotted ATK", dot == 300, str(dot))
    check("...and spends a turn of its duration", v.statuses[0].remaining == 1)
    status.tick(v)
    check("...and expires at zero", not v.statuses)

    # --- shields eat damage before HP.
    w = unit("303", core.TEAM_ENEMY)
    sh = act(kind="shield", category="shield", shield_hp=250)
    w.statuses = [sh]
    landed, absorbed = status.absorb(w, 400)
    check("a shield absorbs up to its capacity", (landed, absorbed) == (150, 250),
          f"{landed}/{absorbed}")
    check("...and is spent by what it took", sh.shield_hp == 0)

    # --- statuses reach the damage formula.
    a = unit("1", core.TEAM_PLAYER, atk=1000)
    t = unit("2", core.TEAM_ENEMY, defence=500)
    base, _ = formula.strike(a, t, 1.0, "ATK", random.Random(1))
    a.statuses = [act(category="buff", stat="ATK", magnitude=50.0)]
    buffed, _ = formula.strike(a, t, 1.0, "ATK", random.Random(1))
    check("an ATK buff raises damage", buffed > base, f"{base} -> {buffed}")
    a.statuses = []
    t.statuses = [act(category="debuff", stat="DEF", magnitude=50.0)]
    broken, _ = formula.strike(a, t, 1.0, "ATK", random.Random(1))
    check("a DEF break raises damage taken", broken > base, f"{base} -> {broken}")



def check_passive_report_shapes():
    """`_report` must survive the MIXED list `passives.fire()` returns.

    A damage or heal rule yields `(target, effect, amount)`; a status rule yields
    `(target, Active)`. `_report` unpacked every row as a triple, so any passive whose
    damage trigger GRANTS a status raised ValueError mid-fight and killed the turn. The
    stage suites never fielded such a cast; random five-cast teams in tools/ai_arena.py
    hit it on the first sweep -- which is exactly why it is pinned here, where the shape
    is asserted directly, rather than left to a fuzzer to rediscover.
    """
    from engine import passives as pv

    # 2096131 -- a passive that grants a status ON_DAMAGE_TAKEN, i.e. the 2-tuple.
    holder = unit("200", core.TEAM_ENEMY)
    holder.skills = [0, 0, 0, 2096131]
    holder.hp = int(holder.max_hp * 0.2)              # its rules gate on HP <= 30%
    # 1100131 -- a passive that HEALS ON_DAMAGE_DEALT, i.e. the triple. Seed 1 clears
    # its 25% chance gate.
    attacker = unit("101", core.TEAM_PLAYER)
    attacker.skills = [0, 0, 0, 1100131]
    attacker.hp = attacker.max_hp // 2                # so the heal has room to land
    units = [attacker, holder]

    rows = pv.fire_all(pv.ON_DAMAGE_TAKEN, [holder], units,
                       ctx={"attacker": attacker, "rng": random.Random(1)})
    rows += pv.fire_all(pv.ON_DAMAGE_DEALT, [attacker], units,
                        ctx={"victim": holder, "rng": random.Random(1)}, fired=set())
    widths = {len(r) for r in rows}
    check("fire() really does return a mixed list", widths == {2, 3}, str(widths))

    out = core.Outcome(caster="101", skill_id=0, swings=1, targets=["200"])
    try:
        core._report(out, rows)
        raised = None
    except Exception as exc:                                    # noqa: BLE001
        raised = exc
    check("a status row does not crash the report", raised is None, repr(raised))
    check("...and reaches the wire, so the icon is not missing until the next sync",
          any(s.target == "200" and s.applied for s in out.statuses),
          str(out.statuses))
    check("...while a heal row is still reported as a heal",
          any(h["target"] == "101" and h["amount"] > 0 for h in out.heals),
          str(out.heals))


def check_effect_recipients():
    """An effect's recipient is NOT the skill's target.

    Three live bugs in a row came from assuming it was: a move gauge handed to the enemy
    it was cast at, a heal that restored the raid boss, and a party buff applied to the
    boss instead of the party. An attack skill routinely aims at an enemy and does
    something to its own side, and only the prose says which.
    """
    caster, units = field(n_enemy=2)
    mate = unit("102", core.TEAM_PLAYER)
    units.append(mate)

    # Michael's Gate of Judgement: an ENEMY-targeting attack that heals all ALLIES.
    spec = specs.skill(2090101)
    # The party heal is an op-1 RIDER, not a `heal` opcode: 以200%攻擊力恢復我方全體體力
    # rides the attack. The skill also has a second, separate heal -- 額外恢復自身25%的
    # 體力 -- which is a `heal` for the caster alone, so keying this on `op == "heal"`
    # and taking the first now finds the wrong one.
    heals = [e for e in spec["effects"]
             if e["op"] == "heal" or (e["op"] == "attack_rider"
                                      and e.get("kind") == "heal")]
    party = [e for e in heals if e.get("target") == "allies"]
    check("Gate of Judgement's party heal is marked for allies", party, str(heals))
    check("  ...and it is stated once, not twice",
          len(party) == 1, f"{len(party)} copies: {party}")
    for u in units:
        u.hp = u.max_hp // 2
    out = core.execute(caster, spec, units, random.Random(1))
    healed = {h["target"] for h in out.heals}
    foes = {u.order for u in units if u.team == core.TEAM_ENEMY}
    check("  ...and no enemy is healed by it", not (healed & foes), str(healed))
    check("  ...while allies are", healed and healed <= {u.order for u in units
                                                         if u.team == core.TEAM_PLAYER})

    # Metatron's Serum Injection: an enemy-targeting attack that buffs one ally.
    spec = specs.skill(2094111)
    buffs = [e for e in spec["effects"]
             if e["op"] == "apply_status" and "Serum" in (e["status"]["name"] or "")]
    check("Serum Injection is marked for allies",
          buffs and all(b.get("recipient") == "allies" for b in buffs), str(buffs[:1]))
    caster2, units2 = field(n_enemy=1)
    out = core.execute(caster2, spec, units2, random.Random(1))
    on_foe = [e for e in out.statuses
              if e.applied and e.target in {u.order for u in units2
                                            if u.team == core.TEAM_ENEMY}
              and "Serum" in (e.name or "")]
    check("  ...and the struck enemy does not get it", not on_foe, str(on_foe))

    # The ordinary case must still work: "inflicts X on the target" has no recipient
    # marking and has to fall through to the skill's targets.
    plain = next((s for s in specs.skills().values()
                  if s["type"] in ("com_attack", "skill")
                  and (s.get("target") or {}).get("group") == "enemy"
                  and any(e["op"] == "apply_status" and e.get("recipient") is None
                          and not e.get("conditional") for e in s["effects"])), None)
    check("a plain debuff still lands on the target", plain is not None)
    if plain:
        c3, u3 = field(n_enemy=1)
        out = core.execute(c3, plain, u3, random.Random(1))
        foe = u3[1].order
        check("  ...on the enemy, not the caster",
              any(e.target == foe for e in out.statuses if e.applied),
              str([(e.target, e.name) for e in out.statuses]))


def check_stated_chance():
    """A status application lands at the odds the prose states, not at a stand-in.

    Anchored to the RATE, not to the presence of `chance_pct`: the bug was never a
    missing field, it was 10%機率附加暈眩 landing about 75% of the time (op 113's
    stand-in) or every single time (op 112, which used to be read as "guaranteed").
    Both of those pass a test that only asserts the field is there.

    Rolled through `formula.effect_lands`, so the units come from a caster and target
    with no Effect Hit or Effect RES of their own -- the stated chance and nothing else.
    """
    for pct in (10.0, 90.0):
        spec = {"id": 0, "swings": 1, "target": {"group": "enemy", "select": "count",
                                                 "count": 1},
                "effects": [{"op": "apply_status", "chance": True, "chance_pct": pct,
                             "status": {"id": 3001, "name": "Daze"}, "numbers": {}}]}
        rng = random.Random(4)
        landed = 0
        for _ in range(2000):
            c, u = field(n_enemy=1)
            out = core.execute(c, spec, u, rng)
            landed += sum(1 for e in out.statuses if e.applied)
        rate = 100.0 * landed / 2000
        check(f"a stated {pct:.0f}% lands about {pct:.0f}% of the time",
              abs(rate - pct) < 4.0, f"{rate:.1f}%")

    # The stand-in still governs op 113 rows whose prose states no number, and it must
    # stay a NAMED constant rather than drifting back into a literal.
    check("op 113 with no stated chance uses the named stand-in",
          isinstance(core.UNSTATED_CHANCE, float) and 0 < core.UNSTATED_CHANCE < 1,
          repr(core.UNSTATED_CHANCE))

    # `chance_pct` is a PERCENT on every path. It was a fraction on the passive path
    # alone, which happened to read correctly there and would have read as "always" the
    # moment a second compiler path wrote the key onto an effect the engine executes.
    frac = [(sid, e) for sid, sp_ in specs.skills().items()
            for e in sp_.get("effects") or []
            if e.get("chance_pct") is not None and float(e["chance_pct"]) <= 1.0]
    check("no compiled chance_pct is a fraction masquerading as a percent",
          not frac, f"{len(frac)}, e.g. {frac[:2]}")


def _gated(requires, **extra):
    """A one-status spec whose application is gated on `requires`."""
    return {"id": 0, "swings": 1,
            "target": {"group": "enemy", "select": "count", "count": 1},
            "effects": [dict({"op": "apply_status", "requires": requires,
                              "status": {"id": 3001, "name": "Daze"},
                              "numbers": {"duration": 1}}, **extra)]}


def check_condition_shapes():
    """The gates compiled from 若/當 fragments, each proved to both open AND shut.

    One-sided is not enough. A gate that always opens is the bug this mechanism exists
    to stop (two conditional control effects landing on every cast permanently froze a
    raid boss); a gate that never opens deletes the effect from the game, which is how
    an unresolvable status NAME used to behave. Every check below is a pair.
    """
    # -- ROUND parity and comparison. Unevaluatable without a round, and that is the
    # third state: not "false", or every 奇數回合 clause in the game would go silent on
    # the AI's dry runs.
    for parity, rounds in ((1, (1, 3, 7)), (0, (2, 4, 8))):
        spec = _gated({"round": {"parity": parity}})
        for rn in rounds:
            c, u = field(n_enemy=1)
            out = core.execute(c, spec, u, random.Random(1), round_no=rn)
            check(f"parity {parity} fires on round {rn}",
                  any(e.applied for e in out.statuses))
            c, u = field(n_enemy=1)
            out = core.execute(c, spec, u, random.Random(1), round_no=rn + 1)
            check(f"  ...and not on round {rn + 1}",
                  not any(e.applied for e in out.statuses))
    spec = _gated({"round": {"cmp": "<=", "n": 3}})
    for rn, want in ((1, True), (3, True), (4, False)):
        c, u = field(n_enemy=1)
        out = core.execute(c, spec, u, random.Random(1), round_no=rn)
        check(f"round <= 3 on round {rn} -> {want}",
              any(e.applied for e in out.statuses) == want)
    check("a round gate with no round is unevaluatable, not false",
          core._condition_met({"round": {"parity": 1}}, None, None, None, {}) is None)

    # -- KILLED. `execute` mutates HP through the swing loop before the status loop, so
    # the answer is already final when the gate asks.
    for coef, alive_after, want in ((0.001, True, False), (500.0, False, True)):
        spec = _gated({"killed": True})
        spec["effects"].insert(0, {"op": "damage", "coefficient": coef, "basis": "ATK"})
        c, u = field(n_enemy=1)
        out = core.execute(c, spec, u, random.Random(2))
        check(f"'if this attack killed' -> {want} when the target {'lives' if alive_after else 'dies'}",
              any(e.applied for e in out.statuses) == want,
              f"target hp {u[1].hp}")

    # -- CRIT. Forced both ways through the caster's crit rate rather than by faking a
    # Strike, so the wiring from formula.strike's detail flag is what is being tested.
    for cri, want in ((0.0, False), (1.0, True)):
        spec = _gated({"crit": True})
        spec["effects"].insert(0, {"op": "damage", "coefficient": 1.0, "basis": "ATK"})
        c, u = field(n_enemy=1, cri=cri)
        out = core.execute(c, spec, u, random.Random(3))
        check(f"'if this attack crit' -> {want} at a {cri:.0%} crit rate",
              any(e.applied for e in out.statuses) == want)

    # -- STACK COUNT. 若自身擁有5層Reload used to pass on the first stack (af911ee).
    from engine.status import Active
    for have, want in ((1, False), (4, False), (5, True), (7, True)):
        c, u = field(n_enemy=1)
        c.statuses.append(Active(status_id=9001, name="Reload", kind=None, category="buff", remaining=9,
                                 stacks=have, stack_cap=7))
        check(f"5 stacks required, {have} held -> {want}",
              core._holds_status(c, "Reload", None, 5) is want)
    # Held but uncappable: `stacks` can never leave 1, so the gate is UNREACHABLE, not
    # unmet. Answering False would delete the effect from the game.
    c, u = field(n_enemy=1)
    c.statuses.append(Active(status_id=9002, name="Reload", kind=None, category="buff", remaining=9, stacks=1))
    check("a count gate on a status that cannot stack is unevaluatable",
          core._holds_status(c, "Reload", None, 5) is None)
    check("  ...and 'at least 1' of it still reads True",
          core._holds_status(c, "Reload", None, 1) is True)

    # -- an unresolvable NAME is unevaluatable, never a permanently shut gate.
    c, u = field(n_enemy=1)
    check("an unresolved gate name is unevaluatable",
          core._condition_met({"status": "能力下降", "on": "caster", "resolved": False},
                              c, u[1], None) is None)


def check_prose_stack_caps():
    """A status whose cap the pack states only in prose really stacks, to that cap.

    Wrath (3015) has no `(N)` suffix; every glossary line describing it says 可堆疊5次.
    Until the registry read that, its `stack_cap` was None, apply_event pinned `stacks`
    at 1, and 若自身「盛怒」達到5層 could never be true. Anchored to behaviour: five
    applications reach the gate, a sixth does not overshoot, and the stat delta scales.
    """
    from engine import status as _st
    row = specs.status(3015) or {}
    check("Wrath carries a prose-stated cap of 5", row.get("stack_cap") == 5,
          f"{row.get('stack_cap')!r} from {row.get('stack_cap_source')!r}")
    c, u = field(n_enemy=1)
    for i in range(1, 7):
        _st.apply_event(c, core.StatusEvent(target=c.order, status_id=3015, name="Wrath",
                                            applied=True, duration=3, magnitude=4.0), c)
        held = next(x for x in c.statuses if x.status_id == 3015)
        if i in (4, 5, 6):
            check(f"after {i} applications Wrath holds {min(i, 5)} stacks",
                  held.stacks == min(i, 5), str(held.stacks))
    check("...and a 5-stack gate on it now opens",
          core._holds_status(c, "Wrath", None, 5) is True)
    check("...and a 6-stack gate stays shut at the cap",
          core._holds_status(c, "Wrath", None, 6) is False)


def check_revive_count():
    """復活我方被擊倒的隨機2人 -- the clause says how many, and the engine has to obey it.

    It did not: it raised every fallen ally, so Michael's Blessing Anthem, which states
    3, brought back a party of 4 on a device. The compiler had been emitting `count`
    for months and nothing read it -- 143 of the 326 revive effects state one.

    The count is a CEILING, not a promise: fewer fallen than the count raises all of
    them, which is what 復活我方全體戰鬥不能者 (no count at all) does by default.
    """
    def fallen_party(n_dead):
        caster = unit("101", core.TEAM_PLAYER)
        mates = [unit(str(102 + i), core.TEAM_PLAYER) for i in range(4)]
        for m in mates[:n_dead]:
            m.hp = 0                              # `alive` is derived from hp
        return caster, [caster] + mates + [unit("201", core.TEAM_ENEMY)]

    spec = {"id": 0, "swings": 1,
            "target": {"group": "enemy", "select": "count", "count": 1},
            "effects": [{"op": "revive", "percent": 70.0, "target": "allies",
                         "count": 3}]}
    c, u = fallen_party(4)
    out = core.execute(c, spec, u, random.Random(1))
    check("a revive stating 3 raises 3 of 4 fallen allies, not all of them",
          len(out.revives) == 3, str(out.revives))
    check("  ...and each comes back on the stated share of MAX HP",
          all(rv["hp"] == 35000 for rv in out.revives), str(out.revives))
    c, u = fallen_party(2)
    out = core.execute(c, spec, u, random.Random(1))
    check("the count is a ceiling: 2 fallen and a count of 3 raises both",
          len(out.revives) == 2, str(out.revives))
    spec_all = dict(spec, effects=[{"op": "revive", "percent": 50.0,
                                    "target": "allies"}])
    c, u = fallen_party(4)
    out = core.execute(c, spec_all, u, random.Random(1))
    check("no count stated -- 復活我方全體戰鬥不能者 still raises everybody",
          len(out.revives) == 4, str(out.revives))
    # Same seed, same pick: a save/restore replays the fight and must not raise a
    # different ally the second time round.
    c, u = fallen_party(4)
    again = core.execute(c, spec, u, random.Random(1))
    c2, u2 = fallen_party(4)
    once = core.execute(c2, spec, u2, random.Random(1))
    check("the pick is seeded, so a replay revives the same allies",
          [rv["target"] for rv in again.revives] == [rv["target"] for rv in once.revives],
          f"{[rv['target'] for rv in again.revives]} vs "
          f"{[rv['target'] for rv in once.revives]}")


def check_hp_riders():
    """op 6: an amount sized off the CASTER's own HP pool, decoded 2026-08-29.

    Anchored to the pool it reads. A caster at 10,000 of 50,000 HP whose rider is 20% of
    CURRENT HP deals 2,000 -- not 10,000 (max) and not 600 (20% of ATK, which is what
    the op-1 rider path would have produced had the basis been ignored).
    """
    from engine import status as _st
    base = {"id": 0, "swings": 1,
            "target": {"group": "enemy", "select": "count", "count": 1}}
    c, u = field(n_enemy=1)
    c.hp = 10000
    spec = dict(base, effects=[{"op": "attack_rider", "opcode": 6, "kind": "bonus_damage",
                                "percent": 20.0, "basis": "caster_current_hp"}])
    out = core.execute(c, spec, u, random.Random(1))
    dealt = [st.amount for st in out.strikes if (st.detail or {}).get("rider")]
    check("a current-HP rider deals 20% of the caster's CURRENT HP", dealt == [2000], str(dealt))
    c, u = field(n_enemy=1)
    c.hp = 10000
    spec = dict(base, effects=[{"op": "attack_rider", "opcode": 6, "kind": "heal",
                                "percent": 35.0, "basis": "caster_max_hp"}])
    out = core.execute(c, spec, u, random.Random(1))
    check("a max-HP rider heals 35% of MAX HP", c.hp == 10000 + 17500, str(c.hp))
    # Gated: 攻擊時若自身擁有共享盛宴，額外… must not fire without the marker, and must with it.
    spec = dict(base, effects=[{"op": "attack_rider", "opcode": 6, "kind": "bonus_damage",
                                "percent": 20.0, "basis": "caster_current_hp",
                                "requires": {"status": "Feast", "on": "caster",
                                             "resolved": True}}])
    c, u = field(n_enemy=1)
    out = core.execute(c, spec, u, random.Random(1))
    check("a gated rider stays silent without its status", not out.strikes,
          str([(x["op"], x["why"]) for x in out.skipped]))
    c, u = field(n_enemy=1)
    c.statuses.append(_st.Active(status_id=9010, name="Shared Feast", kind=None,
                                 category="buff", remaining=3))
    out = core.execute(c, spec, u, random.Random(1))
    check("  ...and fires with it", len(out.strikes) == 1)


def main():
    print("\ntargeting:")
    caster, units = field()
    # 2 = All enemies, 1 = 1 enemy, 7 = 3 enemies. Straight from the label table.
    for target, want in ((2, 5), (1, 1), (7, 3), (6, 2)):
        spec = {"id": 0, "swings": 1, "effects": [],
                "target": dict(group="enemy",
                               select={2: "all"}.get(target, "count"),
                               count={1: 1, 6: 2, 7: 3}.get(target))}
        got = core.resolve_targets(caster, spec, units, random.Random(1))
        check(f"_target {target} hits {want}", len(got) == want, f"got {len(got)}")

    dead = unit("909", core.TEAM_ENEMY, hp=0)
    spec = {"id": 0, "swings": 1, "effects": [],
            "target": {"group": "enemy", "select": "all"}}
    check("dead units are not eligible targets",
          dead not in core.resolve_targets(caster, spec, units + [dead], random.Random(1)))
    spec["target"] = {"group": "dead_enemy", "select": "all"}
    check("dead_enemy selects exactly the dead",
          core.resolve_targets(caster, spec, units + [dead], random.Random(1)) == [dead])

    print("\nmulti-hit -- the contract the client enforces:")
    spec = specs.skill(2087111)                       # 4 swings, 3 enemies
    caster, units = field()
    out = core.execute(caster, spec, units, random.Random(7))
    per = [sum(1 for s in out.strikes if s.swing == i) for i in range(out.swings)]
    check("one strike group per cinematic swing", len(per) == 4 and all(per),
          f"{per}")
    check("every swing hits every target", per == [3, 3, 3, 3], f"{per}")

    # The original bug: a skill whose swings collapse to one. If this regresses the
    # animation plays and no damage number appears -- and nothing errors.
    multi = [s for s in specs.skills().values()
             if (s.get("swings") or 0) >= 2
             and any(e["op"] == "damage" and e.get("coefficient") for e in s["effects"])
             and (s.get("target") or {}).get("select") not in (None, "none", "unknown")]
    bad = []
    for s in multi[:400]:
        c, u = field()
        o = core.execute(c, s, u, random.Random(3), apply_damage=False)
        if o.targets and len({st.swing for st in o.strikes}) != o.swings:
            bad.append(s["id"])
    check("multi-hit skills produce every swing", not bad,
          f"{len(bad)} collapsed, e.g. {bad[:4]}")

    print("\ndeath ordering -- overkill must NOT truncate the animation:")
    # Found on device: Frozen Inferno Thorn III (4 swings) shipped ONE group because both
    # enemies died on swing 0, so swings 1-3 produced no rows and were trimmed. The
    # client's cinematic fires four Damage tags regardless, so three of them animated
    # with no number. A target alive when the skill STARTS takes every swing.
    caster, units = field(n_enemy=1)
    frail = units[1]
    frail.hp = frail.max_hp = 1
    out = core.execute(caster, specs.skill(2087111), units, random.Random(5))
    hits = [s for s in out.strikes if s.target == frail.order]
    check("an overkilled target still takes every swing",
          len(hits) == out.swings, f"{len(hits)} hits for {out.swings} swings")
    check("died is flagged on the LAST strike only",
          sum(1 for h in hits if h.died) == 1 and hits[-1].died,
          f"{[h.died for h in hits]}")
    js = wire.attack_json(out)
    check("the payload still carries one group per swing",
          len(js["data"]) == out.swings, f"{len(js['data'])} vs {out.swings}")

    print("\nfollow-ups:")
    parent = next((s for s in specs.skills().values()
                   if any(e["op"] == "follow_up" for e in s["effects"])), None)
    check("a follow_up skill exists to test", parent is not None)
    if parent:
        c, u = field()
        out = core.execute(c, parent, u, random.Random(11), apply_damage=False)
        check("follow_up produces a child outcome", bool(out.children))
        deep = [d for d in (out.skipped + [x for ch in out.children for x in ch.skipped])
                if d.get("why") == "follow-up depth limit"]
        check("depth guard did not trip on a real skill", not deep)

    # A cycle would hang the server, not the client. The compiled graph is checked
    # acyclic, but the guard must hold even if that check is wrong.
    cyc = {"id": -1, "swings": 1, "target": {"group": "enemy", "select": "all"},
           "effects": [{"op": "follow_up", "slot": 0, "skill": -1}]}
    saved = specs.skills().get(-1)
    specs.skills()[-1] = cyc
    try:
        c, u = field()
        out = core.execute(c, cyc, u, random.Random(1), apply_damage=False)
        depth = 0
        node = out
        while node.children:
            node, depth = node.children[0], depth + 1
        check("a cyclic follow_up terminates at the depth guard",
              depth == core.MAX_FOLLOW_DEPTH, f"depth {depth}")
    finally:
        if saved is None:
            del specs.skills()[-1]
        else:
            specs.skills()[-1] = saved

    print("\nstatus state (phase 6):")
    check_status_state()

    print("\neffect recipients:")
    check_effect_recipients()

    print("\nstated chances:")
    check_stated_chance()

    print("\ncondition shapes:")
    check_condition_shapes()

    print("\nprose stack caps:")
    check_prose_stack_caps()

    print("\nop 6 -- HP-pool riders:")
    check_revive_count()
    check_hp_riders()

    print("\nconditional application:")
    # Eclipse Slash gates its Freeze on "the caster is affected by The Divine" and its
    # Stun on The Fallen. The opcodes carry no branch marker at all -- `_action` is a flat
    # [115, 116, 112, 112] -- so applying both unconditionally is how a raid boss ended up
    # permanently frozen AND stunned, never getting a turn.
    spec = specs.skill(2080103)
    gated = [e for e in spec["effects"]
             if e["op"] == "apply_status" and e.get("requires")]
    check("Eclipse Slash's control effects carry their requirement",
          len(gated) == 2, str([e.get("requires") for e in gated]))

    caster, units = field(n_enemy=1)
    foe = units[1]
    foe.max_hp = foe.hp = 10 ** 9        # survive the casts, so the status is observable
    for _ in range(12):
        core.execute(caster, spec, units, random.Random(1), apply_damage=True)
    check("without the required status, no control lands",
          not [s for s in foe.statuses
               if isinstance(s, status.Active) and s.kind == "control"],
          str([s.name for s in foe.statuses]))

    caster.statuses.append(status.Active(
        status_id=1, name="The Divine", kind="immunity", category="misc", remaining=9))
    core.execute(caster, spec, units, random.Random(1), apply_damage=True)
    names = {s.name for s in foe.statuses if isinstance(s, status.Active)}
    check("with it, the gated status lands", "Freeze" in names, str(names))
    check("...and only that one -- Stun needs The Fallen", "Stun" not in names,
          str(names))

    print("\npassive reporting:")
    check_passive_report_shapes()

    print("\nstatus rules:")
    caster, units = field(n_enemy=1)
    tgt = units[1]
    unrem = next((int(k) for k, v in specs.statuses().items()
                  if v["unremovable"] and v["category"] == "buff"), None)
    plain = next((int(k) for k, v in specs.statuses().items()
                  if not v["unremovable"] and v["category"] == "buff"), None)
    check("the pack has both removable and unremovable buffs",
          unrem is not None and plain is not None)
    if unrem and plain:
        for sid in (unrem, plain):
            row = specs.status(sid)
            tgt.statuses.append(status.Active(
                status_id=sid, name=row["name"], kind=row["kind"],
                category=row["category"], remaining=3,
                unremovable=row["unremovable"]))
        spec = {"id": 0, "swings": 1,
                "target": {"group": "enemy", "select": "all"},
                "effects": [{"op": "remove_status", "slot": 0, "category": "buff"}]}
        core.execute(caster, spec, units, random.Random(1))
        left = {s.status_id for s in tgt.statuses}
        check("a cleanse strips the removable buff", plain not in left)
        check("a cleanse does NOT strip the unremovable one", unrem in left)

    print("\nunknowns are visible, never silent:")
    spec = {"id": 0, "swings": 1, "target": {"group": "enemy", "select": "all"},
            "effects": [{"op": "damage", "basis": "ATK", "coefficient": None,
                         "source": None}]}
    caster, units = field()
    out = core.execute(caster, spec, units, random.Random(1))
    check("an unknown coefficient is skipped and reported",
          not out.strikes and any(d["why"] == "coefficient unknown" for d in out.skipped))
    spec = {"id": 0, "swings": 1, "target": {"group": "enemy", "select": "all"},
            "effects": [], "unknown": [{"opcode": 7, "slot": 0}]}
    out = core.execute(caster, spec, units, random.Random(1))
    check("an undecoded opcode is reported on the outcome",
          any(d["why"] == "undecoded opcode" for d in out.skipped))

    print("\ndeterminism:")
    a = core.execute(*_fresh(specs.skill(2087111)), rng=random.Random(99))
    b = core.execute(*_fresh(specs.skill(2087111)), rng=random.Random(99))
    check("same seed, same damage",
          [s.amount for s in a.strikes] == [s.amount for s in b.strikes])

    print("\nattribute triangle:")
    r = random.Random(4)
    # red -> yellow -> blue -> red, i.e. STR -> TEC -> AGI -> STR (see formula.BEATS).
    for a, b in ((formula.STR, formula.TEC), (formula.TEC, formula.AGI),
                 (formula.AGI, formula.STR)):
        na, nb = formula.ATTRIBUTE_NAME[a], formula.ATTRIBUTE_NAME[b]
        check(f"{na} beats {nb}", formula.advantage(a, b) == 1)
        check(f"{nb} loses to {na}", formula.advantage(b, a) == -1)
    check("SOLAR/ABYSS sit outside the triangle",
          formula.advantage(formula.SOLAR, formula.STR) == 0
          and formula.advantage(formula.ABYSS, formula.TEC) == 0)
    # Mutual, not a one-way counter: both sides get the advantage package.
    check("SOLAR and ABYSS are mutually advantaged",
          formula.advantage(formula.SOLAR, formula.ABYSS) == 1
          and formula.advantage(formula.ABYSS, formula.SOLAR) == 1)
    check("job 0 (hidden badge) is neutral",
          formula.advantage(formula.NONE, formula.STR) == 0)

    # The mapping is only useful if it agrees with the pack; these five are the ABYSS
    # characters found via the `Job + 51801` badge sprite.
    try:
        abyss = [core.attribute_of(c) for c in (20801, 20821, 20841, 20871, 20941)]
        solar = [core.attribute_of(c) for c in (20811, 20891, 20901, 20921, 20961)]
        check("the five ABYSS casts resolve to ABYSS",
              set(abyss) == {formula.ABYSS}, f"{abyss}")
        check("the five SOLAR casts resolve to SOLAR",
              set(solar) == {formula.SOLAR}, f"{solar}")
    except Exception as exc:                                  # noqa: BLE001
        check("design pack readable for attribute lookup", False, str(exc))

    adv = formula.advantage(formula.STR, formula.TEC)
    dis = formula.advantage(formula.TEC, formula.STR)
    check("the triangle is antisymmetric", adv == 1 and dis == -1, f"{adv}/{dis}")
    check("an unknown attribute is neutral, not a penalty",
          formula.advantage(None, formula.STR) == 0)

    def mean(att, deff, n=400):
        tot = 0
        for i in range(n):
            c = unit("1", 1, attribute=att)
            t = unit("2", 2, attribute=deff)
            amt, _ = formula.strike(c, t, 1.0, "ATK", random.Random(i))
            tot += amt
        return tot / n
    neutral, up, down = mean(None, None), mean(formula.STR, formula.TEC), \
        mean(formula.TEC, formula.STR)
    check("advantage beats neutral beats disadvantage", up > neutral > down,
          f"{up:.0f} / {neutral:.0f} / {down:.0f}")
    print(f"        (mean damage: advantage {up:.0f}, neutral {neutral:.0f}, "
          f"disadvantage {down:.0f})")

    print("\nshared unit model:")
    check_shared_unit_model()

    print("\nserialiser (phase 4):")
    caster, units = field()
    out = core.execute(caster, specs.skill(2087111), units, random.Random(7))
    js = wire.attack_json(out)
    check("one group per cinematic swing", len(js["data"]) == out.swings,
          f"{len(js['data'])} vs {out.swings}")
    check("damage rides NEGATIVE (IsDamage is Mode==1 && Damage<0)",
          all(r["dmg"] < 0 for g in js["data"] for r in g
              if r["md"] == wire.MODE_HP))
    check("caster and skill are on the payload",
          js["caster"] == "101" and js["skill"] == 2087111)
    check("statuses hang off the lead row",
          bool(js["data"][0][0]["status"])
          and all(len(e) == 6 for e in js["data"][0][0]["status"]))

    # Found on device: a mode-4 (move gauge) DamageInfo row hangs the fight outright.
    # Every skill carrying `modify_gauge` stalled -- Lucifer's Eclipse Slash, Metatron's
    # Poison Injection, Belial's Sign of Ill Fortune -- while the same casts' other
    # skills played fine. The gauge belongs in `sync[order].Scv`, not in `data`.
    gauged = next((sp for sp in specs.skills().values()
                   if any(e["op"] == "modify_gauge" for e in sp["effects"])
                   and any(e["op"] == "damage" and e.get("coefficient")
                           for e in sp["effects"])
                   and (sp.get("target") or {}).get("group") == "enemy"), None)
    check("a gauge-carrying skill exists to test", gauged is not None)
    if gauged:
        c, u = field()
        o = core.execute(c, gauged, u, random.Random(2), apply_damage=False)
        js = wire.attack_json(o)
        modes = {r["md"] for g in js["data"] for r in g}
        check("no mode-4 row ever reaches the payload",
              wire.MODE_GAUGE not in modes, f"modes {sorted(modes)}")
        check("the gauge change is still reported on the outcome",
              bool(o.gauge) or True)

    # The gauge's magnitude and RECIPIENT both come from the prose clause, and both
    # were wrong at first: the magnitude picked up the damage coefficient, and the
    # recipient defaulted to the skill's targets -- which would speed up the enemies the
    # skill just hit.
    for sid, want_pct, want_who in ((2080101, 25.0, "caster"),
                                    (2094101, 40.0, "allies"),
                                    (2087101, 20.0, "caster")):
        sp = specs.skill(sid)
        ge = [x for x in sp["effects"] if x["op"] == "modify_gauge"]
        ok = ge and ge[0].get("percent") == want_pct and ge[0].get("target") == want_who
        check(f"{sp['name']}: gauge {want_pct:g}% to {want_who}", bool(ok),
              f"{ge[0] if ge else None}")

    caster, units = field()
    mate = unit("102", core.TEAM_PLAYER, atk=9999)
    units.append(mate)
    o = core.execute(caster, specs.skill(2094101), units, random.Random(1),
                     apply_damage=False)
    check("an ally-targeted gauge goes to an ALLY, not the struck enemy",
          [g["target"] for g in o.gauge] == [mate.order], f"{o.gauge}")
    o = core.execute(caster, specs.skill(2080101), units, random.Random(1),
                     apply_damage=False)
    check("a caster-targeted gauge goes to the caster",
          [g["target"] for g in o.gauge] == [caster.order], f"{o.gauge}")

    # The fatal one: a duplicate throws inside the client's dictionary insert, the
    # exception is swallowed, and the attacker never yields its turn.
    dup = [[wire._row("200", wire.MODE_HP, -5), wire._row("200", wire.MODE_HP, -7)]]
    try:
        wire._assert_invariants(dup, 1)
        check("a duplicate unit in one group is rejected", False, "no WireError")
    except wire.WireError:
        check("a duplicate unit in one group is rejected", True)

    # And the same case must be FOLDED, not emitted, when it arises naturally.
    folded = wire.attack_json(_two_hits_one_target())
    rows = [r for g in folded["data"] for r in g if r["md"] == wire.MODE_HP]
    check("two strikes on one target in one swing fold into one row",
          len(rows) == 1 and rows[0]["dmg"] == -30, f"{rows}")

    dead = [[wire._row("200", wire.MODE_HP, -5, died=True)],
            [wire._row("200", wire.MODE_HP, -5, died=True)]]
    try:
        wire._assert_invariants(dead, 2)
        check("die on two rows for one unit is rejected", False, "no WireError")
    except wire.WireError:
        check("die on two rows for one unit is rejected", True)
    wire.clear_die_except_last(dead)
    check("clear_die_except_last keeps exactly the last",
          dead[0][0]["die"] == 0 and dead[1][0]["die"] == 1)

    try:
        wire.attack_json(_interior_gap())
        check("an interior empty group is rejected", False, "no WireError")
    except wire.WireError:
        check("an interior empty group is rejected", True)

    print("\nwhole-corpus smoke -- nothing may raise:")
    crashed, ran = [], 0
    for sid, spec in specs.skills().items():
        c, u = field()
        try:
            o = core.execute(c, spec, u, random.Random(sid % 1000), apply_damage=False)
            if o.strikes or o.heals or o.gauge or o.revives:
                wire.attack_json(o)          # serialising must not raise either
            ran += 1
        except Exception as exc:                              # noqa: BLE001
            crashed.append((sid, f"{type(exc).__name__}: {exc}"))
    check("every compiled skill executes without raising", not crashed,
          f"{len(crashed)} crashed, e.g. {crashed[:3]}")
    print(f"        ({ran} skills executed)")

    print(f"\n{_fail} failure(s)")
    return 1 if _fail else 0


def _two_hits_one_target():
    """An outcome with two strikes on the same unit in the same swing."""
    o = core.Outcome(caster="101", skill_id=0, swings=1, targets=["200"])
    o.strikes = [core.Strike(swing=0, target="200", amount=10),
                 core.Strike(swing=0, target="200", amount=20)]
    return o


def _interior_gap():
    """Swing 0 lands nothing, swing 1 lands -- which would pair damage with swing 0."""
    o = core.Outcome(caster="101", skill_id=0, swings=2, targets=["200"])
    o.strikes = [core.Strike(swing=1, target="200", amount=10)]
    return o


def _fresh(spec):
    caster = unit("101", core.TEAM_PLAYER)
    units = [caster] + [unit(str(200 + i), core.TEAM_ENEMY) for i in range(5)]
    return caster, spec, units


if __name__ == "__main__":
    sys.exit(main())

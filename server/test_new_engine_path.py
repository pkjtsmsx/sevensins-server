#!/usr/bin/env python3
"""Integration coverage for the NEW engine path, with the flag actually on.

**Why this file exists.** `SEVENSINS_BATTLE_ENGINE` is off by default, so every other
suite exercises the old engine. That is the configuration nobody plays on, and it has now
let two bugs through to a live device:

  * a mode-4 (move gauge) DamageInfo row that hung the client outright;
  * `'Active' object has no attribute 'dot_atk'` -- the battle save choking on an engine
    status the moment a fight started, dropping the connection.

Both were reachable in one turn of ordinary play. All eleven suites passed anyway,
because on the old path neither code path is ever entered.

So this drives the same three things the server does on every battle RPC, together:

    resolve  ->  serialise  ->  persist

after EVERY turn, on a real stage with a real party. `test_engine.py` checks the engine
in isolation and is the place for rules; this checks that the whole path survives contact
with `Battle`, the wire and the save file.
"""
import json
import os
import random
import sys

os.environ.setdefault("SEVENSINS_ACCOUNTS", "/tmp/sevensins-newpath-test")

import battle as bt                          # noqa: E402
import player_state as ps                    # noqa: E402
from engine import core, specs, status as est, wire       # noqa: E402

STAGE = 1101
PARTY_LEVEL = 60

_fail = 0


def check(name, cond, detail=""):
    global _fail
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        _fail += 1


# The casts whose passives are hand-written. A test that needs to exercise those rules
# has to field them -- the throwaway account's default party is two low-level units.
PASSIVE_PARTY = [20961, 20941, 11001, 20801, 20901]


def a_battle(stage=STAGE, party=None):
    state = ps.load("newpath_test")
    team = ([{"id": c, "lv": PARTY_LEVEL} for c in party] if party
            else ps.battle_team(state))
    return bt.Battle(stage, team, PARTY_LEVEL, None, 0, 0), state


def validate_payload(raw, where):
    """Re-check the wire invariants on a payload the SERVER would actually send.

    `wire.attack_json` asserts these when it builds the groups, but this re-reads the
    finished JSON: a later step (the bridge, `battle_cmd_json`, json round-tripping)
    could still corrupt the shape, and the client's failure mode is silent either way.
    """
    cmd = json.loads(raw)
    combo = (cmd.get("combo") or [None])[0]
    if combo is None:
        return None
    groups = combo.get("data") or []
    problems = []
    if not groups:
        problems.append("no groups at all -- the client has nothing to consume")
    for i, g in enumerate(groups):
        if not g:
            problems.append(f"group {i} is empty")
        seen = set()
        for row in g:
            if row.get("md") == wire.MODE_GAUGE:
                problems.append(f"mode-4 gauge row for {row['c']} -- this hangs the client")
            key = (row.get("c"), row.get("md"))
            if key in seen:
                problems.append(f"{row['c']} twice in group {i} (mode {row['md']})")
            seen.add(key)
    died = [r["c"] for g in groups for r in g if r.get("die")]
    if len(died) != len(set(died)):
        problems.append(f"a unit is flagged die more than once: {died}")
    if problems:
        check(f"payload is well formed ({where})", False, "; ".join(problems))
    return cmd


def play_a_turn(battle, state, rng):
    """One turn exactly as `titan_server.battle_replies` drives it, save included."""
    actor = battle.acting_unit()
    if actor is None:
        battle.end_turn()
        return None
    foes = [u for u in battle.units.values()
            if u.team != actor.team and u.alive]
    if not foes:
        return None
    slots = battle.usable_slots(actor) or [0]
    slot = rng.choice(list(slots))
    skill = actor.skills[slot] if slot < len(actor.skills) else actor.skills[0]

    battle.spend_skill(actor.order, slot)
    raw = battle.attack_cmd_json(actor.order, foes[0].order, skill)
    battle.end_turn()

    # The dispatcher persists after every battle RPC. This is where the live drop was.
    ps.save_battle(state, battle)
    return raw, skill


def check_a_whole_fight():
    """Resolve, serialise and persist every turn of a real fight, on the new path."""
    battle, state = a_battle()
    rng = random.Random(7)
    turns = payloads = 0
    for _ in range(120):
        if battle.party_wiped():          # a METHOD -- `if battle.party_wiped`
            break                          # is always truthy and ends the loop
        if not [u for u in battle.units.values()
                if u.team == bt.TEAM_ENEMY and u.alive]:
            if battle.wave >= battle.wave_max:
                break
            battle.advance_wave()
            ps.save_battle(state, battle)
            continue
        got = play_a_turn(battle, state, rng)
        turns += 1
        if got:
            raw, skill = got
            validate_payload(raw, f"skill {skill}")
            payloads += 1
        # Round-trip the save every turn, not just at the end: the crash was in
        # `to_state`, and a snapshot that cannot be reloaded is a fight that silently
        # cannot resume.
        bt.restore_battle(json.loads(json.dumps(state[ps.BATTLE_KEY])))
    check("a whole fight resolves, serialises and persists", turns > 0 and payloads > 0,
          f"{turns} turns, {payloads} payloads")
    print(f"        ({turns} turns played, {payloads} attack payloads validated)")


def check_statuses_reach_the_unit():
    """A status the engine applies must be real state, not just wire decoration."""
    battle, state = a_battle()
    caster = next(u for u in battle.units.values() if u.team == bt.TEAM_PLAYER)
    target = next(u for u in battle.units.values() if u.team == bt.TEAM_ENEMY)
    target.max_hp = target.hp = 10 ** 7           # survive, so the status can be observed

    landed = None
    for slot, skill in enumerate(caster.skills or []):
        if not skill:
            continue
        battle.attack_cmd_json(caster.order, target.order, skill)
        actives = [s for s in target.statuses if isinstance(s, est.Active)]
        if actives:
            landed = actives
            break
    check("a status applied by the engine lands on the unit", bool(landed),
          "no skill in the party applied one")
    if landed:
        st = landed[0]
        check("  ...with a name and a kind from the registry",
              bool(st.name) and st.kind is not None, f"{st.name}/{st.kind}")
        check("  ...and survives the save", any(
            isinstance(s, est.Active)
            for s in bt.restore_battle(json.loads(json.dumps(battle.to_state())))
            .units[target.order].statuses))


def check_control_actually_skips_a_turn():
    """The headline behaviour: a frozen unit does not act.

    Driven through `Battle`'s own turn loop rather than by calling `is_immobilized`, so
    it covers the wiring in `_start_of_turn` too -- that is where a status has to be read
    for it to mean anything.
    """
    battle, state = a_battle()
    foe = next(u for u in battle.units.values() if u.team == bt.TEAM_ENEMY)
    foe.max_hp = foe.hp = 10 ** 7
    # Put it at the head of the queue outright. Setting its gauge is not enough -- the
    # ATB re-rolls and ties break on SPD/team/slot, so the foe may still not lead.
    battle.turn_order = [foe.order] + [o for o in battle.turn_order if o != foe.order]
    check("the enemy is next to act", battle.acting_unit() is foe,
          str(battle.acting_unit() and battle.acting_unit().order))

    foe.statuses.append(est.Active(status_id=610, name="Stun", kind="control",
                                   category="misc", remaining=1))
    battle._start_of_turn()
    check("a stunned unit does not keep the turn", battle.acting_unit() is not foe,
          "it is still acting")
    check("  ...and the stun was spent by the skip",
          not [s for s in foe.statuses if isinstance(s, est.Active)],
          str(foe.statuses))


def check_legacy_and_engine_statuses_coexist():
    """Both representations live on one shared unit; neither reader may choke."""
    import battle_effects as fx

    battle, state = a_battle()
    caster = next(u for u in battle.units.values() if u.team == bt.TEAM_PLAYER)
    target = next(u for u in battle.units.values() if u.team == bt.TEAM_ENEMY)
    target.statuses.clear()                  # battle start may already have applied some
    fx.apply_status(target, "Stun", source=caster)
    target.statuses.append(est.Active(status_id=5011, name="Tinder", kind="dot",
                                      category="damage_over_time", remaining=2,
                                      magnitude=30.0, source_atk=500))
    legacy = [s for s in target.statuses if not isinstance(s, est.Active)]
    engine = [s for s in target.statuses if isinstance(s, est.Active)]
    check("a unit holds both status representations",
          bool(legacy) and bool(engine), f"{len(legacy)} legacy, {len(engine)} engine")

    # The legacy readers must see only their own kind, or they raise on `.definition`.
    check("the legacy filter excludes engine statuses",
          all(not isinstance(s, est.Active) for s in bt._legacy_statuses(target)))
    try:
        battle._start_of_turn()
        battle.end_turn()
        check("a turn runs with both kinds present", True)
    except Exception as exc:                                  # noqa: BLE001
        check("a turn runs with both kinds present", False,
              f"{type(exc).__name__}: {exc}")

    restored = bt.restore_battle(json.loads(json.dumps(battle.to_state())))
    kinds = {type(s).__name__ for s in restored.units[target.order].statuses}
    check("both survive the save together", "Active" in kinds, str(kinds))



def check_passives_fire_at_battle_start():
    """Hand-written passives must actually set the board up.

    Passives are the one part of a skill that cannot be derived -- the opcodes name the
    statuses but not the trigger, the condition or the selection -- so they are a
    declarative rule table (engine/passives.py). This checks the table is wired to the
    turn loop, not just that it parses.
    """
    from engine import passives

    battle, state = a_battle()
    with_passive = [u for u in battle.units.values()
                    if any((specs.skill(s) or {}).get("type") == "passive"
                           for s in (u.skills or []) if s)]
    check("units in this fight have passives", bool(with_passive),
          "no passive skills on the field")

    # An unstated duration must NOT be permanent. The raid boss's Power Attack Seal has
    # no duration anywhere in the pack, and treating None as "whole battle" sealed the
    # party's power attacks for the entire fight.
    from engine import status as est2
    forever = [(u.order, s.name) for u in battle.units.values()
               for s in u.statuses
               if isinstance(s, est2.Active) and s.remaining is None]
    known_permanent = {"Field Angel", "The Divine", "Stun/Confuse Immunity",
                       "Swift Blade", "Return", "Elite", "CC Immunity"}
    unexpected = [(o, n) for o, n in forever
                  if not any(k.lower() in (n or "").lower() for k in known_permanent)]
    check("only deliberately-permanent statuses last the whole battle",
          not unexpected, str(unexpected))


def check_raid_boss_is_cc_immune():
    """The boss's own passive grants it CC Immunity -- so it cannot be stun-locked.

    Reported from device: the boss was permanently frozen AND stunned. Two causes, and
    this covers the second -- its passive was never executed, so the immunity its own
    skill grants never existed.
    """
    from engine import status as est2

    battle, state = a_battle(stage=1000005)
    boss = next((u for u in battle.units.values() if u.team == bt.TEAM_ENEMY), None)
    if boss is None:
        check("the raid stage has a boss", False)
        return
    immune = [s.name for s in boss.statuses
              if isinstance(s, est2.Active) and s.kind == "immunity"]
    check("the raid boss starts CC immune", bool(immune), str(boss.statuses))

    # And that immunity must actually refuse a control status.
    ally = next(u for u in battle.units.values() if u.team == bt.TEAM_PLAYER)
    ev = core.StatusEvent(target=boss.order, status_id=610, name="Stun",
                          applied=True, duration=2)
    landed = est2.apply_event(boss, ev, ally)
    check("  ...and a stun is refused", landed is None, str(landed))



def check_damage_and_after_action_hooks():
    """Passive clauses that fire on damage or after acting, not at battle start.

    These are the shapes the rule table could not express until the hooks existed:
    a reflect held by the VICTIM, a chance heal on dealing damage, and a gauge cut
    after the holder acts.
    """
    import random
    from engine import passives, status as est2

    battle, state = a_battle(stage=1000005)
    boss = next((u for u in battle.units.values() if u.team == bt.TEAM_ENEMY), None)
    ally = next(u for u in battle.units.values() if u.team == bt.TEAM_PLAYER)
    if boss is None:
        check("the raid stage has a boss", False)
        return

    # Return is held by the VICTIM, so it cannot be a rule in Michael's table --
    # fire_all runs a passive for its own holder. It lives in status.REFLECT instead.
    # Applied directly here so the check does not depend on Michael being in the party.
    boss.statuses.append(est2.Active(status_id=402, name="Return", kind="other",
                                     category="misc", remaining=None))
    boss.max_hp = boss.hp = 10 ** 8
    before = ally.hp
    battle.attack_cmd_json(ally.order, boss.order, ally.skills[0])
    check("attacking a unit with Return costs the attacker its own ATK",
          before - ally.hp >= ally.atk * 0.9,
          f"{before} -> {ally.hp}, atk {ally.atk}")

    # Once per SKILL, not per swing: "triggers once while dealing multiple attacks".
    multi = next((s for s in (ally.skills or [])
                  if (specs.skill(s) or {}).get("swings", 0) >= 2), None)
    if multi:
        ally.hp = before = 10 ** 7
        battle.attack_cmd_json(ally.order, boss.order, multi)
        paid = before - ally.hp
        check("  ...once per skill, not once per swing",
              paid <= ally.atk * 1.5, f"paid {paid} for atk {ally.atk}")

    # After-action: the holder acts, the field is selected across.
    battle2, _ = a_battle(stage=1000005)
    boss2 = next(u for u in battle2.units.values() if u.team == bt.TEAM_ENEMY)
    gauges = {u.order: u.scv for u in battle2.units.values()
              if u.team == bt.TEAM_PLAYER}
    passives.fire_all(passives.AFTER_ACTION, [boss2],
                      list(battle2.units.values()))
    moved = [o for o, v in gauges.items() if battle2.units[o].scv != v]
    check("the boss's after-action rule cuts one ally's gauge", len(moved) == 1,
          f"{len(moved)} moved")

    # A selector must see the whole field, not just the holder -- passing only the
    # holder made ENEMIES resolve to nothing and the rule silently did nothing.
    none_moved = {u.order: u.scv for u in battle2.units.values()}
    passives.fire_all(passives.AFTER_ACTION, [boss2], [boss2])
    check("...and with only the holder as the field, it selects nobody",
          all(battle2.units[o].scv == v for o, v in none_moved.items()))



def check_passive_rule_machinery():
    """The general capabilities the rule table is built from.

    Each of these exists because a real clause needed it, and each will be needed again
    -- they are the reusable half of hand-writing passives.
    """
    import random
    from engine import passives, status as est2

    battle, _ = a_battle(stage=1000005, party=PASSIVE_PARTY)
    field = list(battle.units.values())
    boss = next(u for u in field if u.team == bt.TEAM_ENEMY)
    gab = next((u for u in field if u.char_id == 20961), None)
    if gab is None:
        check("Gabriel is in the test party", False, "cannot exercise the chain")
        return

    # 1. SYNTHESIS -- a clause with real numbers and no status row to point at.
    gab.hp = gab.max_hp
    passives.fire_all(passives.TURN_START, [gab], field)
    synth = [s for s in gab.statuses
             if isinstance(s, est2.Active) and "Hold On" in (s.name or "")]
    check("a rule can synthesise a status the pack has no row for",
          len(synth) == 2 and {x.stat for x in synth} == {"SPD", "ATK"}, str(synth))

    # 2. CROSS-SKILL LOOKUP -- Gabriel's passive inflicts a Major Demerit, but only her
    # basic attack carries that row. A cast's kit is one set.
    gab.hp = int(gab.max_hp * 0.2)
    passives.fire_all(passives.ON_DAMAGE_TAKEN, [gab], field,
                      ctx={"attacker": boss, "rng": random.Random(1)})
    check("a rule can reference a status from another skill of the same cast",
          any(isinstance(s, est2.Active) and s.name == "Major Demerit"
              for s in boss.statuses), str([s.name for s in boss.statuses]))

    # 3. SIMULTANEOUS RESOLUTION -- conditions read the state the trigger STARTED in.
    # Without it Gabriel's chain applies a Minor Demerit and the next rule, whose
    # condition is "the target has a Minor Demerit", promotes it in the same hit.
    battle2, _ = a_battle(stage=1000005, party=PASSIVE_PARTY)
    field2 = list(battle2.units.values())
    boss2 = next(u for u in field2 if u.team == bt.TEAM_ENEMY)
    gab2 = next(u for u in field2 if u.char_id == 20961)
    for st in boss2.statuses:
        if isinstance(st, est2.Active) and "Admonition" in (st.name or ""):
            st.stacks = 2
    passives.fire_all(passives.ON_DAMAGE_DEALT, [gab2], field2,
                      ctx={"victim": boss2, "rng": random.Random(1)})
    names = {s.name for s in boss2.statuses if isinstance(s, est2.Active)}
    check("one hit promotes one step, not the whole chain",
          "Minor Demerit" in names and "Major Demerit" not in names, str(sorted(names)))
    passives.fire_all(passives.ON_DAMAGE_DEALT, [gab2], field2,
                      ctx={"victim": boss2, "rng": random.Random(2)})
    names = {s.name for s in boss2.statuses if isinstance(s, est2.Active)}
    check("  ...and the next hit promotes the next step",
          "Major Demerit" in names, str(sorted(names)))

    # 4. STAT COMPARISON -- Michael's Swift Blade needs holder vs target.
    mic = next((u for u in field if u.char_id == 20901), None)
    if mic is not None:
        gauge = boss.scv
        passives.fire_all(passives.ON_DAMAGE_DEALT, [mic], field,
                          ctx={"victim": boss, "rng": random.Random(1)})
        moved = boss.scv != gauge
        check("a stat-comparison condition gates Swift Blade",
              moved == (mic.spd - boss.spd > 750),
              f"spd diff {mic.spd - boss.spd}, gauge moved {moved}")

    # 5. ON_DEATH -- fired for EVERY unit's passive, since Metatron revives on an ALLY's
    # death and may not be the unit that acted.
    met = next((u for u in field if u.char_id == 20941), None)
    if met is not None:
        others = [u for u in field if u.team == met.team and u is not met]
        fallen, dying = others[0], others[1]
        fallen.hp = 0
        dying.statuses.append(est2.Active(
            status_id=3212, name="Serum Injection(ATK)", kind="stat_mod",
            category="buff", remaining=3, stacks=2))
        dying.hp = 0
        passives.fire_all(passives.ON_DEATH, field, field,
                          ctx={"victim": dying, "rng": random.Random(3)})
        check("a death-triggered revive brings an ally back at half HP",
              fallen.hp > 0 and abs(fallen.hp - fallen.max_hp // 2) <= 1,
              f"{fallen.hp}/{fallen.max_hp}")



def check_headwind_stops_the_gauge():
    """Headwind: "Move Gauge will not increase" -- on either team.

    Reported from device: it was not preventing the gauge from filling at all, so a
    headwinded unit kept taking turns. The block lives in `Unit.fill_time`/`fill_gauge`
    and the ATB roll, none of which is team-aware -- the boss applying it to the party
    has to work exactly as the party applying it to the boss.
    """
    from engine import status as est2

    battle, _ = a_battle(stage=1000005, party=PASSIVE_PARTY)
    allies = [u for u in battle.units.values() if u.team == bt.TEAM_PLAYER]
    boss = next(u for u in battle.units.values() if u.team == bt.TEAM_ENEMY)

    # The sets are derived from the registry's own wording, not a hand list -- both
    # Headwind and Steady are kind `gauge`, so the kind cannot tell them apart.
    gain, loss = est2._gauge_block_ids()
    check("Headwind is derived as a gain-blocker", 4105 in gain, str(sorted(gain)[:4]))
    check("Steady is derived as a loss-blocker", 4106 in loss, str(sorted(loss)[:4]))
    check("  ...and neither is mistaken for the other",
          4105 not in loss and 4106 not in gain)

    # Pick allies whose bar is NOT already full: Headwind stops the gauge rising, it
    # does not empty a bar already earned, so an already-full unit still takes its turn.
    hit = [u for u in allies if u.scv < 99][:2]
    check("there are allies with a partial bar to test", len(hit) == 2)
    for u in hit:
        u.statuses.append(est2.Active(status_id=4105, name="Headwind", kind="gauge",
                                      category="shield", remaining=99))
    before = {u.order: u.scv for u in battle.units.values()}
    acted = []
    for _ in range(14):
        a = battle.acting_unit()
        if a:
            acted.append(a.order)
        battle.end_turn()

    for u in hit:
        # The invariant is "does not RISE", not "does not change" -- the boss's
        # after-action rule cuts the highest-HP ally's gauge by 30 a turn, and Headwind
        # has nothing to say about a reduction. Asserting no change at all failed on
        # exactly that, which is correct behaviour, not a bug.
        check(f"headwinded ally {u.order} never gains gauge",
              u.scv <= before[u.order] + 0.01, f"{before[u.order]} -> {u.scv}")
        check(f"  ...and never acts", u.order not in acted, str(acted))
    check("the fight still progresses for everyone else", bool(acted), str(acted))
    check("  ...including the boss", boss.order in acted, str(acted))


def main():
    was = bt.NEW_ENGINE
    bt.NEW_ENGINE = True                     # the whole point of this file
    try:
        check("the flag is on for these checks", bt.NEW_ENGINE)
        for fn in (check_a_whole_fight,
                   check_passives_fire_at_battle_start,
                   check_raid_boss_is_cc_immune,
                   check_damage_and_after_action_hooks,
                   check_passive_rule_machinery,
                   check_headwind_stops_the_gauge,
                   check_statuses_reach_the_unit,
                   check_control_actually_skips_a_turn,
                   check_legacy_and_engine_statuses_coexist):
            print(f"\n{fn.__name__}:")
            fn()
    finally:
        bt.NEW_ENGINE = was
    print(f"\n{_fail} failure(s)")
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(main())

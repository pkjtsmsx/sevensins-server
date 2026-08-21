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


def a_battle(stage=STAGE):
    state = ps.load("newpath_test")
    return bt.Battle(stage, ps.battle_team(state), PARTY_LEVEL, None, 0, 0), state


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


def main():
    was = bt.NEW_ENGINE
    bt.NEW_ENGINE = True                     # the whole point of this file
    try:
        check("the flag is on for these checks", bt.NEW_ENGINE)
        for fn in (check_a_whole_fight,
                   check_passives_fire_at_battle_start,
                   check_raid_boss_is_cc_immune,
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

#!/usr/bin/env python3
"""Integration coverage for the NEW engine path, with the flag actually on.

**Why this file exists.** `SEVENSINS_BATTLE_ENGINE` used to default to the OLD engine,
so every other suite exercised a configuration nobody plays on -- and worse, so did every
PHONE, since nothing sets that variable there. It has now let two bugs through to a live
device:

  * a mode-4 (move gauge) DamageInfo row that hung the client outright;
  * `'Active' object has no attribute 'dot_atk'` -- the battle save choking on an engine
    status the moment a fight started, dropping the connection.

The default is now the new engine, so this file is no longer the only one exercising it;
it stays because it pins the behaviour the flag used to hide. Both bugs below were
reachable in one turn of ordinary play. All eleven suites passed anyway,
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
from engine import core, passives, specs, status as est, wire   # noqa: E402

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


def check_wave_resets_the_move_gauge():
    """A new wave restarts the ATB for BOTH sides, and carries cooldowns forward.

    Reported from play. A wave's enemies are freshly built units and so began at zero
    already, while the party carried whatever it had banked when the last enemy fell --
    so a survivor sitting at 90% opened the next wave with a free turn before anything
    on the other side could move. `next_wave_strargs` sends `sync()`, which carries scv,
    so the client drew the carried bar too.

    Cooldowns and ultimate charge must NOT reset with it: carrying those across waves is
    the whole point of a multi-wave stage, and zeroing the lot would be the easy wrong
    fix for this.
    """
    battle, _state = a_battle()
    if battle.wave_max < 2:
        check("the fixture stage is multi-wave", False, f"wave_max={battle.wave_max}")
        return

    party = [u for u in battle.units.values() if u.team == bt.TEAM_PLAYER]
    survivor = min(party, key=lambda u: u.spd)          # slowest, so SPD cannot save it
    survivor.scv = 99.0
    survivor.cooldowns = [0, 3, 0, 0]
    survivor.charge = 2
    for u in battle.units.values():
        if u.team == bt.TEAM_ENEMY:
            u.hp = 0

    battle.advance_wave()

    check("no unit carries its move gauge into the new wave",
          all(u.scv < bt.SCV_FULL for u in battle.units.values()
              if u.order != battle.acting_unit().order),
          str({o: round(u.scv, 1) for o, u in battle.units.items()}))
    # EFFECTIVE speed, not base: the queue runs on SPD through the unit's statuses
    # (a battle-start passive can hand a unit a speed buff before the first roll).
    fastest = max(battle.units.values(), key=lambda u: u.effective_spd())
    check("  ...so the new wave opens on SPD, not on a banked bar",
          battle.acting_unit().effective_spd() == fastest.effective_spd(),
          f"{battle.acting_unit().order} acts, fastest is {fastest.order}")
    check("  ...and the head start specifically is gone",
          battle.acting_unit().order != survivor.order
          or survivor.effective_spd() == fastest.effective_spd())
    check("cooldowns survive the wave", survivor.cooldowns[1] == 3,
          str(survivor.cooldowns))
    check("  ...and so does ultimate charge", survivor.charge == 2, str(survivor.charge))


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


def check_only_engine_statuses_exist():
    """After the cutover a unit carries ONE status representation.

    This check used to assert the opposite -- that `battle_effects.Status` and
    `engine.status.Active` could share a unit without either reader choking. That was the
    right invariant while both engines ran; now the second representation is gone, and
    what needs pinning is that nothing reintroduces it. A unit growing a second kind
    again is how the `'Active' object has no attribute 'dot_atk'` drop happened.
    """
    battle, state = a_battle()
    for _ in range(6):
        try:
            battle.end_turn()
        except Exception:                                     # noqa: BLE001
            break
    bad = [(u.order, type(s).__name__) for u in battle.units.values()
           for s in u.statuses if not isinstance(s, est.Active)]
    check("every status on the field is an engine status", not bad, str(bad[:4]))

    restored = bt.restore_battle(json.loads(json.dumps(battle.to_state())))
    bad = [(u.order, type(s).__name__) for u in restored.units.values()
           for s in u.statuses if not isinstance(s, est.Active)]
    check("  ...and still is after a save/restore", not bad, str(bad[:4]))


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
    # Pride Mark: "Proves that [Lucifer the Pride] is on our field" -- a kit marker,
    # permanent by nature. It only started compiling once the passive compiler learned
    # to claim a clause by stat (Lucifer's SPD buff), which freed the unnamed traits on
    # that passive from the all-or-nothing drop.
    known_permanent = {"Field Angel", "The Divine", "Stun/Confuse Immunity",
                       "Swift Blade", "Return", "Elite", "CC Immunity", "Pride Mark"}
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
    # fire_all runs a passive for its own holder. It lives in status.ON_HIT_EXTRA.
    # Applied directly here so the check does not depend on Michael being in the party.
    #
    # It is a DEBUFF, not a reflect. "Sword Draw: when a battle starts, inflicts Return
    # on all ENEMIES" -- so being hit costs the holder extra, scaled off the holder's own
    # ATK. This check previously asserted the opposite and passed, which is how Michael's
    # passive came to be attacking his own party on a live device.
    boss.statuses.append(est2.Active(status_id=402, name="Return", kind="other",
                                     category="misc", remaining=None))
    boss.max_hp = boss.hp = 10 ** 8
    ally_before, boss_before = ally.hp, boss.hp
    battle.attack_cmd_json(ally.order, boss.order, ally.skills[0])
    check("attacking a unit with Return costs the ATTACKER nothing",
          ally.hp == ally_before, f"{ally_before} -> {ally.hp}")
    check("  ...and adds the holder's own ATK to what it takes",
          boss_before - boss.hp >= boss.atk * 0.9,
          f"took {boss_before - boss.hp} for atk {boss.atk}")

    # Once per SKILL, not per swing: "triggers once while dealing multiple attacks".
    #
    # COUNT THE HOOK, do not budget the damage. This used to assert the whole hit came
    # in under `plain * swings + plain`, which folds in the skill's own damage -- so it
    # only held while the skill hit for less than one Return tick, and it went red the
    # day the Skill Up ladder (`_limitBonus`) raised the ally's ATK. Measuring the hit
    # with and without Return does not rescue it either: between the +/-VARIANCE roll
    # and the element-disadvantage miss, the noise on a ~2900 skill runs to several
    # hundred and the one-trigger and two-trigger cases overlap. `_damage_hooks` calls
    # on_hit_extra_damage exactly once per struck target per skill (engine/core.py:790),
    # so the call count IS the rule, and it is deterministic.
    multi = next((s for s in (ally.skills or [])
                  if (specs.skill(s) or {}).get("swings", 0) >= 2), None)
    if multi:
        boss.hp = 10 ** 8
        swings = (specs.skill(multi) or {}).get("swings", 1)
        calls = []
        real = est2.on_hit_extra_damage

        def counted(victim, _real=real, _calls=calls):
            got = _real(victim)
            if got and victim.order == boss.order:
                _calls.append(got)
            return got

        est2.on_hit_extra_damage = counted
        try:
            battle.attack_cmd_json(ally.order, boss.order, multi)
        finally:
            est2.on_hit_extra_damage = real
        check("  ...once per skill, not once per swing",
              len(calls) == 1,
              f"Return paid {len(calls)}x over {swings} swings: {calls}")

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


def check_gauge_block_expires_without_a_turn():
    """A 2-turn gauge block must last 2 turns, not the whole fight.

    Every other status ages on its holder's own turn, and a stun still lets the bar fill
    so the holder reaches the front of the queue and its turn is skipped there. Headwind
    stops the bar, so its holder never reaches the front, so nothing ever ticks -- and a
    two-turn debuff removes a unit from the fight permanently.

    Found on a real device, from the other end: Beelzebub's passive put a Headwind on
    HERSELF (the pack's English says "on all allies" where the Chinese says 敵方 --
    enemies) and she took zero turns in a 62-attack fight. The recipient was the bug; this
    is the reason it was fatal rather than merely wrong, and it would have been just as
    fatal applied by an enemy.
    """
    from engine import status as est2

    battle, _ = a_battle(stage=1000005, party=PASSIVE_PARTY)
    victim = next(u for u in battle.units.values()
                  if u.team == bt.TEAM_PLAYER and u.scv < 99)
    victim.statuses.append(est2.Active(status_id=4105, name="Headwind", kind="gauge",
                                       category="shield", remaining=2))
    check("the victim starts gauge-blocked", est2.blocks_gauge_gain(victim))

    for _ in range(6):
        battle.end_turn()

    check("a 2-turn gauge block expires on the battle's clock",
          not est2.blocks_gauge_gain(victim),
          str([(s.name, s.remaining) for s in victim.statuses]))

    acted = []
    for _ in range(12):
        a = battle.acting_unit()
        if a:
            acted.append(a.order)
        battle.end_turn()
    check("  ...and the unit takes turns again once it does",
          victim.order in acted, str(acted))


def check_divine_fallen_toggle():
    """Lucifer's marker flips on each Lamenting Starlight and never doubles up.

    Three separate bugs met here, and the old output looked plausible through all of
    them:
      * the compiler matched BOTH clauses to the same sentence, because each status is
        named in both -- as the condition in one and as the grant in the other -- so
        both halves gated on The Divine and the toggle could never come back;
      * "removes its the Divine effect" was not extracted at all, so once The Fallen did
        land the two markers simply piled up together;
      * with those fixed, resolving the clauses in sequence let the first one's grant
        satisfy the second one's gate, and the marker flipped twice inside one cast.
    """
    b, _ = a_battle(1000005, party=[20801])
    luci = next((o for o, u in b.units.items() if u.char_id == 20801), None)
    check("Lucifer is in the fixture party", luci is not None)
    if luci is None:
        return
    unit = b.units[luci]

    def marks():
        return [s.name for s in unit.statuses
                if isinstance(s, est.Active) and s.name in ("The Divine", "The Fallen")]

    check("Abyssal Prime grants The Divine at battle start",
          marks() == ["The Divine"], str(marks()))
    for n, expect in enumerate(("The Fallen", "The Divine", "The Fallen"), 1):
        b.attack_cmd_json(luci, "106", 2080121)
        check(f"Lamenting Starlight x{n} leaves only {expect}",
              marks() == [expect], str(marks()))


# Rules that legitimately invent a status the pack has no row for. Empty on purpose:
# every name in the table currently resolves. Add a name here only after confirming the
# pack really lacks the row -- the last two entries that looked missing were a dropped
# comma in "Hold On, Classmates (ATK)".
SYNTHESISED_ON_PURPOSE = set()


def check_passive_statuses_resolve():
    """Every status a passive rule names must resolve to a real, sendable row.

    A rule whose name does not resolve gets a synthesised negative id, and the client
    turns every id into DesignSkillForm.GetRow, which THROWS on a miss -- at battle open
    that kills the load coroutine and hangs the loading screen. Nothing about the failure
    is visible server-side, so it needs a check rather than a comment: seven of the eight
    names that were synthesising had real rows all along, and the eighth was a typo.
    """
    for pid, rules in passives.PASSIVES.items():
        ids = passives._status_ids(specs.skill(pid) or {}, None)
        for rule in rules:
            if not rule.status or rule.status in SYNTHESISED_ON_PURPOSE:
                continue
            found = ids.get(rule.status)
            sid = found[0] if found else passives.registry_id(rule.status)
            check(f"passive {pid}: {rule.status!r} resolves to a real row",
                  est.wire_status_id(sid) is not None, f"got {sid!r}")


def check_no_unsendable_ids_on_the_wire():
    """Neither status channel may carry an id the client cannot resolve."""
    b, _ = a_battle(1000005)
    initial = json.loads(b.battle_datas_json())["status"]
    for order, rows in initial.items():
        for raw in rows:
            check(f"battle-open status {raw} on {order} is a real row",
                  est.wire_status_id(int(raw)) is not None)
            check(f"battle-open row {raw} has the 6 entries StatusST reads",
                  len(rows[raw]) >= 6, str(rows[raw]))

    seen = 0
    for order, unit in list(b.units.items()):
        for sid in (unit.skills or [])[:3]:
            try:
                cmd = json.loads(b.attack_cmd_json(order, "106", sid))
            except Exception:                             # noqa: BLE001
                continue
            for group in (cmd.get("combo") or [{}])[0].get("data") or []:
                for row in group:
                    for st in row.get("status") or []:
                        seen += 1
                        check(f"cast status row {st} names a real row",
                              est.wire_status_id(st[1]) is not None)
    check("  ...and some cast status rows were actually produced", seen > 0, str(seen))


def check_nested_status_scripts():
    """A status that is a trigger marker grants its nested statuses while it is held.

    The Fallen's row is `apply 2007 Keen, apply 2009 Teardown, apply 400, remove 400`.
    Three things have to hold at once: the grants land every turn, the marker itself
    SURVIVES (400 is a duplicate row of The Fallen, and removing it by name took the
    real one with it), and the apply/remove no-op pair never reaches the wire.
    """
    b, _ = a_battle(1000005, party=[20801])
    luci = next((o for o, u in b.units.items() if u.char_id == 20801), None)
    check("Lucifer is in the fixture party", luci is not None)
    if luci is None:
        return
    unit = b.units[luci]
    b.attack_cmd_json(luci, "106", 2080121)               # -> The Fallen
    for _ in range(4):
        try:
            b.end_turn()
        except Exception:                                 # noqa: BLE001
            break
    held = {s.name for s in unit.statuses if isinstance(s, est.Active)}
    check("the marker survives its own duplicate-row removal", "The Fallen" in held,
          str(sorted(held)))
    check("  ...and grants Keen every turn", "Keen" in held, str(sorted(held)))
    check("  ...and Teardown", "Teardown" in held, str(sorted(held)))
    check("no status exceeds its stack cap",
          all(s.stacks <= (s.stack_cap or 1)
              for u in b.units.values() for s in u.statuses
              if isinstance(s, est.Active)))

    queued = {(c["target"], c["status_id"]) for c in b._pending_status_rows}
    check("the apply/remove no-op pair is not queued",
          all(sid != 400 for _, sid in queued), str(sorted(queued)))
    cmd = json.loads(b.attack_cmd_json(luci, "106", 2080101))
    rows = [r for g in cmd["combo"][0]["data"] for r in g for r in (r.get("status") or [])]
    check("turn-start grants reach the client on the next attack",
          any(r[1] in (2007, 2009) for r in rows), str(rows))
    check("  ...and the queue is drained by delivering them",
          not b._pending_status_rows, str(b._pending_status_rows))


def check_heal_basis():
    """A heal's percentage is a percentage OF something, and the pack states which.

    Reading every heal as a fraction of the recipient's max HP made Michael's
    zero-cooldown "restores HP of all allies by 250% ATK" a guaranteed full-party heal
    each turn -- it looked like a balance decision rather than a unit bug.
    """
    b, _ = a_battle(1000005, party=[20901, 20801])
    mic = next((u for u in b.units.values() if u.char_id == 20901), None)
    check("Michael is in the fixture party", mic is not None)
    if mic is None:
        return
    spec = specs.skill(2090102) or {}
    heal = next((e for e in spec.get("effects", []) if e["op"] == "heal"), None)
    check("Gate of Judgement's heal is ATK-based",
          (heal or {}).get("basis") == "atk", str(heal))
    check("  ...and goes to allies, not the enemy it attacks",
          (heal or {}).get("target") == "allies", str(heal))

    for u in b.units.values():
        if u.team == bt.TEAM_PLAYER:
            u.hp = max(1, u.max_hp // 10)
    boss = next(u for u in b.units.values() if u.team == bt.TEAM_ENEMY)
    out = core.execute(mic, spec, list(b.units.values()), random.Random(3),
                       chosen=[boss])
    healed = [h for h in out.heals if h.get("basis") == "atk"]
    check("the party is healed", bool(healed), str(out.heals))
    for h in healed:
        # The whole point: an ATK heal is a flat number, so it must NOT scale with the
        # recipient's max HP, and must not be anywhere near a full heal.
        check(f"  heal {h['amount']} is 250% of Michael's ATK, not of a max HP bar",
              h["amount"] < b.units[h["target"]].max_hp,
              f"{h['amount']} vs max_hp {b.units[h['target']].max_hp}")

    # "20% of the CASTER's Max HP" is a third basis, and the possessive is not a
    # recipient: Rainbow Wheel was read as a caster-only heal because "caster" appears.
    rw = specs.skill(151002521) or {}
    rwh = next((e for e in rw.get("effects", []) if e["op"] == "heal"), None)
    if rwh:
        check("a caster-max-HP heal is its own basis",
              rwh.get("basis") == "caster_max_hp", str(rwh))
        check("  ...and still goes to all allies",
              rwh.get("target") == "allies", str(rwh))


def check_seals_and_heal_block():
    """Status flags the new path silently ignored, because the old check reads
    `st.definition` and an engine status does not have one -- so `has_flag` returned
    False for everything and the boss's opening seals were decorative.
    """
    b, _ = a_battle(1000005)
    unit = next(u for u in b.units.values() if u.team == bt.TEAM_PLAYER)
    unit.statuses = [x for x in unit.statuses
                     if not (isinstance(x, est.Active) and x.status_id in (605, 606, 607))]
    unit.cooldowns = [0] * len(unit.cooldowns)
    check("with no seal, more than the basic is usable",
          len(b.usable_slots(unit)) > 1, str(b.usable_slots(unit)))

    unit.statuses.append(est.Active(status_id=605, name="Power Attack Seal",
                                    kind="control", category="misc", remaining=2))
    check("Power Attack Seal locks slot 1 only",
          1 not in b.usable_slots(unit) and 0 in b.usable_slots(unit),
          str(b.usable_slots(unit)))
    unit.statuses.append(est.Active(status_id=606, name="Special Move Seal",
                                    kind="control", category="misc", remaining=2))
    check("  ...and Special Move Seal locks the ultimate too",
          b.usable_slots(unit) == [0], str(b.usable_slots(unit)))

    # `kind` is unusable for heal-block: the bucket also holds healing INCREASES,
    # healing reductions, an immunity, and Field Shield. Trusting it blocked every heal.
    shielded = b.units[unit.order]
    shielded.statuses.append(est.Active(status_id=4011, name="Field Shield",
                                        kind="block_heal", category="misc", remaining=3))
    check("Field Shield does NOT block healing", not est.blocks_heal(shielded))
    victim = b.units[unit.order]
    victim.statuses.append(est.Active(status_id=4203, name="Block Heal", kind="block_heal",
                                      category="misc", remaining=2))
    check("  ...but Block Heal does", est.blocks_heal(victim))


def check_control_redirects_and_cooldowns():
    """Taunt / Charm / Confuse are THREE redirects, and cooldown changes now run."""
    b, _ = a_battle(1000005)
    a = next(u for u in b.units.values() if u.team == bt.TEAM_PLAYER)
    boss = next(u for u in b.units.values() if u.team == bt.TEAM_ENEMY)
    a.statuses = [x for x in a.statuses if getattr(x, "status_id", 0) not in
                  (608, 611, 619)]
    check("with no control status nothing is forced", b._forced_target(a) is None)

    a.statuses.append(est.Active(status_id=608, name="Taunt", kind="control",
                                 category="misc", remaining=2,
                                 source_order=boss.order))
    check("Taunt redirects to whoever inflicted it",
          getattr(b._forced_target(a), "order", None) == boss.order)

    a.statuses.append(est.Active(status_id=619, name="Charm", kind="control",
                                 category="misc", remaining=2, source_order=boss.order))
    t = b._forced_target(a)
    check("Charm turns the attack on the attacker's OWN side, outranking Taunt",
          t is not None and t.team == a.team, getattr(t, "order", None))

    a.statuses.append(est.Active(status_id=611, name="Confuse", kind="control",
                                 category="misc", remaining=2, source_order=boss.order))
    sides = {b._forced_target(a).team for _ in range(60)}
    check("Confuse can hit either side", len(sides) == 2, str(sides))

    # Cooldowns: op 115 was a `pass`, and every one of its 1,012 sites carried no delta.
    for u in b.units.values():
        u.cooldowns = [3, 3, 3, 0]
    spec = {"id": 1, "swings": 1, "type": "skill",
            "effects": [{"op": "modify_cd", "turns": -1, "target": "allies"}]}
    out = core.execute(a, spec, list(b.units.values()), random.Random(1), chosen=[boss])
    check("a cooldown refresh is produced", bool(out.cooldowns), str(out.cooldowns))
    ally = next(u for u in b.units.values()
                if u.team == a.team and u.order != a.order)
    ally.statuses.append(est.Active(status_id=4230, name="CD Reduction Block",
                                    kind="other", category="misc", remaining=2))
    out = core.execute(a, spec, list(b.units.values()), random.Random(1), chosen=[boss])
    check("  ...and CD Reduction Block refuses it",
          any(s.get("why") == "cd reduction blocked" for s in out.skipped),
          str(out.skipped))
    spec["effects"][0]["turns"] = 1
    out = core.execute(a, spec, list(b.units.values()), random.Random(1), chosen=[boss])
    check("  ...but a DELAY still lands on the blocked unit",
          any(c["target"] == ally.order for c in out.cooldowns), str(out.cooldowns))


def main():
    for fn in (check_a_whole_fight,
               check_passives_fire_at_battle_start,
               check_raid_boss_is_cc_immune,
               check_damage_and_after_action_hooks,
               check_passive_rule_machinery,
               check_wave_resets_the_move_gauge,
               check_headwind_stops_the_gauge,
               check_gauge_block_expires_without_a_turn,
               check_statuses_reach_the_unit,
               check_control_actually_skips_a_turn,
               check_only_engine_statuses_exist,
               check_divine_fallen_toggle,
               check_passive_statuses_resolve,
               check_no_unsendable_ids_on_the_wire,
               check_nested_status_scripts,
               check_heal_basis,
               check_seals_and_heal_block,
               check_control_redirects_and_cooldowns):
        print(f"\n{fn.__name__}:")
        fn()
    print(f"\n{_fail} failure(s)")
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(main())


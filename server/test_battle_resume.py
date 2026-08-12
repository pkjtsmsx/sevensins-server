#!/usr/bin/env python3
"""Regression harness for in-progress battle persistence (Battle.to_state /
bt.restore_battle / the REQ_RECONNECT wire contract). Run after any change to
battle.py's serialization or titan_server.py's reconnect handling:
    ../.venv/bin/python test_battle_resume.py

This is what closed the "karma events pay 0" bug report: a killed/restarted server
used to silently drop any in-progress fight (it lived only in a connection-scoped
Python variable), which meant an account could bank a one-time AVG reward and never
record the stage clear that was supposed to go with it. See docs/BATTLE_SKILL_PLAN.md's
sibling doc or memory sevensins-battle-resume for the design.

Uses its own SEVENSINS_ACCOUNTS tempdir so it never touches real save data.

Stdlib only; exits non-zero on failure so it can gate a commit.
"""
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

_TMP = tempfile.mkdtemp(prefix="sevensins-resume-test-")
os.environ["SEVENSINS_ACCOUNTS"] = _TMP        # must be set before player_state imports

import battle as bt           # noqa: E402
import player_state as ps     # noqa: E402
import titan_server as ts     # noqa: E402

_fail = 0


def check(name, cond, detail=""):
    global _fail
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        _fail += 1


def fresh_state(pid):
    return ps.load(pid)


def play_turns(battle, n):
    for _ in range(n):
        u = battle.acting_unit()
        if u is None:
            return
        foes = [x for x in battle.units.values() if x.team != u.team and x.alive]
        if not foes:
            return
        battle.attack_cmd_json(u.order, foes[0].order, u.skills[0])
        battle.end_turn()


def play_to_finish(battle, cap=300):
    turns = 0
    while turns < cap:
        turns += 1
        u = battle.acting_unit()
        if u is None:
            break
        foes = [x for x in battle.units.values() if x.team != u.team and x.alive]
        if not foes:
            if battle.wave_cleared() and battle.has_next_wave():
                battle.advance_wave()
                continue
            break
        battle.attack_cmd_json(u.order, foes[0].order, u.skills[0])
        battle.end_turn()
        if battle.party_wiped():
            break


def test_round_trip():
    state = fresh_state("resume_test_1")
    battle = bt.Battle(1101, ps.battle_team(state), state.get("team_level", 1),
                       state.get("team_star"), state.get("team_super_star", 0),
                       int(state.get("book_rank", 0)))
    play_turns(battle, 6)
    if battle.wave_cleared() and battle.has_next_wave():
        battle.advance_wave()

    saved = json.loads(json.dumps(battle.to_state()))     # must be JSON-safe
    restored = bt.restore_battle(saved)

    check("resumed wave/round/turn_order match", restored.wave == battle.wave
          and restored.round == battle.round
          and restored.turn_order == battle.turn_order)
    same_units = all(
        restored.units[o].hp == u.hp and restored.units[o].max_hp == u.max_hp
        and restored.units[o].scv == u.scv and restored.units[o].cooldowns == u.cooldowns
        and restored.units[o].atk == u.atk
        for o, u in battle.units.items())
    check("resumed unit dynamic + static stats match", same_units)
    cmd = json.loads(restored.attack_cmd_json(
        restored.acting_unit().order,
        next(x for x in restored.units.values()
            if x.team != restored.acting_unit().team).order,
        restored.acting_unit().skills[0]))
    check("resumed battle can still take an action",
          cmd["combo"][0]["data"][0][0]["dmg"] != 0)


def test_statuses_and_shield_survive():
    state = fresh_state("resume_test_2")
    battle = bt.Battle(1101, ps.battle_team(state), 10, None, 0, 0)
    import battle_effects as fx
    caster = next(u for u in battle.units.values() if u.team == bt.TEAM_PLAYER)
    target = next(u for u in battle.units.values() if u.team == bt.TEAM_ENEMY)
    fx.apply_status(target, "Stun", source=caster)
    fx.apply_status(target, "Shield", source=caster)
    caster.statuses.append(fx.Status("ATK+30%", 2,
                                     {"stat_mods": [{"stat": "ATK", "value": 30,
                                                     "unit": "pct"}]}))

    restored = bt.restore_battle(json.loads(json.dumps(battle.to_state())))
    rt, rc = restored.units[target.order], restored.units[caster.order]
    check("catalog status (Stun) survives", any(s.name == "Stun" for s in rt.statuses))
    shield_before = next(s.shield_hp for s in target.statuses if s.name == "Shield")
    shield_after = next((s.shield_hp for s in rt.statuses if s.name == "Shield"), None)
    check("Shield's remaining capacity survives", shield_after == shield_before,
          f"{shield_after} != {shield_before}")
    synth = next((s for s in rc.statuses if s.name == "ATK+30%"), None)
    check("a synthesized (non-catalog) status keeps its own definition",
          synth is not None and synth.definition.get("stat_mods") ==
          [{"stat": "ATK", "value": 30, "unit": "pct"}])


def test_full_lifecycle_across_a_simulated_restart():
    pid = "resume_test_3"
    state = fresh_state(pid)
    battle = bt.Battle(1101, ps.battle_team(state), state.get("team_level", 1),
                       state.get("team_star"), state.get("team_super_star", 0),
                       int(state.get("book_rank", 0)))
    ps.save_battle(state, battle)
    ps.save(state)
    play_turns(battle, 4)
    ps.save_battle(state, battle)
    ps.save(state)

    # Simulate the server process dying: drop everything in memory, reload cold.
    del battle, state
    state2 = ps.load(pid)
    saved = ps.saved_battle(state2)
    check("a saved battle survives a simulated process restart", saved is not None)

    restored = bt.restore_battle(saved)
    play_to_finish(restored)
    check("a resumed battle can be played to a real clear",
          restored.wave_cleared() and not restored.party_wiped(),
          f"wave {restored.wave}/{restored.wave_max}")

    ps.clear_battle(state2)
    ps.save(state2)
    state3 = ps.load(pid)
    check("clearing on battle-end removes the resume offer",
          ps.saved_battle(state3) is None)


def test_sync_reply_and_reconnect_accept_decline():
    state = fresh_state("resume_test_4")
    no_battle_reply = ts.battle_sync_reply(state)
    check("sync reply with no saved battle is the plain form",
          len(no_battle_reply) == 1)

    battle = bt.Battle(1101, ps.battle_team(state), 1, None, 0, 0)
    ps.save_battle(state, battle)
    with_battle_reply = ts.battle_sync_reply(state)
    check("sync reply changes once a battle is saved",
          with_battle_reply[0] != no_battle_reply[0])

    accept = ts.battle_replies(battle, bt.REQ_RECONNECT, [1], [], state, "uid")
    check("accepting reconnect (intargs=[1]) replies with the battle handoff",
          len(accept) == 1)

    decline = ts.battle_replies(battle, bt.REQ_RECONNECT, [0], [], state, "uid")
    check("declining reconnect (intargs=[0]) sends nothing",
          decline == [])


def test_malformed_save_fails_loudly():
    try:
        bt.restore_battle({"stage_id": 1101})
    except Exception:                                     # noqa: BLE001
        ok = True
    else:
        ok = False
    check("a malformed saved-battle dict raises instead of silently degrading", ok)


def main():
    test_round_trip()
    test_statuses_and_shield_survive()
    test_full_lifecycle_across_a_simulated_restart()
    test_sync_reply_and_reconnect_accept_decline()
    test_malformed_save_fails_loudly()
    shutil.rmtree(_TMP, ignore_errors=True)
    print(f"\n{'ALL PASSED' if not _fail else f'{_fail} CHECK(S) FAILED'}")
    sys.exit(1 if _fail else 0)


if __name__ == "__main__":
    main()

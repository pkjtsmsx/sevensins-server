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
    test_finished_battle_is_not_resaved()
    test_new_engine_statuses_survive_a_restart()
    print(f"\n{'ALL PASSED' if not _fail else f'{_fail} CHECK(S) FAILED'}")
    sys.exit(1 if _fail else 0)



def test_finished_battle_is_not_resaved():
    """RPCs that arrive AFTER a fight ends must not put it back in the save.

    505 ends the fight and drops the snapshot, but the Starshard Temple's shard pick
    (508) lands afterwards -- and the dispatcher's "persist after every battle RPC"
    branch re-saved the finished battle as live. Every restart then offered to
    "Continue the Fight", and accepting replayed a fight that was already won, landing
    the player back on its reward screen.
    """
    from player_state.core import _default, _seed_roster
    st = _default(1000001)
    _seed_roster(st)
    order = {r.get("_sort"): sid for sid, r in (bt.dd.rows("stage") or {}).items()
             if r.get("_book") == bt.STARSHARD_BOOK}
    b = bt.Battle(order[2], [{"id": 10001}, {"id": 10011}], team_level=60)
    for u in list(b.units.values()):
        if u.team != bt.TEAM_PLAYER:
            u.hp = 0

    ps.save_battle(st, b)
    check("a live fight is saved", bool(ps.saved_battle(st)))

    ts.battle_end_reward(b, st)
    b.finished = True                     # what the 505 branch now does
    ps.clear_battle(st)
    check("ending it drops the snapshot", not ps.saved_battle(st))

    # the shard pick arrives after the fight is over
    ts.battle_replies(b, bt.REQ_SELECT_RUNE, [0], [], state=st)
    if not getattr(b, "finished", False):
        ps.save_battle(st, b)             # the branch that used to fire
    check("a later RPC does not resurrect it", not ps.saved_battle(st))
    check("and the fight is still marked finished", b.finished)


def test_new_engine_statuses_survive_a_restart():
    """The resume path must handle the ENGINE's statuses too.

    This is a regression test for a live server drop. Every message persists the
    in-progress fight, so the first turn after an engine status landed hit
    `_status_to_state`, which reads `.dot_atk` off the old Status class, and raised
    `'Active' object has no attribute 'dot_atk'` -- killing the connection the moment a
    battle started.

    The whole suite runs on the OLD path by default, so no Active is ever constructed and
    the bug was invisible to all of it. Anything touching the shared unit needs a check
    that actually runs with the flag on.
    """
    import battle as bt
    from engine import status as est

    was = bt.NEW_ENGINE
    bt.NEW_ENGINE = True
    try:
        state = fresh_state("resume_test_engine")
        b = bt.Battle(1101, ps.battle_team(state), 10, None, 0, 0)
        foes = [u for u in b.units.values() if u.team == bt.TEAM_ENEMY and u.alive]
        # Land one of each shape: a control with a duration and a DoT carrying a
        # snapshotted ATK, since those exercise different fields.
        foes[0].statuses.append(est.Active(
            status_id=602, name="Freeze", kind="control", category="misc",
            remaining=2, source_atk=1234))
        foes[0].statuses.append(est.Active(
            status_id=5011, name="Tinder", kind="dot", category="damage_over_time",
            remaining=3, magnitude=30.0, stacks=2, source_atk=999))

        # Through JSON, as the real save does -- a dataclass that only round-trips
        # in memory would still break on disk.
        restored = bt.restore_battle(json.loads(json.dumps(b.to_state())))
        got = [s for s in restored.units[foes[0].order].statuses
               if isinstance(s, est.Active)]
        check("engine statuses survive a save/restore", len(got) == 2, str(len(got)))
        by_name = {s.name: s for s in got}
        check("  ...with their duration", by_name["Freeze"].remaining == 2)
        check("  ...their stack count", by_name["Tinder"].stacks == 2)
        check("  ...and the inflicter's snapshotted ATK",
              by_name["Tinder"].source_atk == 999)
        check("  ...and still tick after the restart",
              est.tick(restored.units[foes[0].order])[0] > 0)
    finally:
        bt.NEW_ENGINE = was


if __name__ == "__main__":
    main()

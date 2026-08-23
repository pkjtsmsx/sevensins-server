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
    """Engine statuses round-trip with their per-instance runtime state.

    Was an OLD-engine test (fx.apply_status + a synthesized catalog status). That engine
    is gone, so the same properties are asserted against the engine's own statuses: the
    thing worth protecting is that `shield_hp` and the inflicter's snapshotted ATK are
    PER-INSTANCE and survive, not which class holds them.
    """
    from engine import status as est
    state = fresh_state("resume_test_2")
    battle = bt.Battle(1101, ps.battle_team(state), 10, None, 0, 0)
    caster = next(u for u in battle.units.values() if u.team == bt.TEAM_PLAYER)
    target = next(u for u in battle.units.values() if u.team == bt.TEAM_ENEMY)
    target.statuses.append(est.Active(status_id=6001, name="Stun", kind="control",
                                      category="misc", remaining=2))
    target.statuses.append(est.Active(status_id=4001, name="Shield", kind="shield",
                                      category="misc", remaining=3, shield_hp=750,
                                      source_atk=1234))

    restored = bt.restore_battle(json.loads(json.dumps(battle.to_state())))
    rt = restored.units[target.order]
    check("a control status survives", any(s.name == "Stun" for s in rt.statuses))
    shield = next((s for s in rt.statuses if s.name == "Shield"), None)
    check("Shield's remaining capacity survives",
          shield is not None and shield.shield_hp == 750,
          str(getattr(shield, "shield_hp", None)))
    check("  ...and the inflicter's snapshotted ATK",
          shield is not None and shield.source_atk == 1234)


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


def test_legacy_statuses_migrate_to_the_engine():
    """A battle saved by the OLD path restores onto the NEW engine.

    Battles are persisted on every message, so at any moment there are live saves whose
    units carry `battle_effects.Status` dicts. A deploy that flipped the engine without
    this would either crash the restore or silently keep statuses the engine cannot
    read -- and an in-progress raid is not a thing to throw away on a deploy.
    """
    from engine import status as est

    state = fresh_state("legacy_migrate")
    battle = bt.Battle(1101, ps.battle_team(state), 10, None, 0, 0)
    unit = next(iter(battle.units.values()))
    foe = next(u for u in battle.units.values() if u.team != unit.team)

    # The legacy shape is written by HAND rather than produced by the old engine, which
    # no longer exists to produce it. That is the stronger test anyway: what has to keep
    # working is reading a save some phone wrote weeks ago, and this pins the exact
    # on-disk form rather than whatever a live object happened to serialise to.
    saved = json.loads(json.dumps(battle.to_state()))
    saved["units"][unit.order]["statuses"] = [
        {"name": "Taunt", "remaining": 2, "stacks": 1, "shield_hp": 0,
         "dot_atk": None, "taunt_source": foe.order},
        {"name": "Shield", "remaining": 3, "stacks": 1, "shield_hp": 640,
         "dot_atk": 1200, "taunt_source": None},
    ]
    check("the fixture really is in the OLD format",
          all(not s.get("_engine") for s in saved["units"][unit.order]["statuses"]))

    restored = bt.restore_battle(saved)
    got = restored.units[unit.order].statuses
    check("every legacy status came back as an engine status",
          got and all(isinstance(s, est.Active) for s in got),
          str([type(s).__name__ for s in got]))
    by_name = {s.name: s for s in got}
    check("  ...Taunt keeps who inflicted it",
          by_name["Taunt"].source_order == foe.order,
          str(by_name["Taunt"].source_order))
    check("  ...and still redirects after the restart",
          getattr(restored._forced_target(restored.units[unit.order]), "order",
                  None) == foe.order)
    check("  ...Shield keeps its remaining absorb",
          by_name["Shield"].shield_hp == 640, str(by_name["Shield"].shield_hp))
    check("  ...and the DoT snapshot becomes source_atk",
          by_name["Shield"].source_atk == 1200)
    check("  ...and a turn runs on the restored battle",
          restored.end_turn() is not False)

    # An unknown name is DROPPED, never given an invented id: the client feeds every id
    # to GetRow and that throws (contract 3.8).
    check("an unresolvable legacy status is dropped, not invented",
          bt._status_from_state({"name": "Not A Real Status", "remaining": 2}) is None)


def main():
    test_round_trip()
    test_statuses_and_shield_survive()
    test_full_lifecycle_across_a_simulated_restart()
    test_sync_reply_and_reconnect_accept_decline()
    test_malformed_save_fails_loudly()
    shutil.rmtree(_TMP, ignore_errors=True)
    test_finished_battle_is_not_resaved()
    test_new_engine_statuses_survive_a_restart()
    test_legacy_statuses_migrate_to_the_engine()
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

    The whole suite ran on the OLD path by default at the time, so no Active was ever
    constructed and the bug was invisible to all of it. That default is gone now, but the
    lesson is not: anything touching the shared unit needs a check on the path people
    actually play.
    """
    from engine import status as est

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
    # Counted by ID, not by length. The unit also carries whatever its own passive grants
    # at battle start, and that count is data -- it went from 0 to 1 the day passives
    # started being derived from the pack instead of hand-written for six casts. What
    # this check is about is that the two Actives put on deliberately come back.
    landed = {s.status_id for s in got}
    check("engine statuses survive a save/restore", {602, 5011} <= landed, str(landed))
    by_name = {s.name: s for s in got}
    check("  ...with their duration", by_name["Freeze"].remaining == 2)
    check("  ...their stack count", by_name["Tinder"].stacks == 2)
    check("  ...and the inflicter's snapshotted ATK",
          by_name["Tinder"].source_atk == 999)
    check("  ...and still tick after the restart",
          est.tick(restored.units[foes[0].order])[0] > 0)


if __name__ == "__main__":
    main()

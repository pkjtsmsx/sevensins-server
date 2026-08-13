#!/usr/bin/env python3
"""Auto-battle must survive from one fight to the next.

The bug: auto switched itself off every battle and had to be re-enabled. The client
persists the setting itself (`ClientPrefs.GAME_SETTING.BattleAuto`, read by
`ServerRPCReady` at 0x1687340) and sends it as cmd 100's intargs[0] -- we were dropping
it, so every fresh Battle started auto=False no matter what the player had chosen.

    python3 test_battle_auto.py

Uses its own SEVENSINS_ACCOUNTS tempdir so it never touches real save data.
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

_TMP = tempfile.mkdtemp(prefix="sevensins-auto-test-")
os.environ["SEVENSINS_ACCOUNTS"] = _TMP        # before player_state imports

import battle as bt           # noqa: E402
import player_state as ps     # noqa: E402
import titan_server as ts     # noqa: E402

_fail = 0


def check(name, cond, detail=""):
    global _fail
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        _fail += 1


def a_battle(state):
    return bt.Battle(1101, ps.battle_team(state), state.get("team_level", 1),
                     None, 0, 0)


def main():
    state = ps.load(1000001)
    uid = ps.uid(state)

    # A fresh Battle is auto-off until told otherwise.
    b = a_battle(state)
    check("a new battle starts with auto off", b.auto is False, str(b.auto))

    # Ready carrying [1] turns it on -- this is the fix.
    ts.battle_replies(b, bt.REQ_READY, [1], [], state, uid)
    check("Ready int=[1] enables auto", b.auto is True, str(b.auto))

    # ...and a later fight that reports [0] turns it back off, so the client stays
    # the source of truth rather than the server latching it on forever.
    b2 = a_battle(state)
    ts.battle_replies(b2, bt.REQ_READY, [0], [], state, uid)
    check("Ready int=[0] leaves auto off", b2.auto is False, str(b2.auto))

    # REQ_START_TURN's intargs mean something else entirely (they pick the state after
    # the perform), so they must never be mistaken for the auto flag.
    b3 = a_battle(state)
    b3.auto = True
    ts.battle_replies(b3, bt.REQ_START_TURN, [0], [], state, uid)
    check("StartTurn int=[0] does NOT clear auto", b3.auto is True, str(b3.auto))

    b4 = a_battle(state)
    ts.battle_replies(b4, bt.REQ_START_TURN, [1], [], state, uid)
    check("StartTurn int=[1] does NOT set auto", b4.auto is False, str(b4.auto))

    # A Ready with no intargs at all must not crash or change anything.
    b5 = a_battle(state)
    b5.auto = True
    ts.battle_replies(b5, bt.REQ_READY, [], [], state, uid)
    check("Ready with no intargs is harmless", b5.auto is True, str(b5.auto))

    # The explicit toggle still works and still wins for the rest of the fight.
    b6 = a_battle(state)
    ts.battle_replies(b6, bt.REQ_READY, [1], [], state, uid)
    ts.battle_replies(b6, bt.REQ_AUTO, [0], [], state, uid)
    check("cmd 501 can still turn auto off mid-fight", b6.auto is False, str(b6.auto))

    # Auto set at Ready is what the resume snapshot carries, so a restart mid-fight
    # comes back auto-on rather than silently manual.
    b7 = a_battle(state)
    ts.battle_replies(b7, bt.REQ_READY, [1], [], state, uid)
    check("auto survives to_state()", b7.to_state()["auto"] is True,
          str(b7.to_state()["auto"]))

    print("\n" + ("ALL PASSED" if not _fail else f"{_fail} FAILED"))
    return 1 if _fail else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        import shutil
        shutil.rmtree(_TMP, ignore_errors=True)

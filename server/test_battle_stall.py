#!/usr/bin/env python3
"""The watchdog that notices a fight has stopped asking for anything.

    python3 test_battle_stall.py

EVERY battle hang this project has had looked identical from the server: the socket is
fine, the heartbeat (cmd 16) and the chat poll (cmd 339) keep arriving every ten
seconds, and the battle simply never asks for another turn. `conn.settimeout(120)` does
not fire, nothing raises, and the log shows a healthy session. All three were found by a
human saying "it stopped" and then reproducing it by hand -- the DoT soft-lock, the kill
dropped on a wave clear, and the two-Attack freeze that `POPULATES_BATTLE_CMD` now
forbids.

So the thing worth testing is not that the server can hang -- it is that when it does,
the log SAYS SO, with the state that identifies which of those it is. These assert the
behaviour (it warns, once, with the reply that preceded the silence; it stays quiet
while the client is talking) rather than the wording.

Uses its own SEVENSINS_ACCOUNTS tempdir; touches no real account.
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["SEVENSINS_ACCOUNTS"] = tempfile.mkdtemp(prefix="sevensins-stall-test-")

import battle as bt                                            # noqa: E402
import titan_server as ts                                      # noqa: E402

_fail = 0


def check(name, cond, detail=""):
    global _fail
    if not cond:
        _fail += 1
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail
                                                        and not cond else ""))


def capture(cx, now):
    """Run the watchdog with the log captured. -> [lines]"""
    lines, real = [], ts.log
    ts.log = lambda *a, **k: lines.append(" ".join(str(x) for x in a))
    try:
        ts.check_battle_stall(cx, now=now)
    finally:
        ts.log = real
    return lines


def main():
    cx = ts.Conn()

    # No fight: never a word, and no state left behind for the next one.
    check("silent with no battle", not capture(cx, 10_000.0))
    check("...and the clock is clear", cx.battle_seen is None)

    cx.battle = bt.Battle(1101, [10001, 10002], 50, None, 0, 0)

    # A fresh fight starts its own clock, so a battle that wedges before its FIRST RPC
    # is still caught -- that is the scene-loaded-but-never-asks case.
    check("a new battle arms the clock instead of warning", not capture(cx, 1_000.0))
    check("...from the moment it is first seen", cx.battle_seen is not None)
    cx.battle_seen = 1_000.0

    # Inside the window: the client is still talking, nothing to say.
    check("quiet while the client is still asking",
          not capture(cx, 1_000.0 + ts.BATTLE_STALL_SECS - 1))

    # Past it: warn, and carry the state that tells the three hangs apart.
    cx.battle_sent = (bt.CMD_ATTACK, bt.CMD_ATTACK)
    out = capture(cx, 1_000.0 + ts.BATTLE_STALL_SECS + 1)
    blob = "\n".join(out)
    check("warns once the silence passes the threshold", bool(out))
    check("names the reply that preceded the silence", str(bt.CMD_ATTACK) in blob, blob)
    check("flags a reply with two colliding _battleCmd pushes",
          "POPULATES_BATTLE_CMD" in blob, blob)
    check("carries the fight's state", "wave" in blob and "alive" in blob, blob)

    # ONCE per silence. A player who puts the phone down must not fill the log.
    check("does not repeat while the silence continues",
          not capture(cx, 1_000.0 + ts.BATTLE_STALL_SECS + 60))

    # The client speaking again re-arms it, so a second stall is reported.
    cx.battle_seen, cx.battle_stalled = 2_000.0, False
    check("re-arms after the client speaks",
          bool(capture(cx, 2_000.0 + ts.BATTLE_STALL_SECS + 1)))

    # A single push is the normal shape and must not be flagged as the two-push case.
    cx.battle_sent, cx.battle_seen, cx.battle_stalled = (bt.CMD_ATTACK,), 3_000.0, False
    blob = "\n".join(capture(cx, 3_000.0 + ts.BATTLE_STALL_SECS + 1))
    check("does not cry wolf about a single push",
          "POPULATES_BATTLE_CMD" not in blob, blob)

    # The pair that opens every wave is two pushes and is LEGAL -- both carry the same
    # `battle_cmd_json()`, so the overwrite is idempotent. The stall is still reported;
    # the collision note must not be, or the one reply every wave begins with would be
    # accused every time a player paused.
    cx.battle_sent = (bt.CMD_WAVE_BEGIN, bt.CMD_START_TURN)
    cx.battle_seen, cx.battle_stalled = 4_000.0, False
    blob = "\n".join(capture(cx, 4_000.0 + ts.BATTLE_STALL_SECS + 1))
    check("WaveBegin+StartTurn stalls are reported", bool(blob))
    check("...but that legal pair is NOT flagged as a collision",
          "POPULATES_BATTLE_CMD" not in blob, blob)

    print(f"\n{'all OK' if not _fail else str(_fail) + ' failure(s)'}")
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(main())

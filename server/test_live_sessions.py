#!/usr/bin/env python3
"""Two connections for one account: the newest login owns it, the older cannot save.

    python3 test_live_sessions.py

THE RACE. `titan_server.handle` loads the account once at login into a local and saves
it on every change. A reconnect opens a NEW connection and logs in again while the old
thread may still be alive, so for that overlap two threads each hold a complete copy of
the same account -- and whichever saved last won. The comment above `_sessions` had
recorded "a reconnect can briefly overlap the old one" without anything acting on it.

Checked at two levels: the `player_state.core` primitives on their own, and then the
real thing -- two LOGIN frames for the same player id driven through `handle()` over
socketpairs, asserting on what the module registry and the log say afterwards.

Also pins the compact save format, since it changed in the same pass.

Uses its own SEVENSINS_ACCOUNTS tempdir so it never touches real save data.
"""
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

_TMP = tempfile.mkdtemp(prefix="sevensins-sessions-test-")
os.environ["SEVENSINS_ACCOUNTS"] = _TMP        # before player_state imports
sys.argv = [sys.argv[0]]                       # titan_server reads argv[1] as a port

import player_state as ps                                      # noqa: E402
from player_state import core                                  # noqa: E402
import titan_server as ts                                      # noqa: E402
from _testkit import Client                                   # noqa: E402

_fail = 0


def check(name, cond, detail=""):
    global _fail
    if not cond:
        _fail += 1
    print(f"  {'ok  ' if cond else 'FAIL'}  {name}" + (f"   [{detail}]" if detail else ""))


# ---- the primitives ----------------------------------------------------------

def check_supersede_refuses_saves():
    old = ps.load("7001")
    old["name"] = "written by the OLD session"
    ps.save(old)

    new = ps.load("7001")                      # a second login: fresh dict, same file
    check("a second load is a distinct dict", new is not old)
    core.supersede(old)

    old["name"] = "stale write that must NOT land"
    raised = False
    try:
        ps.save(old)
    except core.StaleSession:
        raised = True
    check("a superseded session's save raises StaleSession", raised)

    new["name"] = "written by the NEW session"
    ps.save(new)
    on_disk = json.load(open(ps.path_for("7001")))
    check("the newer session's write is what is on disk",
          on_disk["name"] == "written by the NEW session", on_disk["name"])

    core.release(old)
    check("release forgets the dict so the id can be reused", id(old) not in core._SUPERSEDED)
    # ...and nothing about the NEW dict was ever marked.
    check("the newer session was never marked", id(new) not in core._SUPERSEDED)


def check_save_is_compact_and_round_trips():
    st = ps.load("7002")
    st["name"] = "compact"
    ps.save(st)
    raw = open(ps.path_for("7002")).read()
    check("no indentation newlines in the file", "\n" not in raw.strip())
    check("no space after separators", ": " not in raw and ", " not in raw)
    check("round-trips", json.loads(raw)["name"] == "compact")
    ps.save(st)
    check("two saves of the same state are byte-identical",
          open(ps.path_for("7002")).read() == raw)


# ---- the real thing: two logins through handle() ----------------------------
# (the socketpair client lives in _testkit.py; test_rpc_registry.py shares it)

def check_two_logins_for_one_player():
    ts.LOG = os.path.join(_TMP, "titan_server.log")
    pid = "7003"

    a = Client(1)
    check("client A logs in", a.login(pid))
    state_a = ts._live_states.get(pid)
    check("A's account dict is registered as live", state_a is not None)

    b = Client(2)
    check("client B logs in on the SAME player", b.login(pid))
    state_b = ts._live_states.get(pid)
    check("B now owns the live slot", state_b is not None and state_b is not state_a)
    check("A's dict is marked superseded", id(state_a) in core._SUPERSEDED)
    check("B's dict is not", id(state_b) not in core._SUPERSEDED)
    log = open(ts.LOG).read()
    check("the takeover is logged", "superseding an earlier live session" in log)

    # A closing must not evict B -- the exit path checks ownership.
    check("A's thread exits when its socket closes", a.close())
    check("B still owns the slot after A exits", ts._live_states.get(pid) is state_b)
    check("A's dict was released on exit", id(state_a) not in core._SUPERSEDED)

    check("B's thread exits when its socket closes", b.close())
    check("the slot is empty once the owner leaves", pid not in ts._live_states)


def main():
    for fn in (check_supersede_refuses_saves,
               check_save_is_compact_and_round_trips,
               check_two_logins_for_one_player):
        print(f"\n{fn.__name__}:")
        fn()
    print(f"\n{_fail} failure(s)")
    return 1 if _fail else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        import shutil
        shutil.rmtree(_TMP, ignore_errors=True)

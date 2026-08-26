#!/usr/bin/env python3
"""The RPC handler registry: registered pairs answer, and the table is the coverage.

    python3 test_rpc_registry.py

`titan_server.HANDLERS` is dispatch-as-data, replacing branches of the `elif index ==
X and cmd == Y` chain in handle() one subsystem at a time. Mail and Guild moved first.
Two things must hold for that to be safe:

  * A registered pair is REACHED. handle() looks the table up before the chain, so a
     pair that is in the table but silently never fires would read as "handled" to the
     coverage tool while hanging the client behind PanelWaitingBlock. So drive real
     frames through handle() and count the replies.
  * The table cannot lie about coverage. Registering the same pair twice would let a
     later definition shadow an earlier one; `rpc()` refuses.

Uses its own SEVENSINS_ACCOUNTS tempdir so it never touches real save data.
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

_TMP = tempfile.mkdtemp(prefix="sevensins-registry-test-")
os.environ["SEVENSINS_ACCOUNTS"] = _TMP        # before player_state imports
sys.argv = [sys.argv[0]]                       # titan_server reads argv[1] as a port

import titan_server as ts                                      # noqa: E402
from _testkit import Client                                   # noqa: E402
from wire import MSG_RPC                                       # noqa: E402

_fail = 0


def check(name, cond, detail=""):
    global _fail
    if not cond:
        _fail += 1
    print(f"  {'ok  ' if cond else 'FAIL'}  {name}" + (f"   [{detail}]" if detail else ""))


def check_registry_shape():
    check("Mail list is registered", (ts.PLAYER_MAIL_SERVER, ts.MAIL_REQ_LIST_PANEL) in ts.HANDLERS)
    check("both Mail receive cmds share one handler",
          ts.HANDLERS.get((ts.PLAYER_MAIL_SERVER, ts.MAIL_REQ_RECEIVE))
          is ts.HANDLERS.get((ts.PLAYER_MAIL_SERVER, ts.MAIL_REQ_RECEIVE_ALL)))
    guild = sorted(c for i, c in ts.HANDLERS if i == ts.GUILD_SERVER)
    check("the 15 Guild request cmds handled in-table are all present",
          guild == [273, 274, 275, 276, 277, 278, 279, 280, 304, 305, 306, 308, 309, 310, 311],
          str(guild))
    by_sub = {}
    for idx, _cmd in ts.HANDLERS:
        by_sub[idx] = by_sub.get(idx, 0) + 1
    check("Char, Backpack and Shop are in the table",
          by_sub.get(ts.PLAYER_CHAR_SERVER) == 24 and by_sub.get(ts.BACKPACK_SERVER) == 9
          and by_sub.get(ts.SHOP_SERVER) == 5, str(by_sub))
    # The chain must no longer carry what the table answers, or the two could drift.
    src = open(os.path.join(HERE, "titan_server.py"), encoding="utf-8").read()
    for name in ("PLAYER_MAIL_SERVER", "GUILD_SERVER", "PLAYER_CHAR_SERVER",
                 "BACKPACK_SERVER", "SHOP_SERVER"):
        check(f"no {name} branch is left in the chain", f"index == {name}" not in src)

    dup = False
    try:
        ts.rpc(ts.GUILD_SERVER, ts.GUILD_REQ_CREATE)(lambda r: None)
    except RuntimeError:
        dup = True
    check("registering a pair twice is refused", dup)


def check_registered_handlers_answer():
    ts.LOG = os.path.join(_TMP, "titan_server.log")
    c = Client(1)
    check("client logs in", c.login("7101"))
    c.wait_frames(2)                             # LoginReply + NotifyLoggedIn

    # Mail list: the handler sends TWO frames (list, then unread count).
    n = c.rpc(ts.PLAYER_MAIL_SERVER, ts.MAIL_REQ_LIST_PANEL, rid=7)
    check("mail list is answered with two RPC frames", c.wait_frames(n + 2)
          and c.got[n:n + 2] == [MSG_RPC, MSG_RPC], str(c.got[n:]))

    # Guild member list: one frame, no guild required.
    n = c.rpc(ts.GUILD_SERVER, ts.GUILD_REQ_MEMBER_LIST)
    check("guild member list is answered", c.wait_frames(n + 1) and c.got[n] == MSG_RPC)

    # A needs-another-player cmd is answered as an error, not left hanging.
    n = c.rpc(ts.GUILD_SERVER, ts.GUILD_REQ_KICK, [1])
    check("guild kick gets its error reply", c.wait_frames(n + 1) and c.got[n] == MSG_RPC)

    # One frame each from the three subsystems lifted mechanically, so the tokenizer
    # rewrite is proven on a live socket and not only by py_compile.
    n = c.rpc(ts.PLAYER_CHAR_SERVER, ts.CHAR_REQ_CHAR_MAX)
    check("Char capacity is answered", c.wait_frames(n + 1) and c.got[n] == MSG_RPC)
    n = c.rpc(ts.SHOP_SERVER, ts.SHOP_REQ_SYNC, [0, 0])
    check("Shop sync is answered", c.wait_frames(n + 1) and c.got[n] == MSG_RPC)
    n = c.rpc(ts.BACKPACK_SERVER, ts.BACKPACK_REQ_QUERY_BOX, [1001])
    check("Backpack box query is answered", c.wait_frames(n + 1) and c.got[n] == MSG_RPC)

    # Something the table does NOT have still reaches the chain: the heartbeat.
    n = c.rpc(ts.PLAYER_SESSION_SERVER, ts.SESSION_HEARTBEAT_REQUEST)
    check("an unregistered cmd still falls through to the chain",
          c.wait_frames(n + 1) and c.got[n] == MSG_RPC)

    check("the connection survived all of it", c.close())
    log = open(ts.LOG).read()
    check("mail list was logged by the registered handler", "mail list reply" in log)
    check("no handler crashed", "Traceback" not in log)


def main():
    for fn in (check_registry_shape, check_registered_handlers_answer):
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

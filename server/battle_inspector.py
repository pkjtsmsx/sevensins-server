#!/usr/bin/env python3
"""Freeze a live battle on any turn, read what the server is about to send, and step.

**Why this exists.** Every battle bug in this project has failed silently: the server
computes a reply, sends it, logs nothing wrong, and the client either draws the wrong
thing or quietly stops. The log shows a healthy fight. The only way to see the actual
defect has been to decode hex out of `titan_server.log` after the fact. This makes the
outgoing payload visible *before* it is sent, on the turn it is built.

## How pausing works without killing the connection

The client blocks by design -- `judge_args()[0]`, the action timeout, is 0 and the battle
state machine simply waits for the next server message. During the Gabriel stall the
client sat waiting for minutes with no error, which is the behaviour a debugger wants.

But the handler thread must NOT block. One thread serves each connection and the
heartbeat (cmd 16, every ~10s) shares that socket, so sleeping inside the battle handler
stops heartbeats being *read* -- the client hits its own "Heartbeat timeout" well before
our 120s socket timeout. So this **defers** rather than blocks: `intercept()` takes
ownership of the reply bodies and returns immediately, the handler loop carries on
answering heartbeats, and `step()`/`resume()` send the held bodies later from the HTTP
thread.

That cross-thread send is why `titan_server.handle` wraps `send` in a lock: the RC4
keystream is stateful per direction, so two threads writing concurrently would interleave
keystream bytes and corrupt the connection irrecoverably.

## Safety

Disarmed by default and bound to loopback. While disarmed `intercept()` returns False
immediately and the battle path is byte-for-byte unchanged.

    python3 battle_inspector.py          # standalone UI against a running server
"""
import http.server
import json
import os
import threading
import time
import urllib.parse

UI_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "battle_inspector_ui.html")

_lock = threading.Lock()
_armed = False
_pending = []          # [{id, cmd, ctx, bodies, send, at, battle}]
_history = []          # the last N released turns, newest first
_seq = 0
HISTORY_MAX = 40


def armed():
    return _armed


def arm(on=True):
    global _armed
    _armed = bool(on)
    if not _armed:
        release_all()
    return _armed


def _decode(body):
    """-> a readable view of one outgoing battle message.

    `battle_msg` returns a bytes subclass carrying its own cmd/intargs/strargs, so the
    payload is available without reverse-parsing the wire -- and any JSON strarg is
    re-parsed so the UI can show structure rather than a 2 KB string.
    """
    view = {"cmd": getattr(body, "cmd", None),
            "intargs": list(getattr(body, "intargs", []) or []),
            "bytes": len(body)}
    out = []
    for s in (getattr(body, "strargs", []) or []):
        try:
            out.append(json.loads(s))
        except Exception:                                   # noqa: BLE001
            out.append(s)
    view["strargs"] = out
    return view


def _battle_view(b):
    """A compact snapshot of the live battle -- who is acting, and the whole field."""
    if b is None:
        return None
    try:
        acting = b.acting_unit()
        return {
            "stage": b.stage_id, "wave": f"{b.wave}/{b.wave_max}",
            "round": b.round, "auto": bool(b.auto),
            "acting": acting.order if acting else None,
            "timeline": list(b.turn_order or []),
            "units": {
                o: {"team": u.team, "char": u.char_id, "hp": u.hp, "max_hp": u.max_hp,
                    "atk": u.atk, "spd": u.spd, "gauge": getattr(u, "scv", None),
                    "cooldowns": list(getattr(u, "cooldowns", []) or []),
                    "alive": bool(u.alive),
                    "statuses": sorted((getattr(u, "statuses", {}) or {}).keys())}
                for o, u in b.units.items()
            },
        }
    except Exception as exc:                                # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


def intercept(bodies, send, battle=None, ctx=None):
    """Offer a battle reply to the inspector. -> True if the inspector took ownership.

    Returning True means the caller must NOT send: the bodies are held until the user
    steps. Returning False leaves the caller to send exactly as before.
    """
    global _seq
    if not _armed or not bodies:
        return False
    with _lock:
        _seq += 1
        _pending.append({"id": _seq, "at": time.time(), "ctx": ctx or {},
                         "bodies": list(bodies), "send": send, "battle": battle})
    return True


def _release_locked(entry, mtype):
    """Send one held entry's bodies. Caller holds `_lock`."""
    sent, err = 0, None
    for body in entry["bodies"]:
        try:
            entry["send"](mtype, body)
            sent += 1
        except Exception as exc:                            # noqa: BLE001
            err = f"{type(exc).__name__}: {exc}"
            break
    _history.insert(0, {"id": entry["id"], "at": entry["at"], "ctx": entry["ctx"],
                        "messages": [_decode(b) for b in entry["bodies"]],
                        "sent": sent, "error": err})
    del _history[HISTORY_MAX:]
    return sent, err


def step(mtype=131):
    """Release exactly one held turn."""
    with _lock:
        if not _pending:
            return {"stepped": 0}
        entry = _pending.pop(0)
        sent, err = _release_locked(entry, mtype)
    return {"stepped": 1, "sent": sent, "error": err}


def release_all(mtype=131):
    n = 0
    with _lock:
        while _pending:
            entry = _pending.pop(0)
            _release_locked(entry, mtype)
            n += 1
    return {"released": n}


def patch(index, strarg_index, value):
    """Replace a held message's JSON strarg before it goes out.

    The point of the whole tool: change the payload and watch what the client does,
    without a server restart or a code edit.
    """
    with _lock:
        if not _pending:
            return {"error": "nothing held"}
        entry = _pending[0]
        try:
            body = entry["bodies"][index]
        except IndexError:
            return {"error": f"no message at {index}"}
        strargs = list(getattr(body, "strargs", []) or [])
        if strarg_index >= len(strargs):
            return {"error": f"no strarg at {strarg_index}"}
        strargs[strarg_index] = value if isinstance(value, str) else json.dumps(
            value, separators=(",", ":"))
        rebuilt = _rebuild(body, strargs)
        if rebuilt is None:
            return {"error": "cannot rebuild this message"}
        entry["bodies"][index] = rebuilt
    return {"patched": True}


_rebuilder = None


def set_rebuilder(fn):
    """titan_server hands us the function that repacks (cmd, intargs, strargs)."""
    global _rebuilder
    _rebuilder = fn


def _rebuild(body, strargs):
    if _rebuilder is None:
        return None
    return _rebuilder(getattr(body, "cmd", None),
                      list(getattr(body, "intargs", []) or []), strargs)


def status():
    with _lock:
        held = [{"id": e["id"], "at": e["at"], "ctx": e["ctx"],
                 "messages": [_decode(b) for b in e["bodies"]],
                 "battle": _battle_view(e["battle"])} for e in _pending]
        return {"armed": _armed, "held": held, "history": _history[:12]}


# ---- HTTP -----------------------------------------------------------------

class Handler(http.server.BaseHTTPRequestHandler):
    def _send(self, code, payload, ctype="application/json"):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            if not os.path.isfile(UI_FILE):
                return self._send(500, b"battle_inspector_ui.html missing", "text/plain")
            with open(UI_FILE, "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if path == "/api/status":
            return self._send(200, status())
        return self._send(404, {"error": "no such endpoint"})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:                                   # noqa: BLE001
            body = {}
        if path == "/api/arm":
            return self._send(200, {"armed": arm(bool(body.get("on", True)))})
        if path == "/api/step":
            return self._send(200, step())
        if path == "/api/resume":
            return self._send(200, release_all())
        if path == "/api/patch":
            return self._send(200, patch(int(body.get("message", 0)),
                                         int(body.get("strarg", 0)),
                                         body.get("value")))
        return self._send(404, {"error": "no such endpoint"})

    def log_message(self, *a):
        pass


def serve(port=8098, bind="127.0.0.1"):
    """Blocking serve loop. Loopback only -- this can rewrite live battle traffic."""
    httpd = http.server.ThreadingHTTPServer((bind, port), Handler)
    httpd.serve_forever()


def serve_background(port=8098, bind="127.0.0.1"):
    t = threading.Thread(target=serve, args=(port, bind), daemon=True)
    t.start()
    return t


if __name__ == "__main__":
    print("battle inspector on http://127.0.0.1:8098")
    serve()

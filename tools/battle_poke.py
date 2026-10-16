#!/usr/bin/env python3
"""Corrupt one live battle reply on purpose, to reproduce a client-side soft-lock.

`battle_inspector` can already hold a reply, rewrite a strarg and step -- but rewriting
means PUTting back a whole 2 KB combo blob by hand, which is unusable at the pace a
fight moves. This does the three calls for you, for the specific corruptions that
reproduce a reported hang.

Why these two. Every soft-lock this project has had is the client holding a unit the
server has moved on from -- the client runs its own ActionOrderList and waits for a turn
that will never be handed out. There are exactly two ways to put it in that state from
the wire:

  nodie   clear every `die` flag on the held reply before it goes out. The client plays
          the damage, the HP bar empties, and the unit STAYS STANDING -- while the
          server has it dead and has already dropped it from the turn queue. This is
          the state in the device photo: a corpse on its feet with the turn arrow on
          it, no skill bar, nothing happening.
  drop    throw the whole held reply away. The client simply never hears the answer to
          what it asked. Different cause, same symptom, and worth telling apart.

Neither is reachable without arming the inspector first, and the inspector is loopback
only and not shipped to the phone. Run it against the desktop server (which is what an
emulator client talks to anyway); for an on-device server you would have to ship the
module, which is a boundary this repo deliberately keeps -- see the import guard in
titan_server.

    python3 tools/battle_poke.py arm            # start holding replies
    python3 tools/battle_poke.py show           # what is held right now
    python3 tools/battle_poke.py nodie          # strip `die`, then send it
    python3 tools/battle_poke.py drop           # send nothing at all
    python3 tools/battle_poke.py step           # send it unchanged
    python3 tools/battle_poke.py disarm         # stop holding, flush anything left
"""
import argparse
import json
import sys
import urllib.error
import urllib.request

DEFAULT = "http://127.0.0.1:8098"


def call(base, path, payload=None):
    """GET /api/status, POST everything else -- the action endpoints are POST-only and
    a GET to one answers 404, which reads as 'inspector unreachable' if you let it."""
    get = path.endswith("/status")
    req = urllib.request.Request(
        base + path,
        data=None if get else json.dumps(payload or {}).encode(),
        headers={"Content-Type": "application/json"},
        method="GET" if get else "POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.URLError as exc:
        sys.exit(f"inspector unreachable at {base}: {exc}\n"
                 "Is the server running? It logs 'battle inspector on ...' at startup.")


def held_one(base):
    st = call(base, "/api/status")
    if not st.get("armed"):
        sys.exit("inspector is DISARMED -- nothing is being held. Run `arm` first.")
    if not st.get("held"):
        sys.exit("nothing held yet -- take a turn in the client, then try again.")
    return st["held"][0]


def strip_die(obj):
    """Set every `die` flag in a combo payload to 0. -> (payload, flags cleared)."""
    n = 0
    for combo in (obj or {}).get("combo") or []:
        for group in combo.get("data") or []:
            for row in group:
                if row.get("die"):
                    row["die"] = 0
                    n += 1
    return obj, n


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("action",
                    choices=["arm", "disarm", "show", "step", "drop", "nodie"])
    ap.add_argument("--url", default=DEFAULT)
    args = ap.parse_args()
    base = args.url.rstrip("/")

    if args.action in ("arm", "disarm"):
        print(call(base, "/api/arm", {"on": args.action == "arm"}))
        return 0
    if args.action == "show":
        entry = held_one(base)
        print(json.dumps({"ctx": entry.get("ctx"),
                          "messages": entry.get("messages")}, indent=1)[:4000])
        return 0
    if args.action == "step":
        print(call(base, "/api/step"))
        return 0
    if args.action == "drop":
        print(call(base, "/api/drop"))
        return 0

    # nodie
    entry = held_one(base)
    cleared = 0
    for i, msg in enumerate(entry.get("messages") or []):
        for j, sa in enumerate(msg.get("strargs") or []):
            if not isinstance(sa, dict):
                continue                      # already a plain string: not a combo
            patched, n = strip_die(sa)
            if not n:
                continue
            res = call(base, "/api/patch",
                       {"message": i, "strarg": j, "value": patched})
            if res.get("error"):
                sys.exit(f"patch failed: {res['error']}")
            cleared += n
    if not cleared:
        print("no `die` flag in the held reply -- nothing killed this turn. "
              "Step past it and try again on a killing blow.")
        return 1
    print(f"cleared {cleared} die flag(s); releasing")
    print(call(base, "/api/step"))
    return 0


if __name__ == "__main__":
    sys.exit(main())

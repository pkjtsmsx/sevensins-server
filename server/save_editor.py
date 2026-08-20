#!/usr/bin/env python3
"""Save editor: a local HTTP service for editing accounts by NAME, not by id.

Built for a non-technical player running the Android host app, where the account file
lives at `<filesDir>/sevensins/accounts/<id>.json` -- private internal storage that no
file manager, USB cable or PC tool can reach on an unrooted phone. So the editor runs
ON the device, next to the servers that already do, and the player opens it in their
own browser. There is no export/import shuffle and nothing to install.

Deliberately stdlib-only and a SEPARATE module from bundle_server (which is tuned to
the CDN path shape and has no business growing an API), matching how
tools/serve_hostapp_update.py was kept separate for the same reason.

**The one rule that matters: never write an account that is being played.**
`titan_server.handle` loads state once at login and keeps it in a local for the life
of the connection, writing it back on every change -- so an edit landing on disk
underneath a logged-in player is silently undone by their next tap. Every write here
goes through `_guard_live`, which refuses while that player has a live session.

Usage:  save_editor.py [port] [--accounts DIR] [--bind HOST]
"""
import argparse
import http.server
import json
import os
import shutil
import time
import urllib.parse

import design_data as dd
import player_state as ps

HERE = os.path.dirname(os.path.abspath(__file__))

# Currency ids are CurrencyType enum values, not item ids -- the client's own
# `Balance(CurrencyType.Cash)` sums 1 and 32 (see player_state.core). They have no row
# in the item table, so they are named here or they show up as bare numbers.
CURRENCIES = {
    "1": "Diamonds (free)",
    "32": "Diamonds (paid)",
    "16": "Mira (coins)",
    "64": "Guild Points",
    "48": "DMM Cash (unused)",
}

# Energy ids, same story -- EnergyType, not items.
ENERGIES = {
    "1": "Stamina",
    "16": "Arena Ticket",
    "17": "Raid Ticket",
    "18": "Event Ticket",
}

# Hard ceilings taken from the design data rather than invented, so a typo cannot
# produce a state the client will not render. See _limits().
STAR_MAX = 6
SUPER_STAR_MAX = 6


def _log(msg):
    print(time.strftime("%H:%M:%S ") + "[editor] " + msg, flush=True)


# ---- name resolution -------------------------------------------------------

_names = {"item": None, "char": None}


def item_names():
    """{id: English name} for every item that has one. ~22.7k rows, built once."""
    if _names["item"] is None:
        _names["item"] = {
            int(k): v["_itemName_en"]
            for k, v in (dd.rows("item") or {}).items()
            if v.get("_itemName_en") and v["_itemName_en"] != "Unuseful"
        }
    return _names["item"]


# Placeholder cells the design pack uses for "no string here".
_BLANK = ("", "-", "Unuseful")


def char_names():
    """{id: (base name, variant title)}.

    **`_name_eng` and `_title_en`, not `_name_en`.** A cast's identity in this game is
    the pair: nine different casts are all called LUCIFER, and what tells them apart in
    game is the title -- Pride, Vesperia, Summer Muse, Abyssal Prime. `_title_en` is
    populated on all 3643 rows. `_name_eng` is the title-case form and covers 3642 of
    them against `_name_en`'s 1636, so it also rescues the 22 casts in a real roster
    that were rendering as a bare "Cast 30251".
    """
    if _names["char"] is None:
        out = {}
        for k, v in (dd.rows("char") or {}).items():
            base = v.get("_name_eng") or v.get("_name_en") or ""
            title = v.get("_title_en") or ""
            out[int(k)] = (base if base not in _BLANK else "",
                           title if title not in _BLANK else "")
        _names["char"] = out
    return _names["char"]


def item_name(iid):
    # Falling back to the id (rather than "Unknown") keeps an unnamed row editable
    # instead of turning it into an anonymous entry the player cannot tell apart.
    return item_names().get(int(iid)) or f"Item {iid}"


def char_name(cid):
    """The base name alone -- what the roster SORTS and groups on, so every Belphegor
    stays together instead of scattering under its title's first letter."""
    base, _ = char_names().get(int(cid)) or ("", "")
    return base or f"Cast {cid}"


def char_display(cid):
    """What the player is shown: "Chainsaw Sweetheart Belphegor"."""
    base, title = char_names().get(int(cid)) or ("", "")
    base = base or f"Cast {cid}"
    return f"{title} {base}".strip() if title else base


# ---- account discovery -----------------------------------------------------

def accounts_dir():
    # Read at call time, not import time: the host app sets SEVENSINS_ACCOUNTS from
    # its filesDir well after this module is first imported.
    return ps.core.STATE_DIR if hasattr(ps, "core") else ps.STATE_DIR


def list_accounts():
    d = accounts_dir()
    out = []
    if not os.path.isdir(d):
        return out
    for fn in sorted(os.listdir(d)):
        # Dotfiles are the server's own bookkeeping (.live_sessions.json), not saves --
        # without this they list as an account named ".live_sessions" with 0 items, and
        # sort FIRST, so the editor opens on it instead of the player's actual save.
        if not fn.endswith(".json") or fn.startswith("."):
            continue
        pid = fn[:-5]
        path = os.path.join(d, fn)
        row = {"player_id": pid, "size": os.path.getsize(path),
               "modified": os.path.getmtime(path), "name": pid, "level": None,
               "readable": True}
        try:
            with open(path) as f:
                st = json.load(f)
            row["name"] = st.get("name") or pid
            row["level"] = (st.get("level") or {}).get("lv")
        except Exception:
            # Surfaced rather than hidden: a save that will not parse is exactly the
            # one the player needs to know about, and load() refuses to touch it too.
            row["readable"] = False
        out.append(row)
    return out


# ---- the editable view -----------------------------------------------------

def _limits(state):
    lv = state.get("level") or {}
    return {"level_max": lv.get("lv_max") or 200,
            "star_max": STAR_MAX, "super_star_max": SUPER_STAR_MAX}


def account_view(pid):
    """The state, plus every id resolved to a name, in the shape the UI renders."""
    state = _read(pid)
    cur = state.get("currency") or {}
    currencies = [{"id": k, "name": CURRENCIES.get(k, f"Currency {k}"),
                   "amount": int(v)}
                  for k, v in sorted(cur.items(), key=lambda kv: int(kv[0]))]

    energies = [{"id": k, "name": ENERGIES.get(k, f"Energy {k}"),
                 "amount": int(v.get("energy", 0)), "cap": int(v.get("cap", 0))}
                for k, v in sorted((state.get("energy") or {}).items(),
                                   key=lambda kv: int(kv[0]))]

    items = []
    for stype, slots in (state.get("backpack") or {}).items():
        for sid, rec in slots.items():
            iid = rec.get("iid")
            if iid is None:
                continue
            items.append({"storage": stype, "slot": sid, "iid": int(iid),
                          "name": item_name(iid), "amount": int(rec.get("amount", 0)),
                          "uid": rec.get("uid") or ""})
    items.sort(key=lambda r: r["name"].lower())

    # **Telling duplicate copies apart is the whole problem here.** Two different
    # things collide under one name: several DIFFERENT casts share a name (LUCIFER
    # spans 9 char ids), which the id separates -- and the same cast is often owned
    # SEVERAL TIMES (this save has 5 Leviathans on id 10011), where the id is
    # identical and useless. Edits were always independent, since every row is keyed
    # by its roster uid; what was missing was any way to see WHICH copy a row is. So
    # each row carries what a player would actually recognise it by: the party it is
    # in, whether it is their helper, and how much gear is on it.
    party_of = {}
    for i, form in enumerate(state.get("formations") or []):
        for slot in (form.get("array") or []):
            if slot:
                party_of[slot] = i + 1
    helper = state.get("helper")

    roster = [{"uid": uid, "id": int(c.get("id", 0)), "name": char_name(c.get("id", 0)),
               "display": char_display(c.get("id", 0)),
               "lv": int(c.get("lv", 1)), "star": int(c.get("star", 1)),
               "super_star": int(c.get("super_star", 0)),
               "limit_book": int(c.get("limit_book", 0)),
               "limit_char": int(c.get("limit_char", 0)),
               "party": party_of.get(uid),
               "helper": uid == helper,
               "gear": sum(1 for e in (c.get("equips_list") or []) if e)}
              for uid, c in (state.get("roster") or {}).items()]
    # Within a name, the copy the player actually uses leads: in a party first, then
    # the most developed. Stops the lv1 spares burying the one they came here to edit.
    roster.sort(key=lambda r: (r["name"].lower(), r["party"] or 99, -r["lv"],
                               -r["star"], -r["gear"], r["uid"]))

    # How many copies share this row's cast id, so the UI can say "1 of 5".
    counts = {}
    for r in roster:
        counts[r["id"]] = counts.get(r["id"], 0) + 1
    seen = {}
    for r in roster:
        seen[r["id"]] = seen.get(r["id"], 0) + 1
        r["copy"] = seen[r["id"]]
        r["copies"] = counts[r["id"]]

    return {
        "player_id": pid,
        "name": state.get("name") or pid,
        "level": state.get("level") or {},
        "currencies": currencies,
        "energies": energies,
        "items": items,
        "roster": roster,
        "limits": _limits(state),
        "raw_keys": sorted(state.keys()),
    }


# ---- reading and writing ---------------------------------------------------

def _read(pid):
    """Read a save WITHOUT going through ps.load().

    ps.load() normalises (seeds the roster, clamps stars, refills passes, purges
    orphan quests) and writes the result back. That is right for a login and wrong
    here: the editor must show what is actually on disk, and must not rewrite a file
    the player only wanted to look at.
    """
    if not pid or pid.startswith(".") or "/" in pid or "\\" in pid:
        # Also the path-traversal guard: `pid` arrives straight off the URL.
        raise KeyError(f"no such account: {pid}")
    path = os.path.join(accounts_dir(), f"{pid}.json")
    if not os.path.isfile(path):
        raise KeyError(f"no such account: {pid}")
    with open(path) as f:
        return json.load(f)


def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError, TypeError):
        return False
    return True


def live_sessions():
    """Every connected client, from BOTH registries.

    In the Android host app the editor and titan_server share one interpreter, so the
    in-memory list is authoritative. On a desktop they are separate processes and that
    list is always empty here -- which would make the guard below silently pass. So the
    server also publishes its sessions to a file; entries there are trusted only while
    the writing process is still alive, or a crashed server would block editing forever.
    """
    out = []
    try:
        import titan_server
        out += titan_server.live_sessions()
    except Exception:
        pass
    try:
        with open(os.path.join(accounts_dir(), ".live_sessions.json")) as f:
            d = json.load(f)
        if _pid_alive(d.get("pid")) and d.get("pid") != os.getpid():
            out += d.get("sessions") or []
    except Exception:
        pass
    return out


def _guard_live(pid):
    """Refuse to write an account whose player is connected right now."""
    for s in live_sessions():
        if s.get("player_id") in (None, pid):
            who = s.get("player_id") or "a client that has not logged in yet"
            raise PermissionError(
                f"{who} is connected from {s.get('addr')}. Close the game completely, "
                f"then edit -- otherwise the running session writes its own copy back "
                f"over your changes the next time you tap anything."
            )


def _backup(path):
    """Keep a timestamped copy before every write.

    This codebase has already lost a 100 KB account to a well-meaning write (see
    player_state.load's refusal to overwrite an unreadable save). An editor that hands
    a player a text box is strictly more dangerous than that was.
    """
    if not os.path.isfile(path):
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = f"{path}.bak-{stamp}"
    # **Never reuse a backup name.** The stamp is second-resolution, so two saves in
    # the same second collided and the second one overwrote the first -- which meant a
    # player who double-tapped Save destroyed their only copy of the PRE-EDIT state,
    # the exact thing these files exist to preserve. Suffix until the name is free.
    n = 1
    while os.path.exists(dest):
        dest = f"{path}.bak-{stamp}-{n}"
        n += 1
    shutil.copy2(path, dest)
    # Keep the OLDEST plus the nine most recent. Unbounded backups of a 100 KB file on
    # phone storage is its own bug, but a plain "keep the last N" prunes oldest-first --
    # so the eleventh save would delete the only copy of the account as it was BEFORE
    # anyone started editing, which is the one a player asking for help actually wants.
    base = os.path.basename(path) + ".bak-"
    d = os.path.dirname(path)
    old = sorted(x for x in os.listdir(d) if x.startswith(base))
    for name in old[1:-9]:
        try:
            os.remove(os.path.join(d, name))
        except OSError:
            pass
    return dest


def _write(pid, state):
    path = os.path.join(accounts_dir(), f"{pid}.json")
    backup = _backup(path)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1, sort_keys=True)
    os.replace(tmp, path)               # atomic, matching player_state._save_locked
    _log(f"wrote {pid} (backup {os.path.basename(backup) if backup else 'none'})")
    return backup


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def apply_edits(pid, edits):
    """Apply a validated edit set. -> {"ok": True, "backup": ...} or raises.

    Every numeric field is clamped rather than rejected: a player who types 9999 into
    a star box should get 6, not an error dialog they cannot interpret.
    """
    _guard_live(pid)
    state = _read(pid)
    limits = _limits(state)
    changed = []

    for cid, amount in (edits.get("currencies") or {}).items():
        amount = _clamp(int(amount), 0, 2_000_000_000)
        state.setdefault("currency", {})[str(cid)] = amount
        changed.append(f"{CURRENCIES.get(str(cid), cid)}={amount}")

    for eid, row in (edits.get("energies") or {}).items():
        e = state.setdefault("energy", {}).setdefault(str(eid), {"cap": 0, "energy": 0})
        if "amount" in row:
            e["energy"] = _clamp(int(row["amount"]), 0, 999_999)
        if "cap" in row:
            e["cap"] = _clamp(int(row["cap"]), 0, 999_999)
        changed.append(f"{ENERGIES.get(str(eid), eid)}={e['energy']}/{e['cap']}")

    lv = edits.get("level") or {}
    if lv:
        node = state.setdefault("level", {"lv": 1, "lv_max": 200, "lv_min": 1,
                                          "xp": 0, "xp_cap": 100})
        if "lv" in lv:
            node["lv"] = _clamp(int(lv["lv"]), int(node.get("lv_min", 1)),
                                int(limits["level_max"]))
        if "xp" in lv:
            node["xp"] = _clamp(int(lv["xp"]), 0, 2_000_000_000)
        changed.append(f"level={node['lv']}")

    for row in (edits.get("items") or []):
        _set_item(state, row)
        changed.append(f"{item_name(row['iid'])}x{row.get('amount')}")

    for row in (edits.get("roster") or []):
        c = (state.get("roster") or {}).get(row["uid"])
        if not c:
            continue
        if "lv" in row:
            c["lv"] = _clamp(int(row["lv"]), 1, int(limits["level_max"]))
        if "star" in row:
            c["star"] = _clamp(int(row["star"]), 1, limits["star_max"])
        if "super_star" in row:
            c["super_star"] = _clamp(int(row["super_star"]), 0,
                                     limits["super_star_max"])
        changed.append(f"{char_name(c.get('id', 0))} lv{c.get('lv')}")

    raw = edits.get("raw")
    if raw:
        # The escape hatch for the ~40 keys with no dedicated tab. Replaces whole
        # top-level keys only, and re-serialises through json so a malformed blob
        # fails HERE rather than at the player's next login.
        for k, v in raw.items():
            state[k] = v
            changed.append(f"raw:{k}")

    backup = _write(pid, state)
    _log(f"{pid}: " + ", ".join(changed[:12]) + (" ..." if len(changed) > 12 else ""))
    return {"ok": True, "backup": os.path.basename(backup) if backup else None,
            "changed": changed}


def _instance_storage(iid):
    """Which storage an INSTANCE item lives in, or None for an ordinary stack.

    Starshards (storage 2) and Soulmirrors (storage 3) are instances, not stacks: each
    one is its own slot with a uid and a rolled `attr` block. `item_bucket` already
    encodes the same rule for deciding which sync to push, so ask it rather than
    re-deriving the action ranges here.
    """
    if ps.item_bucket(iid) != "equipment":
        return None
    action = (dd.row("item", iid) or {}).get("_action")
    return (str(ps.BP_STORAGE_SOULFRAG)
            if action in ps.SOULFRAG_SLOT_INDEX else str(ps.BP_STORAGE_EQUIPMENT))


def _set_instances(state, iid, want):
    """Bring the number of owned instances of `iid` to `want`, granting or deleting.

    **Instances cannot be written as a stack.** The editor used to add
    `{"amount": n, "attr": {}, "iid": iid, "uid": ""}` to storage 1 for everything,
    which for a starshard or soulmirror is wrong in three ways at once: wrong storage,
    no uid, and no rolled attributes. The client then failed to file the row and the
    WHOLE list went blank -- "No Available Soulmirror", Owned -/- -- until the bad entry
    was removed again. Granting goes through the same path drops use, so an edited piece
    is indistinguishable from an earned one.
    """
    storage = _instance_storage(iid)
    bag = state.setdefault("backpack", {}).setdefault(storage, {})
    owned = [sid for sid, rec in bag.items() if int(rec.get("iid", -1)) == iid]
    want = _clamp(int(want), 0, 200)
    if want > len(owned):
        for _ in range(want - len(owned)):
            ps.grant_reward(state, iid, 1)
    else:
        # Drop the highest slots first; a lower slot is more likely to be equipped and
        # referenced by a cast, and deleting one of those leaves a dangling reference.
        for sid in sorted(owned, key=lambda x: -int(x))[:len(owned) - want]:
            bag.pop(sid, None)
    return want


def _set_item(state, row):
    """Set an item's amount, adding it to a free slot if the player has none.

    Backpack storages are keyed by ClientBackpackType and then by SLOT id, with the
    item id living in the record -- so "give me 50 Awaker Scrolls" is either an
    amount change on an existing slot or a brand new slot, never a dict update keyed
    by item id.

    Starshards and Soulmirrors are the exception and are handled as instances.
    """
    iid = int(row["iid"])
    if _instance_storage(iid):
        return _set_instances(state, iid, row.get("amount", 0))
    amount = _clamp(int(row.get("amount", 0)), 0, 999_999)
    storage = str(row.get("storage") or "1")
    bp = state.setdefault("backpack", {}).setdefault(storage, {})
    for sid, rec in bp.items():
        if int(rec.get("iid", -1)) == iid:
            if amount == 0:
                del bp[sid]
            else:
                rec["amount"] = amount
            return
    if amount == 0:
        return
    slot = str(max((int(s) for s in bp), default=0) + 1)
    bp[slot] = {"amount": amount, "attr": {}, "iid": iid, "sid": int(slot), "uid": ""}


def search_items(q, limit=50):
    q = (q or "").strip().lower()
    if not q:
        return []
    out = []
    for iid, name in item_names().items():
        if q in name.lower():
            out.append({"iid": iid, "name": name})
            if len(out) >= limit:
                break
    out.sort(key=lambda r: (not r["name"].lower().startswith(q), r["name"].lower()))
    return out


# ---- HTTP ------------------------------------------------------------------

UI_FILE = os.path.join(HERE, "save_editor_ui.html")


class Handler(http.server.BaseHTTPRequestHandler):

    def _send(self, code, body, ctype="application/json"):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # The page is served to the phone's own browser and never embedded; no cache,
        # so a hot-updated UI file is picked up without the player clearing anything.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _fail(self, exc):
        # PermissionError is the "you are still logged in" guard and is the ONE error
        # the player is expected to hit, so it gets its own status and a clean message.
        code = {KeyError: 404, PermissionError: 409, ValueError: 400}.get(type(exc), 500)
        msg = str(exc) or type(exc).__name__
        if isinstance(exc, KeyError):
            msg = msg.strip("'")
        _log(f"{code}: {msg}")
        self._send(code, {"error": msg})

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        path, qs = url.path, urllib.parse.parse_qs(url.query)
        try:
            if path in ("/", "/index.html"):
                if not os.path.isfile(UI_FILE):
                    return self._send(500, b"save_editor_ui.html is missing",
                                      "text/plain; charset=utf-8")
                with open(UI_FILE, "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if path == "/api/accounts":
                return self._send(200, {"accounts": list_accounts(),
                                        "live": _live_summary()})
            if path == "/api/items":
                return self._send(200, {"items": search_items(qs.get("q", [""])[0])})
            if path.startswith("/api/account/"):
                pid = path.rsplit("/", 1)[-1]
                if path.endswith("/raw"):
                    pid = path.split("/")[3]
                    return self._send(200, {"raw": _read(pid)})
                return self._send(200, account_view(pid))
            return self._send(404, {"error": "no such endpoint"})
        except Exception as e:
            return self._fail(e)

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            if url.path.startswith("/api/account/"):
                pid = url.path.rsplit("/", 1)[-1]
                return self._send(200, apply_edits(pid, body))
            return self._send(404, {"error": "no such endpoint"})
        except json.JSONDecodeError as e:
            return self._fail(ValueError(f"malformed request: {e}"))
        except Exception as e:
            return self._fail(e)

    def log_message(self, *a):
        pass                            # _log carries what matters; this is per-asset noise


def _live_summary():
    return [{"player_id": s.get("player_id"), "addr": s.get("addr")}
            for s in live_sessions()]


_httpd = None


def serve(port=8099, bind="127.0.0.1"):
    """Blocking serve loop, callable from an embedding host (the Android app).

    Binds LOOPBACK by default, unlike bundle_server: this endpoint rewrites saves with
    no authentication, and the phone is on networks its owner does not control. The
    on-device UI reaches it at 127.0.0.1, so nothing needs it exposed.
    """
    global _httpd
    _httpd = http.server.ThreadingHTTPServer((bind, port), Handler)
    _log(f"save editor listening on {bind}:{port} (accounts={accounts_dir()})")
    try:
        _httpd.serve_forever()
    finally:
        _httpd = None


def shutdown():
    if _httpd is not None:
        _httpd.shutdown()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("port", nargs="?", type=int, default=8099)
    ap.add_argument("--accounts", help="override SEVENSINS_ACCOUNTS")
    ap.add_argument("--bind", default="127.0.0.1")
    args = ap.parse_args()
    if args.accounts:
        os.environ["SEVENSINS_ACCOUNTS"] = os.path.abspath(args.accounts)
        ps.core.STATE_DIR = os.environ["SEVENSINS_ACCOUNTS"]
    serve(args.port, args.bind)


if __name__ == "__main__":
    main()

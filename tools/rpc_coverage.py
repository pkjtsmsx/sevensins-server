#!/usr/bin/env python3
"""Report which client->server RPCs we answer, per subsystem.

The old generator scanned for literal `index == X and cmd == Y` pairs and therefore
MISSED every reply that arrives another way -- which is how it reported Battle as 1/16
when the truth was 8/16. This one resolves symbols instead:

  * `titan_server` is IMPORTED, so `cmd == CHAR_REQ_WEAR_RUNE` resolves to 295 rather
    than being skipped as an unknown name;
  * handlers are matched as (subsystem, cmd) PAIRS -- counting by bare command number
    credits a subsystem for a number handled somewhere else entirely;
  * the `build_sync_replies` / `FIRE_AND_FORGET` tables' `(index, cmd)` keys are read
    from the source, and the index hex is mapped back to its subsystem;
  * `battle.py`'s REQ_* constants count, since `battle_replies()` dispatches on them
    inside a function the pair-scan cannot see.

Two enum shapes exist. Most subsystems have a one-directional `<Name>RpcServerCmd`.
A few (Backpack, Mail, LoginBonus, Session, Redeem) use a single `<Name>RpcCmd` holding
BOTH directions interleaved, so replies are filtered out by name.

Usage:  tools/rpc_coverage.py [--missing] [--subsystem Char]
"""
import os, re, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "server")
DUMP = os.path.join(ROOT, "re/il2cpp227/dump.cs")

SERVER_ENUM_RE = re.compile(
    r"public enum (\w+?)RpcServerCmd\s*//[^\n]*\n\{(.*?)^\}", re.S | re.M)
MIXED_ENUM_RE = re.compile(
    r"public enum (\w+?)RpcCmd\s*//[^\n]*\n\{(.*?)^\}", re.S | re.M)
MEMBER_RE = re.compile(r"public const \w+ (\w+) = (\d+);")

# Members of a mixed enum that are replies/pushes, not things the client asks for.
REPLY_HINT = re.compile(r"rply|reply|_ack|change|extended|error", re.I)


def client_commands():
    """-> {subsystem: {cmd: name}} of everything the client can SEND."""
    text = open(DUMP, encoding="utf-8", errors="ignore").read()
    out = {}
    for sub, body in SERVER_ENUM_RE.findall(text):
        cmds = {int(n): name for name, n in MEMBER_RE.findall(body)}
        if cmds:
            out[sub] = cmds
    for sub, body in MIXED_ENUM_RE.findall(text):
        cmds = {int(n): name for name, n in MEMBER_RE.findall(body)
                if not REPLY_HINT.search(name) and int(n) != 65535}
        if cmds:
            out.setdefault(sub, {}).update(cmds)
    return out


def _index_labels(ts, subs):
    """-> {index value: subsystem} from titan_server's *_SERVER index constants."""
    labels = {}
    for name in dir(ts):
        if not name.endswith("_SERVER"):
            continue
        val = getattr(ts, name)
        if not isinstance(val, int):
            continue
        stem = name[:-len("_SERVER")].replace("PLAYER_", "").replace("_", "")
        for sub in subs:
            if sub.upper() == stem:
                labels[val] = sub
    return labels


def handled_pairs():
    """-> {(subsystem or None, cmd)} we answer."""
    sys.path.insert(0, SERVER)
    os.chdir(SERVER)
    # titan_server reads sys.argv[1] as its listen PORT at import time, so importing it
    # from a script that takes its own flags died with
    # `invalid literal for int(): '--missing'` -- i.e. --missing had never once run.
    argv, sys.argv = sys.argv, sys.argv[:1]
    try:
        import titan_server as ts                               # noqa: E402
    finally:
        sys.argv = argv
    src = open(os.path.join(SERVER, "titan_server.py"), encoding="utf-8").read()
    subs = list(client_commands())
    labels = _index_labels(ts, subs)

    def resolve(tok):
        if tok.isdigit():
            return int(tok)
        val = getattr(ts, tok, None)
        return val if isinstance(val, int) else None

    pairs = set()
    # `index == A ... cmd == B` within one elif condition (may wrap lines).
    for idx_tok, gap, cmd_tok in re.findall(
            r"index == (\w+)(.{0,160}?)cmd == (\w+)", src, re.S):
        if "elif" in gap or "index ==" in gap:
            continue
        idx, cmd = resolve(idx_tok), resolve(cmd_tok)
        if idx is not None and cmd is not None:
            pairs.add((labels.get(idx), cmd))
    # `index == A ... cmd in (B, C, ...)` -- one branch answering several commands.
    # Without this the scan under-reports exactly like it did for Battle: the Guild
    # quit/disband, recommend/search and needs-another-player branches are all written
    # this way, and all seven read as unhandled.
    for idx_tok, gap, group in re.findall(
            r"index == (\w+)(.{0,160}?)cmd in \(([^)]*)\)", src, re.S):
        if "elif" in gap or "index ==" in gap:
            continue
        idx = resolve(idx_tok)
        if idx is None:
            continue
        for tok in re.findall(r"\w+", group):
            cmd = resolve(tok)
            if cmd is not None:
                pairs.add((labels.get(idx), cmd))
    # Table-driven: (0xINDEX, cmd): ...
    for idx_hex, cmd in re.findall(
            r"\((0x[0-9A-Fa-f]+),\s*(\d+)\)\s*:", src):
        pairs.add((labels.get(int(idx_hex, 16)), int(cmd)))
    # battle_replies dispatches inside a function.
    try:
        import battle as bt                                     # noqa: E402
        for name in dir(bt):
            if name.startswith("REQ_") and isinstance(getattr(bt, name), int):
                pairs.add(("Battle", getattr(bt, name)))
    except Exception as exc:                        # pragma: no cover
        print(f"warning: could not import battle.py ({exc})", file=sys.stderr)
    return pairs


def main():
    want_missing = "--missing" in sys.argv
    only = None
    if "--subsystem" in sys.argv:
        only = sys.argv[sys.argv.index("--subsystem") + 1].lower()

    cmds = client_commands()
    pairs = handled_pairs()
    # A pair whose index constant we could not label still proves the cmd is handled.
    loose = {c for s, c in pairs if s is None}

    total = done = 0
    rows = []
    for sub in sorted(cmds):
        if only and sub.lower() != only:
            continue
        have = sorted(c for c in cmds[sub]
                      if (sub, c) in pairs or c in loose)
        miss = sorted(c for c in cmds[sub] if c not in have)
        total += len(cmds[sub])
        done += len(have)
        rows.append((sub, have, miss, cmds[sub]))

    print(f"{done} of {total} client->server commands answered\n")
    for sub, have, miss, names in rows:
        print(f"{sub:<12} {len(have):>3}/{len(names):<3}"
              + ("   COMPLETE" if not miss else ""))
        if want_missing and miss:
            for c in miss:
                print(f"    {c:>5}  {names[c]}")
    if not want_missing:
        print("\n(run with --missing to list the unanswered commands)")


if __name__ == "__main__":
    main()

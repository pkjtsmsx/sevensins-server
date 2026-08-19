#!/usr/bin/env python3
"""Report which server->client RPCs we ever SEND, per subsystem.

The mirror of rpc_coverage.py, and the more useful of the two for the bugs that actually
bite. That tool audits the 186 commands the client can ASK for; this one audits the 213
it can RECEIVE. Nothing in the request list can tell you about a push the client waits
for but never requests -- and those are exactly the ones that fail silently:

  * **563** (`getFlvRewards`) queues the char_flv rows `PanelEvilUp` shows. Unsent, the
    Karma RANK UP splash opened and closed instantly. No request exists for it.
  * **785** (`reward_get_reply`) announces the Guild Raid end-of-day settlement as two
    item popups. Unsent, the rewards arrive with no notice at all.
  * **552** (`update_friendly`) is what raises the CharEvent the Consonance room
    refreshes on.

A missing push has no error, no reply timeout, and no log line -- the client simply
never learns something happened. So "which of these do we never send?" is a real
worklist, not a curiosity.

Method mirrors rpc_coverage: import titan_server so constants resolve to numbers rather
than being skipped as unknown names, and match (subsystem, cmd) PAIRS -- crediting a
subsystem for a number sent somewhere else is how the request-side scan used to
under-report Battle as 1/16.

Send sites recognised:
  * `uint_msg(INDEX, CMD, ...)` and friends -- any builder taking (index, cmd) first;
  * `backpack_msg(CMD, ...)` / `battle_msg(CMD, ...)`, whose subsystem is fixed;
  * the `build_sync_replies` table, whose keys are (index, cmd) pairs;
  * `send(MSG_RPC, ...)` wrappers are irrelevant -- the builder call is what carries the
    command, and it is always one of the above.

Usage:  tools/rpc_push_coverage.py [--missing] [--subsystem Char] [--sent]
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "server")
DUMP = os.path.join(ROOT, "re/il2cpp227/dump.cs")

CLIENT_ENUM_RE = re.compile(
    r"public enum (\w+?)RpcClientCmd\s*//[^\n]*\n\{(.*?)^\}", re.S | re.M)
MIXED_ENUM_RE = re.compile(
    r"public enum (\w+?)RpcCmd\s*//[^\n]*\n\{(.*?)^\}", re.S | re.M)
# A THIRD convention: Campaign/OFA/Raid/Ranking/Stage/Village use RpcReplyCmd +
# RpcRequestCmd instead of RpcClientCmd + RpcServerCmd. Missing it hid six whole
# subsystems from both coverage tools -- Stage alone is most of the campaign flow.
REPLY_ENUM_RE = re.compile(
    r"public enum (\w+?)RpcReplyCmd\s*//[^\n]*\n\{(.*?)^\}", re.S | re.M)
MEMBER_RE = re.compile(r"public const \w+ (\w+) = (\d+);")

# Members of a mixed enum that ARE pushes/replies (the inverse of rpc_coverage's filter).
REPLY_HINT = re.compile(r"rply|reply|_ack|change|extended|error", re.I)

# Builders whose first two positional args are (index, cmd).
INDEXED_BUILDERS = ("uint64_msg", "uint_msg", "sint_msg", "twostr_msg")
# Builders with a fixed subsystem and cmd as the first arg.
FIXED_BUILDERS = {"backpack_msg": "Backpack", "battle_msg": "Battle"}

# A handful of send sites pass the index as a RAW HEX literal with no named constant, so
# the name-pairing above cannot label them. Each was attributed by looking up which enum
# owns the command it carries (e.g. 0xFBC2FA08 sends 1801, and 1801 is Battle.Sync and
# nothing else). Without these the tool silently credited their commands to EVERY
# subsystem via the unlabelled fallback.
RAW_INDEX_SUBSYSTEM = {
    0x4C1872DD: "General",     # general_json, cmd 512
    0x7E7107E7: "Level",       # level sync, 512/513
    0xAE487D79: "Energy",
    0xBC8FDA7C: "Currency",
    0xC4A53FC0: "Backpack",    # the _BpMsg wrapper's own index
    0xFBC2FA08: "Battle",      # 1801 Sync
    0xB66EBE45: "Ranking",     # 530 sync_old_ranking_data_reply (LastSeasonGroupID)
    0xB8553314: "Friend",      # 528 helpers / 529 friends / 530 block / 531 social
}


def push_commands():
    """-> {subsystem: {cmd: name}} of everything the client can RECEIVE."""
    text = open(DUMP, encoding="utf-8", errors="ignore").read()
    out = {}
    for pat in (CLIENT_ENUM_RE, REPLY_ENUM_RE):
        for sub, body in pat.findall(text):
            cmds = {int(n): name for name, n in MEMBER_RE.findall(body)}
            if cmds:
                out.setdefault(sub, {}).update(cmds)
    # The mixed <Name>RpcCmd enums hold both directions; here we want the reply half.
    for sub, body in MIXED_ENUM_RE.findall(text):
        cmds = {int(n): name for name, n in MEMBER_RE.findall(body)
                if REPLY_HINT.search(name) and int(n) != 65535}
        if cmds:
            out.setdefault(sub, {}).update(cmds)
    return out


def _client_index_labels(ts, subs):
    """-> {index value: subsystem} for the SERVER->CLIENT index constants.

    They are named by convention as the bare stem whose `<stem>_SERVER` twin is the
    request index (PLAYER_MAIL / PLAYER_MAIL_SERVER), or explicitly `<stem>_CLIENT`
    (CHALLENGE_CLIENT / CHALLENGE_SERVER). Deriving it from the pairing rather than
    hardcoding a list means a new subsystem is picked up for free.
    """
    labels = {}
    names = {n for n in dir(ts) if isinstance(getattr(ts, n), int)}
    for name in sorted(names):
        if name.endswith("_SERVER"):
            continue
        stem = name[:-len("_CLIENT")] if name.endswith("_CLIENT") else name
        if f"{stem}_SERVER" not in names:
            continue
        val = getattr(ts, name)
        key = stem.replace("PLAYER_", "").replace("_", "")
        for sub in subs:
            if sub.upper() == key:
                labels[val] = sub
    return labels


def sent_pairs():
    """-> ({(subsystem or None, cmd)}, titan_server module)."""
    sys.path.insert(0, SERVER)
    os.chdir(SERVER)
    # titan_server reads sys.argv[1] as its listen PORT at import time; the request-side
    # tool hit exactly this and its --missing flag had never once run.
    argv, sys.argv = sys.argv, sys.argv[:1]
    try:
        import titan_server as ts
    finally:
        sys.argv = argv
    src = open(os.path.join(SERVER, "titan_server.py"), encoding="utf-8").read()
    labels = _client_index_labels(ts, list(push_commands()))
    labels.update(RAW_INDEX_SUBSYSTEM)

    def resolve(tok):
        """Constant token -> int. Handles DOTTED names: the battle commands are written
        `bt.CMD_ATTACK`, and resolving only bare names reported Battle as 1/17."""
        tok = tok.strip()
        if re.fullmatch(r"0x[0-9A-Fa-f]+", tok):
            return int(tok, 16)
        if tok.isdigit():
            return int(tok)
        obj = ts
        for part in tok.split("."):
            obj = getattr(obj, part, None)
            if obj is None:
                return None
        return obj if isinstance(obj, int) else None

    pairs = set()
    for builder in INDEXED_BUILDERS:
        for idx_tok, cmd_tok in re.findall(
                rf"\b{builder}\(\s*([\w.]+)\s*,\s*([\w.]+)\s*[,)]", src):
            idx, cmd = resolve(idx_tok), resolve(cmd_tok)
            if cmd is not None:
                pairs.add((labels.get(idx), cmd))
    for builder, sub in FIXED_BUILDERS.items():
        for cmd_tok in re.findall(rf"\b{builder}\(\s*([\w.]+)\s*[,)]", src):
            cmd = resolve(cmd_tok)
            if cmd is not None:
                pairs.add((sub, cmd))
    # A few messages skip the builders and pack `make_rpc` directly, with the command
    # as the first uint32 group -- the session heartbeat is the live example.
    for idx_tok, cmd_tok in re.findall(
            r"make_rpc\(\s*([\w.]+)\s*,\s*uint32s=\[\[\s*([\w.]+)", src):
        idx, cmd = resolve(idx_tok), resolve(cmd_tok)
        if cmd is not None:
            pairs.add((labels.get(idx), cmd))
    # NOTE: `build_sync_replies` is keyed by the (index, cmd) of the REQUEST, not of the
    # reply -- scanning those keys credited request command numbers as though we had
    # sent them. The replies that table produces are built by the same builders scanned
    # above, so dropping it loses nothing and removes a whole class of false positives.
    return pairs


def main():
    want_missing = "--missing" in sys.argv
    want_sent = "--sent" in sys.argv
    only = None
    if "--subsystem" in sys.argv:
        only = sys.argv[sys.argv.index("--subsystem") + 1].lower()

    cmds = push_commands()
    pairs = sent_pairs()
    # A pair whose index we could not label still proves the cmd is sent somewhere.
    loose = {c for s, c in pairs if s is None}

    total = done = 0
    rows = []
    for sub in sorted(cmds):
        if only and sub.lower() != only:
            continue
        have = sorted(c for c in cmds[sub] if (sub, c) in pairs or c in loose)
        miss = sorted(c for c in cmds[sub] if c not in have)
        total += len(cmds[sub])
        done += len(have)
        rows.append((sub, have, miss, cmds[sub]))

    print(f"{done} of {total} server->client commands ever sent\n")
    for sub, have, miss, names in rows:
        print(f"{sub:<12} {len(have):>3}/{len(names):<3}"
              + ("   COMPLETE" if not miss else ""))
        if want_sent and have:
            for c in have:
                print(f"    sent {c:>5}  {names[c]}")
        if want_missing and miss:
            for c in miss:
                print(f"    {c:>5}  {names[c]}")
    if not (want_missing or want_sent):
        print("\n(--missing lists what we never send, --sent lists what we do)")


if __name__ == "__main__":
    main()

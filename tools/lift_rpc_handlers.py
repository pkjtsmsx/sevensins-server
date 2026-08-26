#!/usr/bin/env python3
"""Lift a subsystem's branches out of titan_server.handle() into @rpc handlers.

    python3 tools/lift_rpc_handlers.py PLAYER_CHAR_SERVER BACKPACK_SERVER SHOP_SERVER

For every 20-space-indented `elif index == X and cmd == Y:` (or the wrapped
`elif (index == X
 and cmd in (A, B)):` form) whose X is named on the command line,
the branch body becomes a module-level function registered with @rpc, inserted before
recv_exact(), and the branch is deleted from the chain. Names that were locals of
handle() -- state, intargs, strargs, strargs2, rid, cmd, session, send -- are rewritten
to attributes of the Rpc argument by TOKEN, so comments and string literals are left
alone and an f-string's braces are rewritten correctly (Python 3.12+ tokenizer).

This is how Char (24), Backpack (9) and Shop (5) moved in one pass. It does not check
that a branch is liftable: run it on one subsystem, py_compile, run the suites, and
read the output -- a branch that rebinds `state` or `cur_battle`, or uses
`continue`, cannot leave the chain and must be skipped by hand. The section heading
is the constant's stem; edit it if the subsystem has a nicer name.
"""
import io, re, sys, tokenize
import os
P = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server", "titan_server.py")
LIFT = {name: name.replace("PLAYER_", "").replace("_SERVER", "").title() for name in sys.argv[1:]}
if not LIFT:
    sys.exit(__doc__)
# handle() local -> what a handler reads it as. `cur_battle` is the one that is not a
# plain attribute: it became Conn.battle so a handler can REBIND it (start a fight).
RENAME = {n: "r." + n for n in ("state", "intargs", "strargs", "strargs2", "rid", "cmd",
                                 "session", "send")}
RENAME["cur_battle"] = "r.cx.battle"
RENAME["cx"] = "r.cx"                 # the chain itself now says cx.battle
lines = open(P).read().split("\n")
IND = " " * 20
head_re = re.compile(r"^ {20}elif (\(?)index == (\w+)")

def parse_cond(i):
    """-> (index_tok, [cmd_toks], last_line_idx) for the elif starting at line i."""
    text, j = lines[i], i
    while not text.rstrip().endswith(":"):
        j += 1; text += " " + lines[j].strip()
    m = re.search(r"index == (\w+)", text); idx = m.group(1)
    m = re.search(r"cmd == (\w+)", text)
    if m: cmds = [m.group(1)]
    else:
        m = re.search(r"cmd in \(([^)]*)\)", text); cmds = re.findall(r"\w+", m.group(1))
    return idx, cmds, j

blocks = []   # (start, end_exclusive, idx, cmds)
i = 0
while i < len(lines):
    m = head_re.match(lines[i])
    if m and m.group(2) in LIFT:
        idx, cmds, j = parse_cond(i)
        k = j + 1
        while k < len(lines) and not re.match(r"^ {20}(elif|else|if)\b", lines[k]) and (lines[k].startswith(" " * 24) or not lines[k].strip()):
            k += 1
        # trim trailing blank lines back into the chain
        while not lines[k - 1].strip(): k -= 1
        blocks.append((i, k, idx, cmds)); i = k
    else:
        i += 1

def _inside_loop(lines, row):
    """Is line `row` lexically inside a for/while that starts within these lines?"""
    ind = len(lines[row]) - len(lines[row].lstrip())
    for j in range(row - 1, -1, -1):
        t = lines[j]
        if not t.strip():
            continue
        jind = len(t) - len(t.lstrip())
        if jind < ind:
            if re.match(r"\s*(for|while)\b", t):
                return True
            ind = jind          # left that block; keep walking out
    return False


def rewrite_names(src):
    """Rename handle() locals by TOKEN, and turn a loop-level `continue` into `return`.

    In the chain a `continue` means "stop handling this RPC" -- it continues the
    `for raw_rpc` loop. In a function that is `return`. But only when the `continue`
    is not inside a loop OF THE BODY'S OWN, where it means what it says; that case is
    refused rather than guessed.
    """
    toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    out = src.split("\n"); edits = []
    for t in toks:
        if t.type == tokenize.NAME and t.string in RENAME:
            edits.append((t.start[0] - 1, t.start[1], t.end[1], RENAME[t.string]))
        elif t.type == tokenize.NAME and t.string == "continue":
            if _inside_loop(out, t.start[0] - 1):
                sys.exit(f"refusing: `continue` inside a loop of the body at:\n"
                         + out[t.start[0] - 1])
            edits.append((t.start[0] - 1, t.start[1], t.end[1], "return"))
    for row, a, b, rep in sorted(edits, reverse=True):
        out[row] = out[row][:a] + rep + out[row][b:]
    return "\n".join(out)

existing = set(re.findall(r"^def (\w+)", open(P).read(), re.M))
sections = {}
for start, end, idx, cmds in blocks:
    _, _, j = parse_cond(start)
    body = [l[20:] for l in lines[j + 1:end]]          # 24-space -> 4-space
    name = cmds[0].lower().replace("_req_", "_")
    if name in existing: name = "rpc_" + name
    existing.add(name)
    fn = f"@rpc({idx}, {', '.join(cmds)})\ndef {name}(r):\n" + "\n".join(body)
    fn = rewrite_names(fn)
    sections.setdefault(LIFT[idx], []).append(fn)

gen = []
for sub in LIFT.values():
    gen.append(f"# ---- {sub} " + "-" * (82 - len(sub)) + "\n")
    gen.append("\n\n\n".join(sections[sub]) + "\n\n\n")
anchor = lines.index("def recv_exact(conn, n):")
for start, end, _, _ in sorted(blocks, reverse=True):
    del lines[start:end]
    if start < anchor: anchor -= (end - start)
lines[anchor:anchor] = "".join(gen).split("\n")
open(P, "w").write("\n".join(lines))
print(f"lifted {len(blocks)} branches: " + ", ".join(f"{s}={len(v)}" for s, v in sections.items()))

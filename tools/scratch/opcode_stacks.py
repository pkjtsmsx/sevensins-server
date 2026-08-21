#!/usr/bin/env python3
"""Does a REPEATED identical (op, operand) slot pair encode a stack count?

Gabriel's Pure Flash carries `_action [117,115,112,112]` / `_actID [2016181,0,3001,3001]`
-- status 3001 `Spirit(5)` applied twice -- and its prose reads "grants the caster 2
stacks of Spirit". If that generalises, the compiler must fold repeats into a count rather
than emitting N separate apply_status effects.
"""
import collections
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "server"))
os.chdir(os.path.join(ROOT, "server"))
import battle as bt  # noqa: E402

rows = bt.dd.rows("skill") or {}
WORDS = {"a": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5}

agree = disagree = silent = 0
examples, bad = [], []
for sid, r in rows.items():
    acts, ids = r.get("_action") or [], r.get("_actID") or []
    pairs = [(o, ids[i] if i < len(ids) else 0) for i, o in enumerate(acts) if o]
    reps = collections.Counter(p for p in pairs if p[0] in (112, 113) and p[1])
    dupes = {p: n for p, n in reps.items() if n > 1}
    if not dupes:
        continue
    note = r.get("_note1_en") or ""
    if not note.strip():
        continue
    for (op, aid), n in dupes.items():
        nm = (rows.get(aid) or {}).get("_name_en") or ""
        base = re.sub(r"\s*\(\d+\)\s*$", "", nm).strip()
        if not base:
            continue
        # "N stacks of X" or "N stack of X", digit or word
        m = re.search(r"(\d+|" + "|".join(WORDS) + r")\s+stacks?\s+of\s+" +
                      re.escape(base), note, re.I)
        if not m:
            silent += 1
            continue
        tok = m.group(1).lower()
        said = int(tok) if tok.isdigit() else WORDS[tok]
        if said == n:
            agree += 1
            if len(examples) < 6:
                examples.append((sid, base, n, note[:78]))
        else:
            disagree += 1
            if len(bad) < 6:
                bad.append((sid, base, n, said, note[:78]))

print("skills where a (112/113, status) pair REPEATS and prose names a stack count:")
print(f"   prose count == repeat count : {agree}")
print(f"   disagree                    : {disagree}")
print(f"   prose does not say 'N stacks': {silent}")
print("\nagreeing examples:")
for sid, nm, n, note in examples:
    print(f"   {sid} {nm!r} x{n}")
    print(f"       {note}")
if bad:
    print("\ndisagreements:")
    for sid, nm, n, said, note in bad:
        print(f"   {sid} {nm!r} repeats={n} prose={said}")
        print(f"       {note}")

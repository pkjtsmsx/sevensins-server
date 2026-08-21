#!/usr/bin/env python3
"""op 114 takes either a STATUS row id or one of ten round numbers. Which is which?"""
import collections
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "server"))
os.chdir(os.path.join(ROOT, "server"))
import battle as bt  # noqa: E402

rows = bt.dd.rows("skill") or {}
num, ref = [], []
for sid, r in rows.items():
    acts, ids = r.get("_action") or [], r.get("_actID") or []
    for i, op in enumerate(acts):
        if op != 114:
            continue
        aid = ids[i] if i < len(ids) else 0
        note = (r.get("_note1_en") or "").replace("\n", " ")
        if not note:
            continue
        (num if (aid and aid not in rows) else ref).append((sid, aid, note))

print("=== op 114 with a NUMERIC operand -- sample prose")
seen = set()
for sid, aid, note in num:
    if aid in seen:
        continue
    seen.add(aid)
    print(f"  actID={aid}")
    print(f"     {sid}: {note[:110]}")
    if len(seen) >= 10:
        break

print("\n=== op 114 with a STATUS operand -- sample prose")
seen = set()
for sid, aid, note in ref:
    nm = (rows.get(aid) or {}).get("_name_en") or (rows.get(aid) or {}).get("_name")
    if nm in seen:
        continue
    seen.add(nm)
    print(f"  actID={aid} -> {nm!r}")
    print(f"     {sid}: {note[:110]}")
    if len(seen) >= 6:
        break

# Does the numeric flavour co-occur with particular words?
kw = collections.Counter()
for _, _, note in num:
    low = note.lower()
    for w in ("shield", "recover", "heal", "barrier", "absorb", "fixed", "max hp",
              "damage", "reduce", "immun", "cleanse", "remove", "dispel"):
        if w in low:
            kw[w] += 1
print("\nnumeric-operand prose keyword counts:", kw.most_common(10))

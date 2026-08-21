#!/usr/bin/env python3
"""112 and 113 both take a STATUS operand 100% of the time. What separates them?"""
import collections
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "server"))
os.chdir(os.path.join(ROOT, "server"))
import battle as bt  # noqa: E402

rows = bt.dd.rows("skill") or {}

def blocks(op):
    """Which status category blocks does this opcode point at?"""
    c = collections.Counter()
    for sid, r in rows.items():
        acts, ids = r.get("_action") or [], r.get("_actID") or []
        for i, o in enumerate(acts):
            if o != op:
                continue
            aid = ids[i] if i < len(ids) else 0
            if aid and aid in rows:
                c[aid // 1000 * 1000 if aid < 100000 else "high"] += 1
    return c

for op in (112, 113):
    print(f"op {op} -> status blocks: {dict(blocks(op).most_common(9))}")

# Do skills ever carry BOTH, and does the prose distinguish?
print("\nskills using op 113, sample prose:")
n = 0
for sid, r in rows.items():
    acts = r.get("_action") or []
    if 113 not in acts:
        continue
    note = (r.get("_note1_en") or "").replace("\n", " ")
    if not note:
        continue
    ids = r.get("_actID") or []
    ops = [(o, ids[i] if i < len(ids) else 0) for i, o in enumerate(acts) if o]
    tgt = next((rows.get(a) for o, a in ops if o == 113 and a in rows), None)
    nm = (tgt or {}).get("_name_en") or (tgt or {}).get("_name")
    print(f"  {sid} ops={ops}")
    print(f"      status={nm!r}")
    print(f"      {note[:104]}")
    n += 1
    if n >= 6:
        break

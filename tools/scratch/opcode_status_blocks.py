#!/usr/bin/env python3
"""Are op 114's round-number operands the THOUSANDS BLOCK of the status id space?

op 114 removes. Its operand is either a specific STATUS row id, or one of ten round
numbers (1000/2000/.../7000, plus 600/4998/4999). The prose pairs those numbers with
categories -- 4000 "Shield", 5000 "DoT", 2000 "removable buffs" -- so the hypothesis is
that a status row id's leading digits identify its category.
"""
import collections
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "server"))
os.chdir(os.path.join(ROOT, "server"))
import battle as bt  # noqa: E402

rows = bt.dd.rows("skill") or {}
status = {sid: r for sid, r in rows.items() if r.get("_type") == 6}
print("STATUS rows:", len(status))

blocks = collections.defaultdict(list)
for sid, r in status.items():
    if sid < 100000:                       # the low-numbered status space
        blocks[sid // 1000 * 1000].append(sid)

print(f"\n{'block':>7} {'n':>5}  sample names")
for b in sorted(blocks):
    names = []
    for sid in sorted(blocks[b])[:60]:
        nm = status[sid].get("_name_en") or status[sid].get("_name") or ""
        if nm and nm not in names:
            names.append(nm)
        if len(names) >= 5:
            break
    print(f"{b:>7} {len(blocks[b]):>5}  {', '.join(names)[:96]}")

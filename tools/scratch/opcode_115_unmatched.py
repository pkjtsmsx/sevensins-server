#!/usr/bin/env python3
"""Is op 115 a trigger or an EFFECT, and why do 2,461 glossary statuses go unnamed?"""
import collections
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "server"))
os.chdir(os.path.join(ROOT, "server"))
import battle as bt  # noqa: E402

rows = bt.dd.rows("skill") or {}

def ops_of(r):
    acts, ids = r.get("_action") or [], r.get("_actID") or []
    return [(o, ids[i] if i < len(ids) else 0) for i, o in enumerate(acts) if o]

print("=== op 115: prose of skills whose ONLY opcodes are 112/115 (so 115 is isolated)")
n = 0
for sid, r in rows.items():
    o = ops_of(r)
    if not o or not any(x == 115 for x, _ in o):
        continue
    note = (r.get("_note1_en") or "").replace("\n", " ")
    if not note or "*" in note:
        continue
    print(f"  {sid} ops={o}")
    print(f"      {note[:104]}")
    n += 1
    if n >= 6:
        break

print("\n=== glossary statuses NOT named by a 112/113 opcode -- what are they?")
missing = collections.Counter()
for sid, r in rows.items():
    if r.get("_type") not in (1, 2, 3):
        continue
    note = r.get("_note1_en") or ""
    gl = re.findall(r"^\s*\*\s*([^:]{1,40}):", note, re.M)
    if not gl:
        continue
    named = set()
    for op, aid in ops_of(r):
        if op in (112, 113) and aid in rows:
            nm = rows[aid].get("_name_en") or rows[aid].get("_name") or ""
            named.add(nm.strip().lower())
    for g in gl:
        if g.strip().lower() not in named:
            missing[g.strip()] += 1
print("  top unmatched status names:", missing.most_common(12))

# Are those names present ANYWHERE as a status row?
allnames = {}
for sid, r in rows.items():
    if r.get("_type") == 6:
        nm = (r.get("_name_en") or r.get("_name") or "").strip().lower()
        if nm:
            allnames.setdefault(nm, sid)
exists = sum(c for g, c in missing.items() if g.lower() in allnames)
print(f"  of {sum(missing.values())} unmatched, name DOES exist as a status row: {exists}")

#!/usr/bin/env python3
"""What is SkillType 7, and what are op 114's non-skill operands?

SkillType (from the client enum) defines 1,2,3,4,5,6,10,101,102,103 -- there is no 7.
Yet op 117 points at a type-7 row 1,257 times, so type 7 is an undocumented row kind.
"""
import collections
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "server"))
os.chdir(os.path.join(ROOT, "server"))
import battle as bt  # noqa: E402

rows = bt.dd.rows("skill") or {}

t7 = [(sid, r) for sid, r in rows.items() if r.get("_type") == 7]
print(f"=== SkillType 7 rows: {len(t7)}")
print("   their own _action opcodes:",
      dict(collections.Counter(v for _, r in t7 for v in (r.get("_action") or []) if v).most_common(8)))
print("   target values:",
      dict(collections.Counter(r.get("_target") for _, r in t7).most_common(6)))
print("   hit values   :",
      dict(collections.Counter(r.get("_count") for _, r in t7).most_common(6)))
print("   have actName :",
      sum(1 for _, r in t7 if (r.get("_actName") or "").strip()), "of", len(t7))
print("   samples:")
for sid, r in t7[:6]:
    note = (r.get("_note1_en") or r.get("_note1") or "").replace("\n", " ")[:74]
    print(f"     {sid} {str(r.get('_name_en') or r.get('_name'))[:22]:<22} "
          f"tgt={r.get('_target')} hit={r.get('_count')} act={r.get('_actName')!r}")
    print(f"         {note}")

print("\n=== op 114 operands that are NOT skill rows")
bad = []
for sid, r in rows.items():
    acts, ids = r.get("_action") or [], r.get("_actID") or []
    for i, op in enumerate(acts):
        if op != 114:
            continue
        aid = ids[i] if i < len(ids) else 0
        if aid and aid not in rows:
            bad.append(aid)
c = collections.Counter(bad)
print("   distinct values:", len(c))
print("   most common    :", c.most_common(10))
lo, hi = min(bad), max(bad)
print(f"   range          : {lo} .. {hi}")
# do they look like statusIDs rather than skill ids?
st = bt.dd.rows("status") or {}
instatus = sum(1 for v in set(bad) if v in st or str(v) in st)
print(f"   of {len(c)} distinct, how many are rows in the STATUS form: {instatus}")

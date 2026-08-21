#!/usr/bin/env python3
"""What does each `_action` opcode's paired `_actID` operand point at?

`DesignSkillRow._action[]` / `_actID[]` are 5-slot parallel arrays the CLIENT never reads
(no getter exists), almost certainly the original server's effect script. This asks the
one question that unlocks the rest: is `_actID` a reference into the skill table, and if
so what kind of row does each opcode point at?
"""
import collections
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "server"))
os.chdir(os.path.join(ROOT, "server"))
import battle as bt  # noqa: E402

TYPE = {1: "COM_ATTACK", 2: "SKILL", 3: "SP_SKILL", 4: "PASSIVE",
        5: "SUPPORT", 6: "STATUS", 7: "type7", 10: "GOD_ITEM",
        101: "COL1", 102: "COL2", 103: "COL3"}

rows = bt.dd.rows("skill") or {}
stat = collections.defaultdict(collections.Counter)
tot = collections.Counter()
slot = collections.defaultdict(collections.Counter)

for sid, r in rows.items():
    acts = r.get("_action") or []
    ids = r.get("_actID") or []
    for i, op in enumerate(acts):
        if not op:
            continue
        tot[op] += 1
        slot[op][i] += 1
        aid = ids[i] if i < len(ids) else 0
        if not aid:
            stat[op]["<no operand>"] += 1
            continue
        tgt = rows.get(aid)
        if tgt is None:
            stat[op]["NOT-A-SKILL"] += 1
        else:
            stat[op][TYPE.get(tgt.get("_type"), str(tgt.get("_type")))] += 1

print(f"{'op':>4} {'n':>6}  slots      operand resolves to...")
for op in sorted(tot, key=lambda o: -tot[o]):
    slots = ",".join(str(k) for k, _ in sorted(slot[op].items()))
    parts = ", ".join(f"{k}={v}" for k, v in stat[op].most_common(4))
    print(f"{op:>4} {tot[op]:>6}  [{slots:<9}] {parts}")

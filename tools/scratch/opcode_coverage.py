#!/usr/bin/env python3
"""How much of the effect surface do the opcodes cover, and is DAMAGE among it?

Decides how the engine should be built: if a pure-damage skill carries no opcodes, then
the script covers statuses and follow-ups only, and damage stays a prose-derived number.
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
ATTACK_TYPES = {1, 2, 3}          # COM_ATTACK / SKILL / SP_SKILL

def ops_of(r):
    acts, ids = r.get("_action") or [], r.get("_actID") or []
    return [(o, ids[i] if i < len(ids) else 0) for i, o in enumerate(acts) if o]

# 1. Coverage: how many rows carry any opcode at all?
have = sum(1 for r in rows.values() if ops_of(r))
print(f"skill rows          : {len(rows)}")
print(f"  carrying opcodes  : {have} ({100.0*have/len(rows):.1f}%)")

# 2. Pure-damage skills: prose deals damage and mentions no status glossary.
pure, pure_ops = [], collections.Counter()
for sid, r in rows.items():
    if r.get("_type") not in ATTACK_TYPES:
        continue
    note = r.get("_note1_en") or ""
    if not note.strip():
        continue
    if "*" in note:                       # has a status glossary -> not pure damage
        continue
    if not re.search(r"\d+% (ATK|DEF|Max HP)", note, re.I):
        continue
    pure.append((sid, r, note))
    o = ops_of(r)
    pure_ops[len(o)] += 1
print(f"\npure-damage attack skills (prose deals %, no status glossary): {len(pure)}")
print("  opcode-count distribution:", dict(sorted(pure_ops.items())))
print("  samples:")
for sid, r, note in pure[:5]:
    print(f"    {sid} ops={ops_of(r)}  {note.strip()[:66]!r}")

# 3. For ATTACK skills that DO apply statuses, do the opcodes name them?
named = missing = 0
for sid, r in rows.items():
    if r.get("_type") not in ATTACK_TYPES:
        continue
    note = r.get("_note1_en") or ""
    glossary = re.findall(r"^\s*\*\s*([^:]{1,40}):", note, re.M)
    if not glossary:
        continue
    statuses = set()
    for op, aid in ops_of(r):
        if op in (112, 113) and aid in rows:
            nm = rows[aid].get("_name_en") or rows[aid].get("_name")
            if nm:
                statuses.add(nm.strip().lower())
    for g in glossary:
        if g.strip().lower() in statuses:
            named += 1
        else:
            missing += 1
print(f"\nstatuses named in an attack skill's glossary : {named + missing}")
print(f"  ALSO named by a 112/113 opcode            : {named}")
print(f"  not matched                                : {missing}")

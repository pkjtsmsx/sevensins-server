#!/usr/bin/env python3
"""Cross-check battle_data/skill_effects.json against independent witnesses.

The prose parser is unavoidable for MAGNITUDES -- the shipped pack does not carry them
(levels 1..4 of a skill share byte-identical `_action`/`_actID`/`_target`/`_count` while
the prose goes 210% -> 300%, so the scaling was server-side like the drop and gacha
tables). But prose is a lossy source: the game's own English has typos ("Delas 210% ATK
as damage" hid the damage clause on 33 skills) and phrasings the matchers miss.

So don't trust it alone. Three witnesses disagree loudly when the parse is wrong:

  STATUS   `_actID` names type-6 status rows outright -- a machine-readable list of what
           the skill applies, independent of any prose. Anything declared there and
           absent from the parse is suspect (with the caveat that a skill may REMOVE a
           status it names, or the name may be an alias).
  DAMAGE   prose promising "N% ATK as damage" with no damage op parsed is a straight
           miss, which is exactly how the Delas typo was found.
  SCALING  a skill group's levels almost always step by a constant (82% of groups do),
           so a level that breaks its group's progression, or is missing a value its
           siblings have, is either a typo or a parse gap -- and the siblings say what
           the value should have been.

Usage:  tools/check_skill_parse.py [--limit N] [--json OUT]
"""
import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))
import battle as bt                                            # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "tools"))
from skillparse.text import clean, strip_status_defs           # noqa: E402

EFFECTS = os.path.join(ROOT, "server", "battle_data", "skill_effects.json")
DAMAGE_PROSE = re.compile(r"(\d+)%\s*ATK\s+as\s+damage", re.I)
STATUS_TYPE = 6
# `_actID` points at a specific LEVEL of a status skill, whose name carries the level in
# parentheses ("Fatigue(5)"), while the catalog and the prose use the bare name. Compare
# on the bare, case-folded name or four figures of the count are just that suffix.
LEVEL_SUFFIX = re.compile(r"\s*\(\d+\)\s*$")


def norm_status(name):
    return LEVEL_SUFFIX.sub("", (name or "").strip()).casefold()


def load():
    with open(EFFECTS, encoding="utf-8") as fh:
        return json.load(fh)


def parsed_ops(effects, sid):
    entry = effects.get(str(sid)) or {}
    return [f for block in entry.get("blocks", []) for f in block["effects"]]


def declared_statuses(row, skills):
    """Status names the row itself names through `_actID` -> type-6 skill rows."""
    out = set()
    for aid in row.get("_actID") or []:
        ref = skills.get(aid) or {}
        if ref.get("_type") == STATUS_TYPE:
            name = (ref.get("_name_en") or "").strip()
            if name:
                out.add(name)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=8, help="examples per category")
    ap.add_argument("--json", help="write the full finding list here")
    args = ap.parse_args()

    skills = bt.dd.rows("skill")
    effects = load()
    findings = defaultdict(list)

    # -- witnesses 1 and 2, per skill -------------------------------------
    for sid, row in skills.items():
        if row.get("_type") == STATUS_TYPE:
            continue
        # Use the SAME prose the parser sees: damage inside a "* Name: ..." gloss
        # belongs to that STATUS (a DoT tick or a conditional bonus the catalog models),
        # not to the skill, and flagging it just buries the real gaps.
        prose, _defs = strip_status_defs(clean(row.get("_note1_en") or ""))
        ops = parsed_ops(effects, sid)
        kinds = {f["op"] for f in ops}

        want = DAMAGE_PROSE.search(prose)
        if want and "damage" not in kinds:
            findings["damage_missing"].append(
                {"skill": int(sid), "name": row.get("_name_en"),
                 "pct": int(want.group(1)), "text": prose[:90]})

        declared = declared_statuses(row, skills)
        if declared:
            # A skill that REMOVES or immunises against a status legitimately names it
            # without applying it, so count every op that references one.
            got = {norm_status(f.get("status")) for f in ops if f.get("status")}
            for f in ops:
                got |= {norm_status(n) for n in (f.get("statuses") or [])}
            for miss in sorted(d for d in declared if norm_status(d) not in got):
                findings["status_missing"].append(
                    {"skill": int(sid), "name": row.get("_name_en"), "status": miss})

    # -- witness 3, per group ---------------------------------------------
    groups = defaultdict(dict)
    for sid, row in skills.items():
        if row.get("_type") == STATUS_TYPE or not row.get("_lv"):
            continue
        m = DAMAGE_PROSE.search(row.get("_note1_en") or "")
        if m:
            groups[row.get("_group")][int(row["_lv"])] = (int(sid), int(m.group(1)))
    for gid, lvs in groups.items():
        if len(lvs) < 3:
            continue
        ks = sorted(lvs)
        vals = [lvs[k][1] for k in ks]
        steps = {vals[i + 1] - vals[i] for i in range(len(vals) - 1)}
        if len(steps) == 1:
            step = steps.pop()
            # every level present? a hole means one level's prose says something else
            span = list(range(ks[0], ks[-1] + 1))
            for lv in span:
                if lv not in lvs:
                    findings["scaling_hole"].append(
                        {"group": gid, "missing_lv": lv,
                         "expected_pct": vals[0] + step * (lv - ks[0])})
        elif len(steps) > 2:
            findings["scaling_irregular"].append(
                {"group": gid, "levels": ks, "pcts": vals})

    print(f"skills checked: {sum(1 for r in skills.values() if r.get('_type') != STATUS_TYPE)}")
    for cat in ("damage_missing", "status_missing", "scaling_hole", "scaling_irregular"):
        rows = findings[cat]
        print(f"\n== {cat}: {len(rows)}")
        for r in rows[:args.limit]:
            print("   ", json.dumps(r, ensure_ascii=False)[:150])
    if findings["status_missing"]:
        top = Counter(r["status"] for r in findings["status_missing"])
        print("\n== most-missed statuses (alias/parse patterns cluster here)")
        for name, n in top.most_common(15):
            print(f"    {n:>5}  {name}")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(findings, fh, ensure_ascii=False, indent=1)
        print(f"\nwrote {args.json}")


if __name__ == "__main__":
    main()

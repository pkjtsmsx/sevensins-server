#!/usr/bin/env python3
"""Which sentences of a skill's prose produced no effect at all. -> a ranked gap list.

**Why this exists.** Every skill bug found so far was found the same way: somebody
played, noticed a number was wrong, and a human traced one clause back to a compiler
that had silently dropped it. `Special Sanction` lost 35% of the target's max HP -- the
Guild Weekly boss's entire damage output -- and `unmodelled` was null, so nothing in the
tree knew. With ~14,000 skills that does not scale, and the alternative is not reading
them all: it is asking the corpus which sentences we cannot account for, and fixing the
biggest shapes first.

**How.** For each skill, split the CHINESE note (the original; the English is a
translation) into fragments the way the compiler does, work out what each fragment
CLAIMS from its own verbs, and check whether the compiled spec contains any effect of
that kind. A fragment claiming a heal against a spec with no heal effect is unaccounted.

**What it is not.** This is a smoke detector, not a proof. It over-reports -- one effect
can satisfy two fragments, and a fragment can be covered by an effect the compiler
attributed to a neighbour -- so a single line here is weak evidence. The RANKING is the
product: 150 skills sharing one unaccounted shape is a missing rule, and that is a claim
worth acting on. Shapes are normalised (digits to N, quoted names to S) so a family
collects into one row instead of scattering across 150.

    python3 tools/clause_coverage.py             # ranked families
    python3 tools/clause_coverage.py --limit 40  # more of them
    python3 tools/clause_coverage.py --shape 傷害  # every example of one family
"""
import argparse
import collections
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "server"))
sys.path.insert(0, HERE)

import design_data as dd                     # noqa: E402
from engine import specs                     # noqa: E402

# What a fragment CLAIMS, by its own verbs, and which compiled ops would satisfy it.
# Deliberately narrow: a marker that fires on half the corpus tells you nothing, and a
# false "accounted" is worse than a missing row because it hides a real gap.
CLAIMS = [
    ("heal",      re.compile(r"(恢復|回復)[^，。]{0,12}(體力|血量|生命)"),
                  {"heal", "attack_rider", "revive"}),
    # 復活道具 is the retry-with-an-item UI line, and 禁止復活 is a status NAME
    # ("Revival Ban") rather than a revive. Both were the loudest false families.
    ("revive",    re.compile(r"(?<!禁止)復活(?!道具)"), {"revive"}),
    # 行動值 is also how the pack SELECTS a target -- 對敵方行動值最高的敵人 -- so a bare
    # mention is not a claim that the gauge changes. Require a direction or a magnitude.
    ("gauge",     re.compile(r"行動值\s*[+\-＋－]|行動值[^，。]{0,6}(增加|減少|提升|降低|\d+\s*[%％])"),
                  {"modify_gauge"}),
    ("cooldown",  re.compile(r"冷卻"), {"modify_cd"}),
    # NOT a cleanse when it is negated. 不可解除 / 不可清除 / 解除不可 are the
    # unremovable MARKER on a status the skill grants, and they were the single biggest
    # "gap" in the first run at 309 fragments -- all of them noise.
    ("cleanse",   re.compile(r"(?<!不可)(?<!無法)(解除|清除|消除)(?!不可)"),
                  {"remove_status"}),
    ("shield",    re.compile(r"(護盾|吸收[^，。]{0,6}傷害)"),
                  {"apply_status", "attack_rider"}),
    # Damage only when a PERCENTAGE is attached: "造成傷害時" is a trigger, not an amount.
    ("damage",    re.compile(r"\d+\s*[%％][^，。]{0,10}傷害|傷害[^，。]{0,6}\d+\s*[%％]"),
                  {"damage", "attack_rider"}),
    ("status",    re.compile(r"(附加|賦予|獲得|使自身|使目標)"), {"apply_status"}),
]

# Fragments that are glossary, UI copy or menu flavour rather than battle mechanics.
# `解鎖技能` is the skill-unlock blurb on the cast sheet; it describes a progression
# reward, not something that happens in a fight.
SKIP = re.compile(r"^\s*[※*]|^\s*$|解鎖技能|復活道具")

_NORM_NUM = re.compile(r"\d+(?:\.\d+)?")
_NORM_NAME = re.compile(r"[「『][^」』]*[」』]")
_SPLIT = re.compile(r"[，。；、\n]")


def shape(fragment):
    """A fragment with its numbers and names blanked, so a family collects into one row."""
    s = _NORM_NAME.sub("「S」", fragment)
    s = _NORM_NUM.sub("N", s)
    return " ".join(s.split())[:90]


def audit():
    rows = dd.rows("skill") or {}
    items = rows.items() if hasattr(rows, "items") else enumerate(rows)
    families = collections.defaultdict(lambda: {"n": 0, "claims": collections.Counter(),
                                                "eg": []})
    seen_skills, flagged_skills = 0, set()
    for _k, r in items:
        sid = int(r.get("_id") or 0)
        spec = specs.skill(sid) or {}
        if not spec or spec.get("type") == "status":
            continue
        note = r.get("_note1") or ""
        if not note:
            continue
        seen_skills += 1
        have = {e.get("op") for e in spec.get("effects") or []}
        for line in note.split("\n"):
            if SKIP.match(line):
                continue
            for frag in _SPLIT.split(line):
                if not frag.strip():
                    continue
                for name, pat, ops in CLAIMS:
                    if not pat.search(frag):
                        continue
                    if have & ops:
                        continue
                    key = shape(frag)
                    fam = families[key]
                    fam["n"] += 1
                    fam["claims"][name] += 1
                    flagged_skills.add(sid)
                    if len(fam["eg"]) < 3:
                        fam["eg"].append((sid, r.get("_name_en") or "", frag.strip()[:70]))
                    break
    return families, seen_skills, flagged_skills


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--shape", help="show every example whose shape contains this text")
    args = ap.parse_args()

    families, seen, flagged = audit()
    total = sum(f["n"] for f in families.values())
    print(f"{seen} skills with Chinese prose; {len(flagged)} have at least one "
          f"unaccounted fragment")
    print(f"{total} unaccounted fragments across {len(families)} distinct shapes\n")

    ranked = sorted(families.items(), key=lambda kv: -kv[1]["n"])
    if args.shape:
        for key, fam in ranked:
            if args.shape in key:
                print(f"[{fam['n']}] {key}")
                for sid, name, frag in fam["eg"]:
                    print(f"     {sid} {name}: {frag}")
        return 0

    print(f"{'count':>6}  {'claims':<10} shape")
    print("-" * 100)
    for key, fam in ranked[:args.limit]:
        claims = ",".join(k for k, _ in fam["claims"].most_common(2))
        print(f"{fam['n']:>6}  {claims:<10} {key}")
        sid, name, frag = fam["eg"][0]
        print(f"{'':>6}  {'':<10}   e.g. {sid} {name}: {frag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

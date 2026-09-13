#!/usr/bin/env python3
"""A review worksheet: one skill group's prose beside what the compiler made of it.

**Why this exists.** 14,410 skill rows collapse to 2,684 groups, 2,246 of them
cast-facing, and until now a human had read about 21 of them. There is no ORACLE: the
only way to answer "does this skill do what its prose says" has been to play it on a
phone, which is how every defect this project has found was actually found.

This prints the two things a verdict needs side by side -- the original Chinese and the
compiled effects the engine will execute -- so a group can be judged in one screen
rather than by cross-referencing three files.

**The prose stays out of git.** This worksheet quotes `_note1` verbatim, so its output is
untracked for the same reason `skill_effects.json` and `status_catalog.json` are
(CLAUDE.md section 10). The VERDICTS are ours, not the game's, and they are tracked --
they live in `docs/skill_verdicts.json` keyed by group id, carrying a judgement and a
short reason but never the pack's text.

    python3 tools/skill_review.py --cast MICHAEL BELIAL      # every group these cast
    python3 tools/skill_review.py --group 1103111            # one ladder
    python3 tools/skill_review.py --cast PUNICA --json out.json
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(os.path.dirname(HERE), "server")
sys.path.insert(0, SERVER)
sys.path.insert(0, HERE)

import design_data as dd                                        # noqa: E402
from engine import specs                                        # noqa: E402

TYPE = {1: "com_attack", 2: "skill", 3: "sp_skill", 4: "passive", 5: "support",
        6: "status", 7: "sub_skill", 10: "god_item"}

# What actually matters about an effect when judging it against prose. `slot`, `source`
# and the other provenance keys are noise here -- they say where the compiler got the
# value, not what the fight will do with it.
SHOWN = ("op", "kind", "percent", "coefficient", "basis", "target", "recipient",
         "count", "turns", "skill", "chance_pct", "trigger", "requires", "category")


def _owners():
    """-> {group id: [cast name, ...]} so a verdict can be prioritised by who casts it."""
    out = {}
    for c in (dd.rows("char") or {}).values():
        name = (c.get("_name_en") or c.get("_name") or "").strip()
        title = (c.get("_title_en") or "").strip()
        label = f"{name} ({title})" if title else name
        for s in (c.get("_skills") or []):
            if s:
                out.setdefault(s // 10, [])
                if label not in out[s // 10]:
                    out[s // 10].append(label)
    return out


def _effect_line(e):
    parts = []
    for k in SHOWN:
        if k not in e or e[k] in (None, False, {}, []):
            continue
        v = e[k]
        if k == "requires":
            v = {q: w for q, w in v.items() if w not in (None, False)}
        parts.append(f"{k}={v}")
    st = (e.get("status") or {}).get("name")
    if st:
        parts.insert(1, f"status={st!r}")
    return "  ".join(parts)


def review(groups):
    """-> [record] for each group, top rank first (the one a maxed cast actually uses)."""
    rows = dd.rows("skill") or {}
    owners = _owners()
    out = []
    for g in groups:
        ranks = sorted(s for s in rows if s // 10 == g)
        if not ranks:
            continue
        top = ranks[-1]
        r = rows[top]
        spec = specs.skill(top) or {}
        out.append({
            "group": g,
            "top_rank": top,
            "ranks": len(ranks),
            "name": r.get("_name_en") or r.get("_name"),
            "type": TYPE.get(r.get("_type"), r.get("_type")),
            "cast": owners.get(g, []),
            "note_zh": (r.get("_note1") or "").strip(),
            "note_en": (r.get("_note1_en") or "").strip(),
            "effects": [_effect_line(e) for e in (spec.get("effects") or [])],
            "unknown": spec.get("unknown") or [],
        })
    return out


VERDICTS = os.path.join(os.path.dirname(HERE), "docs", "skill_verdicts.json")


def summary():
    """Tally the verdict ledger. -> 0.

    The per-group records are what a reader checks; the TALLY is what decides the next
    piece of work. One skill reading `missing_gate` is a bug; 12 of 20 reading it is the
    argument for fixing the compiler's conditionality pass before anything else.
    """
    import collections
    with open(VERDICTS, encoding="utf-8") as fh:
        v = json.load(fh)["verdicts"]
    status = collections.Counter(r["status"] for r in v.values())
    classes = collections.Counter(c for r in v.values() for c in r["classes"])
    casts = collections.Counter(r.get("cast", "?") for r in v.values())
    total = len(v)
    print(f"{total} group(s) reviewed of 2,246 cast-facing "
          f"({total / 2246:.1%})\n")
    print("status:")
    for k in ("ok", "partial", "broken", "suspect"):
        if status[k]:
            print(f"   {k:<9}{status[k]:>4}  {status[k] / total:>5.0%}")
    print("\ndefect classes (a group can carry several):")
    for k, n in classes.most_common():
        print(f"   {k:<22}{n:>4}  in {n / total:>5.0%} of reviewed groups")
    print("\nby cast:")
    for k, n in casts.most_common():
        bad = sum(1 for r in v.values()
                  if r.get("cast") == k and r["status"] in ("broken", "partial"))
        print(f"   {k:<10}{n:>3} reviewed, {bad} not clean")
    return 0


def groups_for_casts(names):
    want = {n.upper() for n in names}
    out = []
    for c in (dd.rows("char") or {}).values():
        if (c.get("_name_en") or "").strip().upper() in want:
            for s in (c.get("_skills") or []):
                if s and s // 10 not in out:
                    out.append(s // 10)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cast", nargs="+", help="cast names, e.g. MICHAEL BELIAL")
    ap.add_argument("--group", nargs="+", type=int, help="skill group ids")
    ap.add_argument("--json", help="write the records here instead of printing")
    ap.add_argument("--summary", action="store_true",
                    help="tally docs/skill_verdicts.json instead of printing records")
    args = ap.parse_args()

    if args.summary:
        return summary()

    groups = list(args.group or [])
    if args.cast:
        groups += [g for g in groups_for_casts(args.cast) if g not in groups]
    if not groups:
        ap.error("give --cast or --group")

    recs = review(groups)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(recs, fh, ensure_ascii=False, indent=1)
        print(f"wrote {args.json}: {len(recs)} group(s)")
        return 0
    for rec in recs:
        print("=" * 78)
        print(f"group {rec['group']}  {rec['name']}  [{rec['type']}, "
              f"{rec['ranks']} ranks, showing {rec['top_rank']}]")
        if rec["cast"]:
            print(f"  cast: {', '.join(rec['cast'][:4])}"
                  + (f" (+{len(rec['cast']) - 4})" if len(rec["cast"]) > 4 else ""))
        print(f"  zh: {rec['note_zh']}")
        if rec["note_en"]:
            print(f"  en: {rec['note_en']}")
        print("  compiled:")
        for line in rec["effects"] or ["    (nothing)"]:
            print(f"    - {line}")
        if rec["unknown"]:
            print(f"  UNDECODED OPCODES: {rec['unknown']}")
    print(f"\n{len(recs)} group(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

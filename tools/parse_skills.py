#!/usr/bin/env python3
"""Parse skill descriptions into ordered effect lists for the battle engine (CLI).

Step 2 of the effect-system revamp. The battle is server-authoritative and the skill
mechanics live ONLY in the description text (the client never reads `_action`/`_actID`) --
so this turns each skill's prose into a structured, ordered list of effect primitives the
runtime can execute. The parser itself is the `skillparse` package; this file is just the
driver that loads the design data + status catalog and reports coverage.

Usage:
  tools/parse_skills.py                  # validate on the starter casts (verbose)
  tools/parse_skills.py --all            # parse every skill, report coverage
  tools/parse_skills.py --all --write    # ... and write battle_data/skill_effects.json
"""
import json
import os
import sys

from skillparse import clean, parse_skill

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "server")
CATALOG = os.path.join(SERVER, "battle_data", "status_catalog.json")


def main():
    sys.path.insert(0, SERVER)
    import design_data as dd                                         # noqa: E402
    catalog = json.load(open(CATALOG, encoding="utf-8"))
    sk = dd.rows("skill")

    if "--all" not in sys.argv:
        # validation set: the starter casts
        for cid in (10001, 10011):
            crow = dd.row("char", cid) or {}
            print(f"===== char {cid} =====")
            for sid in (crow.get("_skills") or []):
                r = sk.get(sid)
                if not r:
                    continue
                res = parse_skill(r, catalog)
                print(f"[{sid}] type={res['type']} target={res['target']} hits={res['hits']}")
                for b in res["blocks"]:
                    if b["name"]:
                        print(f"   ({b['name']})")
                    for e in b["effects"]:
                        print("     ", json.dumps(e, ensure_ascii=False))
                for u in res["unparsed"]:
                    print(f"   UNPARSED: {u}")
        return

    total = clauses = parsed_clauses = complete = 0
    out = {}
    for sid, r in sk.items():
        n1 = clean(r.get("_note1_en"))
        if not n1 or n1 in ("無", "-", "Unuseful"):
            continue
        total += 1
        res = parse_skill(r, catalog)
        got = sum(len(b["effects"]) for b in res["blocks"])
        clauses += got + len(res["unparsed"])
        parsed_clauses += got
        # "complete" = every clause turned into an effect (nothing left unparsed). The
        # engine should trust these; the rest need parser work before they are safe.
        res["complete"] = not res["unparsed"] and got > 0
        if res["complete"]:
            complete += 1
        out[str(sid)] = res
    print(f"skills with English text: {total}")
    print(f"clauses: {clauses}, parsed into effects: {parsed_clauses} "
          f"({100 * parsed_clauses // max(clauses, 1)}%)")
    print(f"fully-understood skills (no unparsed clauses): {complete} "
          f"({100 * complete // max(total, 1)}%)")

    if "--write" in sys.argv:
        path = os.path.join(SERVER, "battle_data", "skill_effects.json")
        _report_regressions(path, out)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1, sort_keys=True)
        print(f"wrote {path}")


def _effect_set(entry):
    return {(f.get("op"), f.get("status"), f.get("pct_atk"), f.get("times"))
            for block in (entry or {}).get("blocks", []) for f in block["effects"]}


def _report_regressions(path, fresh):
    """Diff a regeneration against the file it is about to replace.

    **The parse is re-derived from scratch every run, deliberately** -- freezing the
    skills that look "complete" would create a second source of truth that drifts, and
    "complete" does not mean correct: Luminous Vortex parsed three clean effects and
    still dealt no damage for months because a typo hid its damage clause. Freezing it
    would have locked that in, and blocked the 83 + 553 skills later fixes improved.

    What a freeze is really protecting against is a change quietly LOSING effects, so
    check for that directly instead: anything a skill had before and does not have now
    is printed loudly. Gains are summarised; losses are named.
    """
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        old = json.load(fh)
    gained = lost = 0
    losses = []
    for sid, entry in fresh.items():
        before, after = _effect_set(old.get(sid)), _effect_set(entry)
        if after - before:
            gained += 1
        if before - after:
            lost += 1
            losses.append((sid, sorted(before - after)))
    for sid in old.keys() - fresh.keys():
        lost += 1
        losses.append((sid, ["(skill dropped entirely)"]))
    print(f"vs the file on disk: {gained} skill(s) gained effects, {lost} LOST")
    for sid, gone in losses[:20]:
        print(f"   REGRESSION {sid}: lost {gone}")
    if len(losses) > 20:
        print(f"   ... and {len(losses) - 20} more")


if __name__ == "__main__":
    main()

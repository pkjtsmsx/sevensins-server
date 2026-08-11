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
        with open(path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1, sort_keys=True)
        print(f"wrote {path}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Old over-bump damage must be cleaned out of a save, not just stopped.

`bump_quest_counter` once advanced EVERY quest sharing a `_case_id`, including rows
belonging to systems we do not run (event/OFA/battle-pass -- `_type` 4/6/7/8/9). Three
gacha pulls armed forty-odd quests across twenty chains at once. The bump has skipped
those types for a while, but nothing removed the entries already written, and the
client renders them: the goal list comes out in the wrong order and shows steps from
chains the player never started.

Measured on a real device save: 40 orphan entries against 1 real one.

    python3 test_quest_orphans.py

Uses its own SEVENSINS_ACCOUNTS tempdir so it never touches real save data.
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

_TMP = tempfile.mkdtemp(prefix="sevensins-orphan-test-")
os.environ["SEVENSINS_ACCOUNTS"] = _TMP        # before player_state imports

import battle as bt           # noqa: E402
import player_state as ps     # noqa: E402
from player_state.core import (                # noqa: E402
    QUEST_CASE_GACHA,
    UNSUPPORTED_QUEST_TYPES,
    _default,
    _seed_roster,
    path_for,
)

_fail = 0


def check(name, cond, detail=""):
    global _fail
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        _fail += 1


def main():
    rows = bt.dd.rows("quest") or {}
    orphan_ids = [qid for qid, r in sorted(rows.items())
                  if r.get("_type") in UNSUPPORTED_QUEST_TYPES
                  and r.get("_case_type") == 2][:40]
    legit_ids = [qid for qid, r in sorted(rows.items())
                 if r.get("_type") not in UNSUPPORTED_QUEST_TYPES
                 and r.get("_case_type") == 2][:3]
    check("the pack still has unsupported-type SP quests to test with",
          len(orphan_ids) >= 10, str(len(orphan_ids)))
    check("and supported ones", len(legit_ids) == 3, str(len(legit_ids)))

    # ---- a save carrying old damage is repaired on load --------------------
    st = _default(1000001)
    _seed_roster(st)
    for qid in orphan_ids:
        st["sp_quests"][str(qid)] = {"id": qid, "a_time": 0, "cnt": 3, "status": 0}
    for qid in legit_ids:
        st["sp_quests"][str(qid)] = {"id": qid, "a_time": 0, "cnt": 1, "status": 1}
    # A quest id that no longer exists in the pack can never be shown or claimed.
    st["sp_quests"]["99999999"] = {"id": 99999999, "a_time": 0, "cnt": 1, "status": 0}
    st["quests"]["31001"] = 1
    st["quest_db"]["13_0"] = 9
    ps.save(st)

    reloaded = ps.load(1000001)
    kept = sorted(reloaded["sp_quests"], key=int)
    check("every orphan is gone",
          not [k for k in kept if int(k) in orphan_ids], str(kept))
    check("the unknown quest id is gone", "99999999" not in kept)
    check("every legitimate entry survives",
          sorted(int(k) for k in kept) == sorted(legit_ids), str(kept))
    check("claimed status is preserved",
          all(reloaded["sp_quests"][str(q)]["status"] == 1 for q in legit_ids))
    check("normal quests are untouched", reloaded["quests"].get("31001") == 1)
    check("shared counters are untouched", reloaded["quest_db"].get("13_0") == 9)

    # **The repair must be PERSISTED**, not just applied in memory -- otherwise every
    # load pays for it again and any component reading the file still sees the damage.
    on_disk = json.load(open(path_for(1000001)))
    check("the cleaned save was written back",
          len(on_disk["sp_quests"]) == len(legit_ids),
          str(len(on_disk["sp_quests"])))

    # ---- and a clean save is left completely alone ------------------------
    before = json.load(open(path_for(1000001)))
    ps.load(1000001)
    check("a second load changes nothing",
          json.load(open(path_for(1000001))) == before)

    # ---- the bump that caused it no longer does ---------------------------
    st2 = _default(1000002)
    _seed_roster(st2)
    for _ in range(3):
        ps.bump_quest_counter(st2, QUEST_CASE_GACHA)
    armed = {int(k) for k, v in st2["sp_quests"].items() if v.get("cnt", 0) > 0}
    bad = [q for q in armed
           if (rows.get(q) or {}).get("_type") in UNSUPPORTED_QUEST_TYPES]
    check("three gacha pulls arm no unsupported-type quest", not bad, str(bad))
    check("and arm only a handful of real ones", len(armed) <= 3, str(sorted(armed)))

    print("\n" + ("ALL PASSED" if not _fail else f"{_fail} FAILED"))
    return 1 if _fail else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        import shutil
        shutil.rmtree(_TMP, ignore_errors=True)

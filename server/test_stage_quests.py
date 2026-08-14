#!/usr/bin/env python3
"""Clearing a stage must credit the "clear any stage of <family> N times" goals.

`_case_id` 5 counts stage clears by DUNGEON FAMILY, with `_case_v1` naming it (read off
the rows' own English text):

    0  main story    1  Kizuna Quests    2  Starshard Temple   7  ANY stage
    8  Guild Boss   21  Trainers Gym    22  Rank Up Abyss
   23  Transcend Corridor              24  Treasure Raiders

Nothing bumped case 5 at all, so "Complete any Kizuna Quest 1 time" sat at 0/1 however
many Kizuna stages were cleared. The client cannot re-derive this -- GetQuestValue reads
a server counter -- so the server has to keep it.

**A "Kizuna Quest" is not the Kizuna Tower.** The in-game panel lists one entry per cast
(dmaps 22001..22076); the 24 dmaps actually named "Kizuna Tower" are a separate stat
grind that does not appear in the EN build.

    python3 test_stage_quests.py

Uses its own SEVENSINS_ACCOUNTS tempdir so it never touches real save data.
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

_TMP = tempfile.mkdtemp(prefix="sevensins-stagequest-test-")
os.environ["SEVENSINS_ACCOUNTS"] = _TMP        # before player_state imports

import battle as bt           # noqa: E402
import player_state as ps     # noqa: E402
from player_state.core import UNSUPPORTED_QUEST_TYPES, _default, _seed_roster  # noqa: E402
from player_state.quests import (              # noqa: E402
    CATEGORY_ANY,
    QUEST_CASE_CLEAR_CATEGORY,
    stage_category,
)

_fail = 0

KIZUNA_STAGE = 2200111        # "A Small Payback", dmap 22001 "Death: Esmira"
TOWER_STAGE = 1511801         # "Bond of TEC", dmap 41002, a Kizuna TOWER floor
STORY_STAGE = 1101            # 1-1
GYM_STAGE = 1400001           # Trainers Gym "Beginner Class"
ABYSS_STAGE = 1200001         # Evolution Abyss, the step-19 stage
GOAL = 31020                  # "Complet any Kizuna Quest 1 time." (Beelzebub, step 21)


def check(name, cond, detail=""):
    global _fail
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        _fail += 1


def fresh():
    st = _default(1000001)
    _seed_roster(st)
    return st


def main():
    # ---- the family lookup -------------------------------------------------
    for stage, want, label in ((KIZUNA_STAGE, 1, "a Kizuna Quest stage"),
                               (TOWER_STAGE, 1, "a Kizuna Tower floor"),
                               (STORY_STAGE, 0, "main story 1-1"),
                               (2101, 0, "main story 2-1"),
                               (GYM_STAGE, 21, "Trainers Gym"),
                               (ABYSS_STAGE, 22, "Evolution Abyss")):
        got = stage_category(stage)
        check(f"{label} is family {want}", got == want, str(got))
    check("an unknown stage has no family", stage_category(999999999) is None)

    # The Kizuna panel's 57 casts must all resolve, not just the one we spot-checked.
    kiz = [s for s, r in (bt.dd.rows("stage") or {}).items()
           if 22000 <= int(r.get("_dmap_id") or 0) < 23000]
    check("every Kizuna Quest stage resolves to family 1",
          kiz and all(stage_category(s) == 1 for s in kiz), f"{len(kiz)} stages")
    # 41401/41404/… sit in the tower id span but are EVENT maps -- a range test would
    # have swept them in, which is why the towers are matched by name.
    ev = [s for s, r in (bt.dd.rows("stage") or {}).items()
          if int(r.get("_dmap_id") or 0) in (41401, 41404, 41407, 41410)]
    check("event maps in the tower id span are NOT Kizuna",
          all(stage_category(s) != 1 for s in ev), f"{len(ev)} stages")

    # ---- the goal actually advances ---------------------------------------
    row = bt.dd.row("quest", GOAL)
    check("the goal is still case 5 / Kizuna",
          row.get("_case_id") == QUEST_CASE_CLEAR_CATEGORY and row.get("_case_v1") == 1,
          str((row.get("_case_id"), row.get("_case_v1"))))
    key = f"5_{row['_case_v1']}"

    st = fresh()
    st["quests"]["32000"] = 1                  # its predecessor
    ps.bump_stage_category_quests(st, KIZUNA_STAGE)
    check("a Kizuna clear moves the goal's counter",
          st["quest_db"].get(key, 0) >= row["_case_cnt"], str(st["quest_db"]))

    # ---- and nothing else does --------------------------------------------
    st2 = fresh()
    ps.bump_stage_category_quests(st2, STORY_STAGE)
    check("a main-story clear does not credit the Kizuna goal",
          key not in st2["quest_db"], str(st2["quest_db"]))
    st3 = fresh()
    ps.bump_stage_category_quests(st3, GYM_STAGE)
    check("a Trainers Gym clear does not credit it either",
          key not in st3["quest_db"], str(st3["quest_db"]))

    # Clearing the same stage twice counts twice -- these are "N times" goals.
    st4 = fresh()
    for _ in range(3):
        ps.bump_stage_category_quests(st4, KIZUNA_STAGE)
    check("three clears count three times", st4["quest_db"].get(key) == 3,
          str(st4["quest_db"]))

    # ---- the families we deliberately do not credit ------------------------
    # Every `_case_v1` 0 and 7 row is `_type 7`, a system we do not run, so those keys
    # staying empty is correct rather than a miss. Assert the REASON, so that if a
    # supported row ever appears there this test fails instead of quietly passing.
    rows = bt.dd.rows("quest") or {}
    for fam in (0, CATEGORY_ANY):
        supported = [q for q, r in rows.items()
                     if r.get("_case_id") == QUEST_CASE_CLEAR_CATEGORY
                     and r.get("_case_v1") == fam
                     and r.get("_type") not in UNSUPPORTED_QUEST_TYPES]
        check(f"family {fam} has no supported quest to credit", not supported,
              str(supported))

    # A clear must never touch sp_quests for an unsupported type (the orphan bug).
    st5 = fresh()
    ps.bump_stage_category_quests(st5, KIZUNA_STAGE)
    bad = [q for q in st5["sp_quests"]
           if (rows.get(int(q)) or {}).get("_type") in UNSUPPORTED_QUEST_TYPES]
    check("no unsupported-type quest is armed by a clear", not bad, str(bad))

    print("\n" + ("ALL PASSED" if not _fail else f"{_fail} FAILED"))
    return 1 if _fail else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        import shutil
        shutil.rmtree(_TMP, ignore_errors=True)

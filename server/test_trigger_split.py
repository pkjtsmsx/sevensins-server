#!/usr/bin/env python3
"""Two opcodes for one status are two MOMENTS, not a duplicate.

    python3 test_trigger_split.py

A passive's `_actID` sometimes lists the same status twice, and the clause states two
timings to go with it:

    盛怒萬解：攻擊行動開始前，或受到傷害時，都將為自身疊加1層「盛怒」
    暈眩氣場：戰鬥開始時對敵隨機1人附加暈眩狀態，行動開始前對敵隨機1人附加暈眩狀態

Both opcodes used to inherit ONE trigger, because `annotate_passive` derives timing from
`_note1_en` and finds one clause per status name. Satan gained 2 Wrath per hit and none
before acting; the six 氣場 auras applied TWO control statuses at battle start and none
per turn. Deleting the "duplicate" would have lost a real moment -- the row is the game's
own data.

The English cannot fix it: it writes Satan's first moment as "Before dealing any attack",
which matches no pattern, and the Chinese markers already in PASSIVE_TRIGGERS are dead on
this path because the clause text is English -- adding four more and recompiling the whole
corpus changed exactly 0 effects (measured). So the split reads `_note1` directly, and
only to break a tie the pack itself created.

Anchored to the PACK: the affected skills are re-derived here rather than listed, so a
pack change that adds or removes a two-moment passive fails this rather than passing on a
stale id list.
"""
import collections
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
os.environ["SEVENSINS_ACCOUNTS"] = tempfile.mkdtemp(prefix="sevensins-split-")

import compile_skills as CS                                    # noqa: E402
import design_data as dd                                       # noqa: E402
from engine import specs                                       # noqa: E402

FAILURES = []


def check(cond, msg):
    if not cond:
        FAILURES.append(msg)


def _two_moment_passives():
    """-> {skill id: (status id, [triggers the Chinese states])}, from the pack."""
    rows = dd.rows("skill") or {}
    out = {}
    for sid, spec in specs.skills().items():
        if spec.get("type") != "passive":
            continue
        by = collections.Counter()
        for e in spec.get("effects") or []:
            if e.get("op") == "apply_status":
                by[(e.get("status") or {}).get("id")] += 1
        for st, n in by.items():
            if n < 2 or not st:
                continue
            trig = CS.zh_triggers_in(CS.zh_grant_clause(rows, rows.get(sid) or {}, st))
            if len(trig) >= 2:
                out[sid] = (st, trig[:n])
    return out


def each_opcode_gets_its_own_moment():
    found = _two_moment_passives()
    check(found, "no two-moment passive found at all -- the pack or the matcher moved")
    for sid, (st, triggers) in found.items():
        got = [e.get("trigger") for e in (specs.skills()[sid].get("effects") or [])
               if e.get("op") == "apply_status"
               and (e.get("status") or {}).get("id") == st]
        check(got[:len(triggers)] == triggers,
              f"skill {sid} status {st}: triggers {got[:len(triggers)]}, "
              f"the Chinese states {triggers}")
        check(len(set(got)) > 1,
              f"skill {sid} status {st}: both opcodes still share one trigger {got}")


def the_split_is_narrow():
    """It must only ever break a tie the pack created, never invent a trigger."""
    found = _two_moment_passives()
    # Every split effect is one of the recovered set -- nothing else carries the marker.
    split = [(sid, e) for sid, spec in specs.skills().items()
             for e in (spec.get("effects") or [])
             if e.get("trigger_source") == "prose_zh_split"]
    check(split, "nothing was split -- the compiler step is not running")
    check({sid for sid, _ in split} <= set(found),
          f"a skill was split that the pack does not state two moments for: "
          f"{sorted({sid for sid, _ in split} - set(found))[:5]}")
    # A one-moment duplicate must be LEFT ALONE rather than guessed at.
    rows = dd.rows("skill") or {}
    untouched = 0
    for sid, spec in specs.skills().items():
        if spec.get("type") != "passive" or sid in found:
            continue
        by = collections.Counter()
        for e in spec.get("effects") or []:
            if e.get("op") == "apply_status":
                by[(e.get("status") or {}).get("id")] += 1
        if any(n > 1 and st for st, n in by.items()):
            untouched += 1
            for e in spec.get("effects") or []:
                check(e.get("trigger_source") != "prose_zh_split",
                      f"skill {sid} was split without the Chinese stating two moments")
    check(untouched, "no single-moment duplicate left to check -- premise stale")


def a_glossary_line_is_never_read_as_a_trigger():
    """魂刺痕's 回合開始時 is its DoT TICK, not when it is granted.

    A first pass at this read glossary lines and reported 677 recoverable effects where
    the honest number is 26. The guard is the reason those two numbers differ.
    """
    rows = dd.rows("skill") or {}
    probe = 0
    for sid, spec in specs.skills().items():
        if spec.get("type") != "passive":
            continue
        zh = (rows.get(sid) or {}).get("_note1") or ""
        glossary = [ln for ln in re.split(r"[\n。]", zh) if ln.lstrip().startswith("※")]
        if not glossary:
            continue
        for st in {(e.get("status") or {}).get("id")
                   for e in (spec.get("effects") or []) if e.get("op") == "apply_status"}:
            if not st:
                continue
            clause = CS.zh_grant_clause(rows, rows.get(sid) or {}, st)
            if clause is None:
                continue
            probe += 1
            check(not clause.lstrip().startswith("※"),
                  f"skill {sid}: a glossary line was taken as the grant clause: "
                  f"{clause[:60]}")
    check(probe, "no passive with a glossary line to probe -- premise stale")


def main():
    each_opcode_gets_its_own_moment()
    the_split_is_narrow()
    a_glossary_line_is_never_read_as_a_trigger()
    for f in FAILURES:
        print("FAIL:", f)
    print(f"{len(FAILURES)} failure(s)")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())

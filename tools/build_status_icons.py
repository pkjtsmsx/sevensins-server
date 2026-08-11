#!/usr/bin/env python3
"""Build server/battle_data/status_icons.json: our status catalog NAME -> the
DesignSkillForm row id the client needs in DamageInfo.status to draw that status'
icon.

Reversed from the 2.2.7 client (see memory sevensins-battle, "Step 5"):
`DamageInfo.status` is `List<List<int>>`, each inner list `[order, skillID, round]`
(StatusST..ctor also reads optional [lv, value, actOn]). `AttackBehavior.updateStatus`
resolves the graphic as `DesignSkillForm[skillID]._statusID -> DesignStatusForm`, and
the status VISUALS live on dedicated `_type == 6` (STATUS) skill rows -- an attack skill's
own `_statusID` is 0. `round` doubles as the add/remove flag: >0 inserts and counts down
(the client self-expires at 0), 0 removes the status keyed by that skillID.

So for each status name in status_catalog.json we pick the type-6 skill row whose
`_name_en` matches the name exactly and whose `_statusID` is non-zero (a real graphic).
Names with no such row are omitted -- the server then emits no icon for them rather than
a wrong one.
"""
import collections
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(os.path.dirname(HERE), "server")
sys.path.insert(0, SERVER)
import design_data as dd  # noqa: E402

STATUS_SKILL_TYPE = 6


def build():
    by_name = collections.defaultdict(list)
    for sid in dd.rows("skill"):
        r = dd.row("skill", sid) or {}
        if r.get("_type") == STATUS_SKILL_TYPE:
            name = (r.get("_name_en") or "").strip()
            if name:
                by_name[name].append((sid, r.get("_statusID") or 0))

    catalog = json.load(open(os.path.join(SERVER, "battle_data",
                                          "status_catalog.json"), encoding="utf-8"))
    mapping = {}
    for name in catalog:
        rowset = by_name.get(name)
        if not rowset:
            continue
        with_fx = [x for x in rowset if x[1]]     # nonzero _statusID = a real graphic
        if not with_fx:
            continue
        mapping[name] = sorted(with_fx)[0][0]     # canonical = lowest skill id
    return mapping


if __name__ == "__main__":
    mapping = build()
    out = os.path.join(SERVER, "battle_data", "status_icons.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(mapping, f, ensure_ascii=False, indent=0, sort_keys=True)
    print(f"wrote {len(mapping)} status->skill icon mappings to {out}")

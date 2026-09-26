#!/usr/bin/env python3
"""The Consonance master passive: recovered link, and the rung it is taken to.

    python3 test_master_passive.py

69 casts own a passive the Consonance panel unlocks and no cast's `_skills` column lists,
so it never reached a fight. The cast->passive link is RECOVERED from the pack (see
battle.master_passive); the RUNG is a design choice -- the cast's Karma rank, clamped --
because DesignSoulbookKizunaRow is not in this pack and no RPC carries a level.

The derivation is the part that can rot, so it is re-derived here from the pack rather
than asserted against a list: a pack change that makes two ladders match one cast, or
drops one below the six-rung floor, fails here rather than silently picking a passive.
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["SEVENSINS_ACCOUNTS"] = tempfile.mkdtemp(prefix="sevensins-master-test-")

import battle as bt                                            # noqa: E402
import design_data as dd                                       # noqa: E402
from engine import specs                                       # noqa: E402
from player_state import roster                                # noqa: E402

FAILURES = []


def check(cond, msg):
    if not cond:
        FAILURES.append(msg)


def the_link_is_unambiguous():
    """Every recovered cast must match exactly ONE qualifying ladder, or it is a guess."""
    skills = specs.skills()
    table = bt._master_table()
    check(table, "no master passive recovered at all")
    for cid, row in (dd.rows("char") or {}).items():
        if row.get("_type") != 1 or not row.get("_order"):
            continue
        own = {s for s in (row.get("_skills") or []) if s}
        lo, hi = int(cid) * 100, int(cid) * 100 + 100
        ladders = {}
        for sid, spec in skills.items():
            if lo <= int(sid) < hi and spec.get("type") == "passive":
                ladders.setdefault(spec.get("group"), []).append(int(sid))
        qualifying = [g for g, lv in ladders.items()
                      if g not in own and len(lv) >= bt.MASTER_MIN_LADDER]
        check(len(qualifying) <= 1,
              f"cast {cid} matches {len(qualifying)} candidate ladders {qualifying} -- "
              f"the recovered link is a coin flip")
        if int(cid) in table:
            base, depth = table[int(cid)]
            check(base in qualifying, f"cast {cid} recovered a ladder that no longer qualifies")
            check(base not in own,
                  f"cast {cid}: {base} is in its own _skills -- it is not a panel passive")
            check(depth >= bt.MASTER_MIN_LADDER, f"cast {cid} ladder shorter than the floor")


def the_rung_follows_karma_and_clamps():
    """Karma rank drives it; battle re-clamps so an inflated flv cannot over-level."""
    table = bt._master_table()
    cid, (base, depth) = next(iter(sorted(table.items())))
    entry = {"id": cid, "lv": 1}
    state = {"roster": {"u1": entry}, "karma": {}}
    from player_state.core import karma_of

    karma_of(state, cid)["flv"] = 3
    roster._annotate_master(state, entry)
    check(entry.get("master_lv") == 3, f"karma 3 gave master_lv {entry.get('master_lv')}")

    # The skill actually reaches the unit's kit, at the right rung. `skills` is a list
    # of skill ids and the rung is encoded in the id, so compare against what
    # skill_at_rank resolves rather than re-deriving the offset here.
    base_only = bt.Unit(order="p1", char_id=cid, team=1, index=0, lv=50, master_lv=0)
    u = bt.Unit(order="p1", char_id=cid, team=1, index=0, lv=50, master_lv=3)
    added = [i for i in u.skills if i not in base_only.skills]
    check(added == [bt.skill_at_rank(base, 3)],
          f"cast {cid}: expected rung 3 of {base}, kit gained {added}")

    # Clamped: karma far above the ladder must not exceed its depth.
    karma_of(state, cid)["flv"] = 999
    roster._annotate_master(state, entry)
    check(entry["master_lv"] == 999, "roster stopped passing karma through")
    hi = bt.Unit(order="p1", char_id=cid, team=1, index=0, lv=50,
                 master_lv=entry["master_lv"])
    added_hi = [i for i in hi.skills if i not in base_only.skills]
    check(added_hi == [bt.skill_at_rank(base, depth)],
          f"cast {cid}: karma 999 resolved to {added_hi}, not the {depth}-rung cap "
          f"{[bt.skill_at_rank(base, depth)]}")
    # Anchored to the ladder's own ids rather than to skill_at_rank, which clamps
    # internally and so cannot tell depth from depth+1 on its own.
    ladder = sorted(int(sid) for sid, spec in specs.skills().items()
                    if spec.get("group") == base and spec.get("type") == "passive")
    check(len(ladder) == depth, f"cast {cid}: ladder is {len(ladder)} ids, depth says {depth}")
    check(added_hi and added_hi[0] in ladder,
          f"cast {cid}: resolved {added_hi} is not one of its own ladder ids")
    check(added_hi == [ladder[-1]],
          f"cast {cid}: karma 999 resolved to {added_hi}, not the ladder top {ladder[-1]}")
    check(bt.skill_at_rank(base, 3) == ladder[2],
          f"cast {cid}: rung 3 is {bt.skill_at_rank(base, 3)}, ladder says {ladder[2]}")

    # Karma 0 (or a cast with no ladder) grants nothing.
    check(len(base_only.skills) < len(u.skills),
          "master_lv 0 still appended a passive")


def only_the_recovered_casts_get_one():
    """A cast with no qualifying ladder must not be handed somebody else's passive."""
    table = bt._master_table()
    plain = [int(c) for c, r in (dd.rows("char") or {}).items()
             if r.get("_type") == 1 and r.get("_order") and int(c) not in table]
    check(plain, "every listed cast has a master passive -- the filter is doing nothing")
    for cid in plain[:20]:
        entry = {"id": cid, "lv": 1}
        state = {"roster": {"u1": entry}, "karma": {}}
        from player_state.core import karma_of
        karma_of(state, cid)["flv"] = 10
        roster._annotate_master(state, entry)
        check("master_lv" not in entry,
              f"cast {cid} has no recovered ladder but was given master_lv")


def main():
    the_link_is_unambiguous()
    the_rung_follows_karma_and_clamps()
    only_the_recovered_casts_get_one()
    for f in FAILURES:
        print("FAIL:", f)
    print(f"{len(FAILURES)} failure(s)")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())

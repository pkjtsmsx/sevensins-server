#!/usr/bin/env python3
"""The save editor writes rows the CLIENT can actually file.

Reported 2026-08-20: adding a Starshard or Soulmirror through the editor made the whole
list go blank -- "No Available Soulmirror", Owned `-/-` -- until the added piece was
removed again. One malformed row takes the entire list with it.

`_set_item` hand-wrote a stackable record for everything:

    {"amount": n, "attr": {}, "iid": iid, "sid": slot, "uid": ""}   in storage 1

For a Starshard or Soulmirror that is wrong three ways at once -- wrong storage, no uid,
and no rolled `attr` block. They are INSTANCES, not stacks: one slot each, in storage 2
and 3 respectively, with a uid and the mandatory `lv`/`ts` attributes. Exactly the trap
`grant_reward` was fixed for earlier; the editor bypassed it.

    python3 test_save_editor.py
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

_TMP = tempfile.mkdtemp(prefix="sevensins-editor-test-")
os.environ["SEVENSINS_ACCOUNTS"] = _TMP

import battle as bt                                     # noqa: E402
import player_state as ps                               # noqa: E402
import save_editor as se                                # noqa: E402

_fail = 0


def check(name, cond, detail=""):
    global _fail
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        _fail += 1


def _sample_ids():
    """A real Soulmirror and a real Starshard item id from the design data."""
    soulmirror = starshard = None
    for iid in bt.dd.rows("item"):
        if ps.item_bucket(iid) != "equipment":
            continue
        action = (bt.dd.row("item", iid) or {}).get("_action")
        if action in ps.SOULFRAG_SLOT_INDEX:
            soulmirror = soulmirror or iid
        else:
            starshard = starshard or iid
        if soulmirror and starshard:
            break
    return soulmirror, starshard


def check_instances_go_to_their_own_storage():
    soulmirror, starshard = _sample_ids()
    check("the design data has a Soulmirror item", bool(soulmirror))
    check("  ...and a Starshard item", bool(starshard))

    for iid, label, storage in ((soulmirror, "Soulmirror", str(ps.BP_STORAGE_SOULFRAG)),
                                (starshard, "Starshard", str(ps.BP_STORAGE_EQUIPMENT))):
        st = ps.load(1000070 + iid % 100)
        se._set_item(st, {"iid": iid, "amount": 2})

        check(f"{label} lands in storage {storage}",
              str(se._instance_storage(iid)) == storage, str(se._instance_storage(iid)))
        bag = st["backpack"].get(storage, {})
        rows = [r for r in bag.values() if int(r.get("iid", -1)) == iid]
        check(f"  ...as TWO separate instances, not one stack of 2",
              len(rows) == 2, str(len(rows)))
        # The bug: a stack in Normal storage. Nothing of the sort may appear.
        normal = st["backpack"].get("1", {})
        check("  ...and nothing is written to Normal storage",
              not any(int(r.get("iid", -1)) == iid for r in normal.values()))

        rec = rows[0]
        check("  ...carrying a real uid", bool(rec.get("uid")), repr(rec.get("uid")))
        check("    ...that is unique across the instances",
              len({r["uid"] for r in rows}) == len(rows),
              str([r["uid"] for r in rows]))
        attr = rec.get("attr") or {}
        # `lv` and `ts` are mandatory: without them the piece does not render.
        check("  ...and a rolled attr block with lv/ts",
              "lv" in attr and "ts" in attr, str(sorted(attr)))
        check("    ...plus at least one stat roll",
              any(k.startswith(("be_", "bid_")) for k in attr), str(sorted(attr)))


def check_counts_go_up_and_down():
    soulmirror, _ = _sample_ids()
    storage = str(ps.BP_STORAGE_SOULFRAG)
    st = ps.load(1000061)

    def owned():
        return sum(1 for r in st["backpack"].get(storage, {}).values()
                   if int(r.get("iid", -1)) == soulmirror)

    se._set_item(st, {"iid": soulmirror, "amount": 3})
    check("asking for 3 gives 3 instances", owned() == 3, str(owned()))
    se._set_item(st, {"iid": soulmirror, "amount": 1})
    check("  ...lowering the count deletes instances", owned() == 1, str(owned()))
    se._set_item(st, {"iid": soulmirror, "amount": 0})
    check("  ...and 0 removes them all", owned() == 0, str(owned()))
    # Re-adding after a full clear must still produce a usable uid.
    se._set_item(st, {"iid": soulmirror, "amount": 1})
    rec = next(r for r in st["backpack"][storage].values()
               if int(r.get("iid", -1)) == soulmirror)
    check("re-adding after clearing still yields a uid", bool(rec.get("uid")),
          repr(rec.get("uid")))


def check_ordinary_items_are_still_stacks():
    """The instance path must not swallow normal consumables."""
    st = ps.load(1000062)
    iid = 487                                    # Popular Poster, an ordinary gift item
    check("an ordinary item is not treated as an instance",
          se._instance_storage(iid) is None, str(se._instance_storage(iid)))
    se._set_item(st, {"iid": iid, "amount": 25})
    rows = [r for r in st["backpack"]["1"].values() if int(r.get("iid", -1)) == iid]
    check("  ...and stays ONE row with an amount", len(rows) == 1, str(len(rows)))
    check("  ...carrying the amount asked for", rows[0]["amount"] == 25,
          str(rows[0]["amount"]))
    se._set_item(st, {"iid": iid, "amount": 0})
    rows = [r for r in st["backpack"]["1"].values() if int(r.get("iid", -1)) == iid]
    check("  ...and 0 removes the slot", not rows, str(rows))


def main():
    for fn in (check_instances_go_to_their_own_storage,
               check_counts_go_up_and_down,
               check_ordinary_items_are_still_stacks):
        print(f"\n{fn.__name__}:")
        fn()
    print(f"\n{_fail} failure(s)")
    return 1 if _fail else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        import shutil
        shutil.rmtree(_TMP, ignore_errors=True)

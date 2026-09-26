#!/usr/bin/env python3
"""A save carrying an internal TEST item is repaired at load.

    python3 test_purge_test_items.py

`grant_reward` and `make_rune` refuse these now, but that only protects NEW grants. A
player who already banked one cannot play: a test starshard in storage 2 takes the
client's starshard panel down and the session with it, and one was reported stuck exactly
there.

The repair has to do BOTH halves -- drop the bag row AND clear any `equips_list` slot
holding its uid. Removing the row alone leaves a cast wearing a uid that resolves to
nothing, which is the same dangling reference that breaks the panel.

Uses its own SEVENSINS_ACCOUNTS tempdir; it never touches real save data.
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
_TMP = tempfile.mkdtemp(prefix="sevensins-purge-test-")
os.environ["SEVENSINS_ACCOUNTS"] = _TMP

import design_data as dd                                       # noqa: E402
import player_state as ps                                       # noqa: E402
from player_state.core import path_for                          # noqa: E402

FAILURES = []


def check(cond, msg):
    if not cond:
        FAILURES.append(msg)


def _test_ids():
    return sorted(int(i) for i, r in (dd.rows("item") or {}).items()
                  if "測試" in (r.get("_itemName") or ""))


def _wounded_save(pid, equip_it):
    """Write a save holding a test starshard, optionally worn by a cast."""
    st = ps.load(pid)
    shard = next(i for i in _test_ids()
                 if (dd.row("item", i) or {}).get("_action") in range(111, 117))
    uid = f"{pid}r0001"
    st["backpack"].setdefault("2", {})["1"] = {
        "iid": shard, "sid": 1, "uid": uid, "amount": 1,
        "attr": {"lv": 0, "ts": 0},
    }
    # ...plus an ordinary row that must survive untouched.
    keep = next(i for i, r in (dd.rows("item") or {}).items()
                if "測試" not in (r.get("_itemName") or "")
                and (r.get("_action") in range(111, 117)))
    st["backpack"]["2"]["2"] = {"iid": int(keep), "sid": 2, "uid": f"{pid}r0002",
                                "amount": 1, "attr": {"lv": 0, "ts": 0}}
    cast_uid = next(iter(st["roster"]))
    if equip_it:
        st["roster"][cast_uid]["equips_list"] = [uid] + [""] * 17
    ps.save(st)
    return shard, uid, cast_uid, int(keep)


def a_bagged_test_item_is_removed():
    pid = 1000501
    shard, uid, _cast, keep = _wounded_save(pid, equip_it=False)
    st = ps.load(pid)
    bag = (st.get("backpack") or {}).get("2") or {}
    iids = {r.get("iid") for r in bag.values()}
    check(shard not in iids, f"test item {shard} survived the load purge")
    check(keep in iids, f"ordinary starshard {keep} was removed too -- too aggressive")
    # ...and it was persisted, not just fixed in memory.
    with open(path_for(pid)) as f:
        on_disk = json.load(f)
    check(shard not in {r.get("iid") for r in
                        ((on_disk.get("backpack") or {}).get("2") or {}).values()},
          "the purge was not written back to disk")


def an_equipped_test_item_is_also_unequipped():
    """The half that matters: a dangling uid is its own crash."""
    pid = 1000502
    shard, uid, cast_uid, _keep = _wounded_save(pid, equip_it=True)
    st = ps.load(pid)
    worn = st["roster"][cast_uid].get("equips_list") or []
    check(uid not in worn,
          f"cast {cast_uid} is still wearing {uid}, whose bag row is gone -- a dangling "
          f"reference is the same bug the purge exists to fix")
    bag = (st.get("backpack") or {}).get("2") or {}
    check(shard not in {r.get("iid") for r in bag.values()},
          "the equipped case left the bag row behind")
    check(len(worn) == 18, f"equips_list was resized to {len(worn)}, must stay 18")


def a_clean_save_is_not_rewritten():
    """No test item -> no change, so this does not rewrite every save on every load."""
    pid = 1000503
    st = ps.load(pid)
    ps.save(st)
    before = os.path.getmtime(path_for(pid))
    from player_state.core import _purge_test_items
    check(_purge_test_items(st) is False,
          "the purge reported a change on a save with no test item")
    check(os.path.getmtime(path_for(pid)) == before, "a clean save was rewritten")


def it_is_idempotent():
    pid = 1000504
    _wounded_save(pid, equip_it=True)
    first = ps.load(pid)
    from player_state.core import _purge_test_items
    check(_purge_test_items(first) is False,
          "a second purge still finds something -- the first did not finish")


def it_never_raises():
    """It runs inside `load`, where an exception once destroyed accounts."""
    from player_state.core import _purge_test_items
    for junk in ({}, {"backpack": None}, {"backpack": {"2": None}},
                 {"backpack": {"2": {"1": None}}},
                 {"backpack": {"2": {"1": {"iid": None}}}},
                 {"roster": {"u": {"equips_list": None}}},
                 {"backpack": {"2": {"1": {"iid": 999999999}}}}):
        try:
            _purge_test_items(junk)
        except Exception as exc:                               # noqa: BLE001
            FAILURES.append(f"purge raised on {junk!r}: {exc}")


def main():
    a_bagged_test_item_is_removed()
    an_equipped_test_item_is_also_unequipped()
    a_clean_save_is_not_rewritten()
    it_is_idempotent()
    it_never_raises()
    for f in FAILURES:
        print("FAIL:", f)
    print(f"{len(FAILURES)} failure(s)")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())

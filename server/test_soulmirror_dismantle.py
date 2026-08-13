"""Backpack 119 DecomposeSoulFrag -- the refund maths and the storage side effects.

Run with `python3 test_soulmirror_dismantle.py` from server/.
"""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import player_state as ps
from player_state.core import _default, _seed_roster


def fresh_state():
    """A brand-new account, built in memory -- never touches accounts/ on disk."""
    st = _default(1000001)
    _seed_roster(st)
    return st


def a_mirror(rarity=4, sf_type=1):
    """-> an item id for a Soulmirror of the given rarity and tier, or None."""
    import battle as bt
    lo, hi = {1: (101, 103), 2: (104, 106), 3: (107, 109)}[sf_type]
    for iid, row in (bt.dd.rows("item") or {}).items():
        if (lo <= int(row.get("_action") or 0) <= hi
                and int(row.get("_param2") or 0) == rarity):
            try:
                ps.make_soulmirror({"player_id": 1}, int(iid))
            except ValueError:
                continue      # no usable equipment_bonus rows
            return int(iid)
    return None


def test_refund_formula():
    """base + 80% of what levelling actually cost, truncated once at the end."""
    # lv 0: nothing sunk in yet, so the refund is the flat base alone.
    for rarity in (1, 2, 3, 4, 5):
        item, amt = ps.soulmirror_refund(rarity, 1, 0)
        assert item == ps.SOULFRAG_ENHANCE_MATERIAL, item
        assert amt == 10 * 2 ** (rarity - 1), (rarity, amt)

    # A +15 ★4 tier-1 mirror cost 50000 essence (the figure the upgrade panel shows).
    spent = ps.soulfrag_material_cost(4, 1, 0, 15)
    assert spent == 50000, spent
    item, amt = ps.soulmirror_refund(4, 1, 15)
    assert amt == int(spent * 0.8 + 80) == 40080, amt

    # Monotonic in level, and never more than was put in plus the base.
    prev = -1
    for lv in range(0, 16):
        _, amt = ps.soulmirror_refund(3, 1, lv)
        assert amt > prev, lv
        assert amt <= ps.soulfrag_material_cost(3, 1, 0, lv) + 40
        prev = amt
    print("refund formula OK")


def test_dismantle():
    state = fresh_state()
    iid = a_mirror(4, 1)
    assert iid, "no rarity-4 tier-1 Soulmirror in the design data"

    kept = ps.grant_soulmirror(state, iid)["uid"]
    a = ps.grant_soulmirror(state, iid)
    b = ps.grant_soulmirror(state, iid)
    b["attr"][ps.RUNE_ATTR_LEVEL] = 15
    before_essence = ps.item_count(state, ps.SOULFRAG_ENHANCE_MATERIAL)

    reward, gone = ps.dismantle_soulmirrors(state, [a["uid"], b["uid"]])

    # One aggregated line, both mirrors' worth.
    assert len(reward) == 1, reward
    item, amount = reward[0]
    assert item == ps.SOULFRAG_ENHANCE_MATERIAL, item
    assert amount == 80 + 40080, amount
    assert ps.item_count(state, item) == before_essence + amount

    # Gone from storage, and the survivor untouched.
    bag = state["backpack"][str(ps.BP_STORAGE_SOULFRAG)]
    assert sorted(gone) == sorted(gone), gone
    assert len(gone) == 2
    assert all(sid not in bag for sid in gone), bag.keys()
    assert any(e["uid"] == kept for e in bag.values()), "wrong mirror removed"

    # The 145 push must carry iid-0 tombstones or the icons stay on screen.
    blob = ps.backpacks_all_json(state, {ps.BP_STORAGE_SOULFRAG},
                                 {ps.BP_STORAGE_SOULFRAG: gone})
    import json
    slots = json.loads(blob)["backpack_type"][str(ps.BP_STORAGE_SOULFRAG)]["sid"]
    for sid in gone:
        assert slots[str(sid)]["iid"] == 0, slots[str(sid)]
        assert slots[str(sid)]["attr"] == {}, slots[str(sid)]
    print("dismantle OK ->", reward)


def test_refusals():
    """Every refusal path must raise, so the caller answers [0] rather than nothing."""
    state = fresh_state()
    iid = a_mirror(4, 1)
    m = ps.grant_soulmirror(state, iid)

    for uids, exc in (
            (["nope"], LookupError),          # unknown uid
            ([m["uid"], m["uid"]], ValueError),   # the same mirror twice
    ):
        try:
            ps.dismantle_soulmirrors(state, uids)
        except exc:
            pass
        else:
            raise AssertionError(f"{uids} should have raised {exc.__name__}")

    # A locked mirror is refused, and nothing is consumed on the way out.
    locked = ps.grant_soulmirror(state, iid)
    ps.set_equip_lock(state, locked["uid"], 1)
    held = len(state["backpack"][str(ps.BP_STORAGE_SOULFRAG)])
    try:
        ps.dismantle_soulmirrors(state, [locked["uid"]])
    except ValueError:
        pass
    else:
        raise AssertionError("a locked mirror should be refused")
    assert len(state["backpack"][str(ps.BP_STORAGE_SOULFRAG)]) == held
    print("refusals OK")


def test_unequips():
    """A worn mirror comes off the cast, or its slot points at a dead uid."""
    state = fresh_state()
    iid = a_mirror(4, 1)
    m = ps.grant_soulmirror(state, iid)
    char_uid = next(iter(state["roster"]))
    slot = ps.soulfrag_slot(iid)
    ps.wear_soulmirror(state, char_uid, m["uid"], slot)
    assert m["uid"] in ps.char_equips(state["roster"][char_uid])

    ps.dismantle_soulmirrors(state, [m["uid"]])
    assert m["uid"] not in ps.char_equips(state["roster"][char_uid])
    print("unequip OK")


if __name__ == "__main__":
    test_refund_formula()
    test_dismantle()
    test_refusals()
    test_unequips()
    print("all OK")

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


# ---- fuse / transmute (Backpack 117) ---------------------------------------


def fusable(sf_type=1):
    """-> (charId, [action, ...]) for a character the transmute form lists."""
    tier = ps.transmute_pool()[sf_type]
    char_id = sorted(tier)[0]
    return char_id, sorted(tier[char_id])


def a_fusable_mirror(state, char_id, action):
    items = ps.soulmirror_items_for(ps.SOULFRAG_TRANSMUTE_RARITY, char_id, action)
    assert items, (char_id, action)
    return ps.grant_soulmirror(state, items[0])


def test_transmute_pool():
    pool = ps.transmute_pool()
    assert sorted(pool) == [1, 2], sorted(pool)          # Apoc. is not fusable
    for sf_type in (1, 2):
        assert len(pool[sf_type]) == 112, len(pool[sf_type])
        acts = {a for actions in pool[sf_type].values() for a in actions}
        assert acts == ({101, 102, 103} if sf_type == 1 else {104, 105, 106}), acts
    # The panel prefab has 3 icon slots; a larger number would leave Confirm dead.
    assert 1 <= ps.SOULFRAG_TRANSMUTE_NUM <= 3, ps.SOULFRAG_TRANSMUTE_NUM
    print("transmute pool OK")


def test_fuse_uniform_is_deterministic():
    """All three sharing a char AND a slot -> exactly that mirror, per the predict text."""
    state = fresh_state()
    char_id, actions = fusable(1)
    uids = [a_fusable_mirror(state, char_id, actions[0])["uid"]
            for _ in range(ps.SOULFRAG_TRANSMUTE_NUM)]
    coins_before = int(state["currency"][str(ps.CURRENCY_COIN)])

    new, gone, coins = ps.fuse_soulmirrors(state, uids)

    row = __import__("battle").dd.row("item", int(new["iid"]))
    assert int(row["_param3"]) == char_id, row
    assert int(row["_action"]) == actions[0], row
    assert int(row["_param2"]) == ps.SOULFRAG_TRANSMUTE_RARITY, row
    assert coins == ps.soulfrag_transmute_cost()
    assert int(state["currency"][str(ps.CURRENCY_COIN)]) == coins_before - coins
    assert len(gone) == ps.SOULFRAG_TRANSMUTE_NUM

    # Inputs consumed, output present -- net one mirror fewer than we started with.
    bag = state["backpack"][str(ps.BP_STORAGE_SOULFRAG)]
    assert all(sid not in bag for sid in gone)
    assert any(e["uid"] == new["uid"] for e in bag.values())
    assert len(bag) == 1

    # The reward must NOT land on a slot the same push tombstones -- tombstones are
    # written after the live entries, so a collision would silently delete it.
    import json
    slots = json.loads(ps.backpacks_all_json(
        state, {ps.BP_STORAGE_SOULFRAG},
        {ps.BP_STORAGE_SOULFRAG: gone}))["backpack_type"][
            str(ps.BP_STORAGE_SOULFRAG)]["sid"]
    assert slots[str(new["sid"])]["iid"] == int(new["iid"]), slots[str(new["sid"])]
    for sid in gone:
        assert slots[str(sid)]["iid"] == 0, slots[str(sid)]
    print("uniform fuse OK -> item", new["iid"])


def test_fuse_mixed_stays_legal():
    """A mixed selection rolls, but never off the design form's legal list."""
    import random
    pool = ps.transmute_pool()[1]
    chars = sorted(pool)[:3]
    for seed in range(12):
        state = fresh_state()
        uids = [a_fusable_mirror(state, c, sorted(pool[c])[i % 3])["uid"]
                for i, c in enumerate(chars)]
        new, _gone, _coins = ps.fuse_soulmirrors(state, uids,
                                                 rng=random.Random(seed))
        row = __import__("battle").dd.row("item", int(new["iid"]))
        char_id, action = int(row["_param3"]), int(row["_action"])
        assert action in pool.get(char_id, ()), (seed, char_id, action)
        assert int(row["_param2"]) == ps.SOULFRAG_TRANSMUTE_RARITY
    print("mixed fuse stays legal OK")


def test_fuse_refusals():
    state = fresh_state()
    char_id, actions = fusable(1)
    good = [a_fusable_mirror(state, char_id, actions[0])["uid"]
            for _ in range(ps.SOULFRAG_TRANSMUTE_NUM)]

    cases = [
        (good[:-1], ValueError),                  # too few
        (good + [good[0]], ValueError),           # too many
        ([good[0]] * ps.SOULFRAG_TRANSMUTE_NUM, ValueError),   # duplicates
        (["nope"] + good[1:], LookupError),       # unknown uid
    ]
    for uids, exc in cases:
        try:
            ps.fuse_soulmirrors(state, uids)
        except exc:
            pass
        else:
            raise AssertionError(f"{uids} should have raised {exc.__name__}")

    # Mixing tiers is refused (sfType 1 with sfType 2).
    ex = a_fusable_mirror(state, *(lambda c, a: (c, a[0]))(*fusable(2)))
    try:
        ps.fuse_soulmirrors(state, good[:-1] + [ex["uid"]])
    except ValueError:
        pass
    else:
        raise AssertionError("mixing Break with EX Break should be refused")

    # Too poor, and nothing is consumed on the way out.
    held = len(state["backpack"][str(ps.BP_STORAGE_SOULFRAG)])
    state["currency"][str(ps.CURRENCY_COIN)] = 0
    try:
        ps.fuse_soulmirrors(state, good)
    except ValueError:
        pass
    else:
        raise AssertionError("a fuse with no coins should be refused")
    assert len(state["backpack"][str(ps.BP_STORAGE_SOULFRAG)]) == held
    print("fuse refusals OK")


def test_push_carries_only_what_changed():
    """The 145 push must NOT resend untouched slots of an equipment storage.

    Resending all of storage 3 to delete one mirror made the client run ChangeEquip
    once per slot, and `RemoveEquipment` (0x18EE8A0) then looked the item up a SECOND
    time in the per-type list from GetItemSpace and called `RemoveAt(Index)` with **no
    -1 check**. After a mass update that lookup missed, RemoveAt(-1) threw
    ArgumentOutOfRangeException, and the removal aborted half-done: the item was gone
    from _equipList but its icon stayed on screen. Verified on device -- narrowing the
    push to the tombstone alone made both the exception and the stuck icon disappear.
    """
    import json
    state = fresh_state()
    iid = a_mirror(4, 1)
    keep = [ps.grant_soulmirror(state, iid)["uid"] for _ in range(3)]
    doomed = ps.grant_soulmirror(state, iid)["uid"]
    _reward, gone = ps.dismantle_soulmirrors(state, [doomed])

    blob = json.loads(ps.backpacks_all_json(
        state, {ps.BP_STORAGE_SOULFRAG, ps.BP_STORAGE_NORMAL},
        {ps.BP_STORAGE_SOULFRAG: gone},
        only={ps.BP_STORAGE_SOULFRAG: []}))["backpack_type"]

    slots = blob[str(ps.BP_STORAGE_SOULFRAG)]["sid"]
    assert set(slots) == {str(s) for s in gone}, slots
    assert all(v["iid"] == 0 for v in slots.values()), slots
    assert len(keep) == 3 and len(gone) == 1

    # A fuse pushes the tombstones PLUS the one new mirror, and nothing else.
    state = fresh_state()
    char_id, actions = fusable(1)
    uids = [a_fusable_mirror(state, char_id, actions[0])["uid"]
            for _ in range(ps.SOULFRAG_TRANSMUTE_NUM)]
    a_fusable_mirror(state, char_id, actions[0])          # a bystander
    new, gone, _coins = ps.fuse_soulmirrors(state, uids)
    slots = json.loads(ps.backpacks_all_json(
        state, {ps.BP_STORAGE_SOULFRAG},
        {ps.BP_STORAGE_SOULFRAG: gone},
        only={ps.BP_STORAGE_SOULFRAG: [new["sid"]]}))[
            "backpack_type"][str(ps.BP_STORAGE_SOULFRAG)]["sid"]
    assert set(slots) == {str(s) for s in gone} | {str(new["sid"])}, slots
    assert slots[str(new["sid"])]["iid"] == int(new["iid"])
    print("145 push carries only the changed slots OK")


def test_game_rule_carries_the_unguarded_keys():
    """Keys the client dereferences BEFORE any null/ContainsKey guard.

    `Formula.GetItemCountDecomposeSoulFrag` (0x18F78EC) touches SoulfragDecomposeItemDic
    with no null check at all, so omitting it is a NullReferenceException out of
    PanelSoulFrag.GetBrowsableDecomposeItemList the moment a mirror is selected on the
    DISMANTLE tab -- seen on device. The char-rarity multipliers are the same pattern one
    step further in, reached once the dictionary is non-empty.
    """
    import json
    d = json.loads(ps.game_rule_json())

    assert "soulfrag_decompose_item_dic" in d, sorted(d)
    assert isinstance(d["soulfrag_decompose_item_dic"], dict)

    for key in ps.GAME_RULE_DECOMPOSE_MAGNIFICATION_KEYS:
        vals = d.get(key)
        assert isinstance(vals, list), (key, vals)
        # Indexed by charRarity - 1, and the function bails unless that is 0..4.
        assert len(vals) >= 5, (key, len(vals))
        # **List<int>, not the List<string> decimals the enhance tables use** -- read
        # with a 4-byte stride and then tested `< 1`.
        assert all(isinstance(v, int) and not isinstance(v, bool) and v >= 1
                   for v in vals), (key, vals)

    # The enhance side really is strings; keeping both shapes straight is the point.
    for key in ("soulfrag_enhance_coin_char_rarity_magnification",
                "soulfrag_enhance_dust_char_rarity_magnification",
                "soulfrag_enhance_item_char_rarity_magnification"):
        assert all(isinstance(v, str) for v in d[key]), (key, d[key])
    print("game-rule null-guard keys OK")


def test_char_sort_list_is_long_enough():
    """PanelSoulFrag.ResetSoulShardComparer indexes sort_list at 14 for the FUSE tab."""
    st = fresh_state()
    assert len(ps.char_sort_list(st)) >= 15, len(ps.char_sort_list(st))
    # An account saved before the count went up must be padded on READ, or the login
    # sync keeps publishing the short list and only new accounts get fixed.
    st["sort_list"] = ["0_1"] * 14
    assert len(ps.char_sort_list(st)) == ps.CHAR_SORT_SLOTS >= 15
    import json
    assert len(json.loads(ps.char_json(st))["sort_list"]) >= 15
    print("sort_list padding OK")


def test_game_rule_advertises_the_count():
    """get_SoulfragTransmuteDefaultNum reads the literal string key "4"."""
    import json
    d = json.loads(ps.game_rule_json())
    assert d["soulfrag_transmute_num"]["4"] == ps.SOULFRAG_TRANSMUTE_NUM, \
        d["soulfrag_transmute_num"]
    assert d["soulfrag_transmute_cost"] == ps.soulfrag_transmute_cost()
    print("game-rule sync OK")


if __name__ == "__main__":
    test_refund_formula()
    test_dismantle()
    test_refusals()
    test_unequips()
    test_transmute_pool()
    test_fuse_uniform_is_deterministic()
    test_fuse_mixed_stays_legal()
    test_fuse_refusals()
    test_push_carries_only_what_changed()
    test_game_rule_carries_the_unguarded_keys()
    test_char_sort_list_is_long_enough()
    test_game_rule_advertises_the_count()
    print("all OK")

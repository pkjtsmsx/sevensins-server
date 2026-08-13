"""Every ★5 cast's Soulmirrors must be obtainable from some banner.

The bug this guards: boxes existed for the Sins (alignment 100) and the Virtues (101)
but not the Riders (102), so five ★5 characters owned a full set of mirrors at every
grade with no way to get any of them.

Run with `python3 test_soulmirror_gacha.py` from server/.
"""
import collections, json, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import battle as bt
import player_state as ps
from player_state import gacha as G
from player_state.core import _default, _seed_roster

SOULFRAG_ACTIONS = range(101, 110)


def item_row(iid):
    return bt.dd.row("item", int(iid)) or {}


def mirror_owners():
    """-> {charId: {grade, ...}} for every character that owns a Soulmirror."""
    owners = collections.defaultdict(set)
    for iid, row in (bt.dd.rows("item") or {}).items():
        if int(row.get("_action") or 0) in SOULFRAG_ACTIONS:
            owners[int(row.get("_param3") or 0)].add(int(row.get("_param2") or 0))
    return owners


def char_row(cid):
    return bt.dd.row("char", int(cid)) or {}


def test_every_five_star_cast_has_a_banner():
    """The invariant: no ★5 character's mirrors are stranded."""
    covered = set(G.SOULMIRROR_GACHA_BOXES.values())
    stranded = []
    for cid, _grades in mirror_owners().items():
        row = char_row(cid)
        if row.get("_rarity") == 5 and row.get("_alignment") not in covered:
            stranded.append((cid, row.get("_name_en"), row.get("_alignment")))
    assert not stranded, f"★5 casts with no Soulmirror banner: {stranded}"
    print(f"every ★5 cast is covered (alignments {sorted(covered)}) OK")


def test_rider_pool_covers_all_five():
    pool = G._soulmirror_gacha_pool(102)
    assert pool, "the Rider pool is empty"
    chars = {int(item_row(i).get("_param3") or 0)
             for bucket in pool.values() for i in bucket}
    expected = {cid for cid, _g in mirror_owners().items()
                if char_row(cid).get("_alignment") == 102}
    assert chars == expected, (sorted(chars), sorted(expected))
    # Mirrors exist at every grade, so the weighted table has real buckets to draw from.
    assert {5, 4, G.SOULMIRROR_GACHA_FALLBACK_GRADE} <= set(pool), sorted(pool)
    print("Rider pool covers", sorted(chars), "OK")


def test_rider_draws_stay_in_the_pool():
    st = _default(1000001)
    _seed_roster(st)
    rows = G._draw_soulmirrors(st, 60, 102)
    assert len(rows) == 60, len(rows)
    riders = {cid for cid, _g in mirror_owners().items()
              if char_row(cid).get("_alignment") == 102}
    for obj_type, iid, amount, grade in rows:
        row = item_row(iid)
        assert int(row.get("_action") or 0) in SOULFRAG_ACTIONS, row
        assert int(row.get("_param3") or 0) in riders, row
        assert amount == 1 and grade in (5, 4, G.SOULMIRROR_GACHA_FALLBACK_GRADE)
    print("60 Rider draws all in-pool OK")


def test_banner_is_published_and_well_formed():
    st = _default(1000001)
    st["gacha_count"] = 1                    # past the tutorial box
    boxes = json.loads(ps.gacha_json(st))
    by_id = {b["id"]: b for b in boxes}
    assert 1006 in by_id, sorted(by_id)
    box = by_id[1006]

    # Soulmirrors are an ITEM gacha: char_only would make ShowGachaAnim skip the
    # summon animation straight to the results screen.
    assert box["is_char_only"] is False, box["is_char_only"]
    # Sprite ids must have real `sprite` rows, or the banner renders as dead
    # fallback art with a tab that does nothing (the 761/771 and 766/776 mistake).
    for key in ("img", "banner"):
        assert bt.dd.row("sprite", box[key]), (key, box[key])
    # A draw-limited box (left_cnt > 0) may not be last, and this one is last.
    assert box["left_cnt"] <= 0, box["left_cnt"]
    # Gems are on every banner, so the scroll is not the only way to pull.
    cats = {row[1] for row in box["cost_tbl"]}
    assert cats == {1, 2}, box["cost_tbl"]
    scrolls = {row[2] for row in box["cost_tbl"] if row[1] == 2}
    assert scrolls == {5829}, scrolls
    assert all(row[3] > 0 for row in box["cost_tbl"]), box["cost_tbl"]

    # Sort order must stay dense and unique or tabs collide.
    sorts = [b["sort"] for b in boxes]
    assert sorted(sorts) == list(range(1, len(boxes) + 1)), sorts
    # Every banner uses a distinct tab sprite, else two tabs look identical.
    tabs = [b["banner"] for b in boxes]
    assert len(set(tabs)) == len(tabs), tabs
    print("banner 1006 published and well-formed OK")


if __name__ == "__main__":
    test_every_five_star_cast_has_a_banner()
    test_rider_pool_covers_all_five()
    test_rider_draws_stay_in_the_pool()
    test_banner_is_published_and_well_formed()
    print("all OK")

"""EVERY character's Soulmirrors must be obtainable from some banner.

The bug this guards, in two halves: boxes existed for the Sins (alignment 100) and the
Virtues (101) but not the Riders (102), so five ★5 characters owned a full set of
mirrors with no way to get any of them; and the ★4/★3 casts (103/104, 49 characters)
had no banner of their own either, so they ride on all three pools instead.

Run with `python3 test_soulmirror_gacha.py` from server/.
"""
import collections, json, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import os
# **Point the account store at a THROWAWAY DIR before player_state is imported.**
# This suite drives real server paths (battle_end_reward, battle_replies) and those
# call ps.save(), so without this it writes a fresh default account straight over the
# player's real save. That is not hypothetical -- it happened, and cost a live account.
import tempfile as _tempfile
os.environ["SEVENSINS_ACCOUNTS"] = _tempfile.mkdtemp(prefix="sevensins-test-")

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


def drawable_chars():
    """Every character reachable from any published Soulmirror banner."""
    out = set()
    for alignment in G.SOULMIRROR_GACHA_BOXES.values():
        for bucket in G._soulmirror_gacha_pool(alignment).values():
            out |= {int(item_row(i).get("_param3") or 0) for i in bucket}
    return out


def test_no_character_is_stranded():
    """THE invariant: every character that owns a mirror can be drawn, at any rarity."""
    reachable = drawable_chars()
    stranded = []
    for cid in mirror_owners():
        row = char_row(cid)
        if not row:
            continue                     # not a real character -- see below
        if cid not in reachable:
            stranded.append((cid, row.get("_name_en"), row.get("_rarity"),
                             row.get("_alignment")))
    assert not stranded, f"characters with unobtainable Soulmirrors: {stranded}"

    owners = {cid for cid in mirror_owners() if char_row(cid)}
    assert reachable == owners, sorted(owners - reachable)
    by_rarity = collections.Counter(char_row(c).get("_rarity") for c in reachable)
    print(f"all {len(reachable)} mirror-owning characters are drawable "
          f"(by rarity: {dict(sorted(by_rarity.items()))}) OK")


def test_ownerless_mirrors_stay_out():
    """Mirrors whose `_param3` names no character must never be drawable.

    `_param3` 0 (60 non-character-bound mirrors) and 20851/20861/20911/20931/20951 have
    no `char` row, and the Soulmirror panel filters every list by the cast being viewed
    while GetTransmutePredictText calls DesignCharForm.GetRow(charId) outright -- so
    granting one hands the client a character it cannot look up.
    """
    ghosts = {cid for cid in mirror_owners() if not char_row(cid)}
    assert ghosts, "expected the known ownerless mirror ids to still exist"
    leaked = ghosts & drawable_chars()
    assert not leaked, f"ownerless mirrors are drawable: {sorted(leaked)}"
    print(f"{len(ghosts)} ownerless mirror groups correctly excluded OK")


def test_shared_cast_rides_every_banner():
    """The ★4/★3 casts have no banner of their own, so all three must carry them."""
    shared = G.soulmirror_shared_chars()
    assert shared, "the shared pool is empty"
    assert {char_row(c).get("_alignment") for c in shared} == {103, 104}
    for alignment in G.SOULMIRROR_GACHA_BOXES.values():
        pool = G._soulmirror_gacha_pool(alignment)
        chars = {int(item_row(i).get("_param3") or 0)
                 for bucket in pool.values() for i in bucket}
        assert shared <= chars, (alignment, sorted(shared - chars)[:5])
        # ...and the banner still leads with its own cast.
        own = {cid for cid, a in G._mirror_owning_chars().items() if a == alignment}
        assert own <= chars, (alignment, sorted(own - chars)[:5])
    print(f"{len(shared)} shared characters ride all "
          f"{len(G.SOULMIRROR_GACHA_BOXES)} banners OK")


def test_rider_pool_covers_all_five():
    pool = G._soulmirror_gacha_pool(102)
    assert pool, "the Rider pool is empty"
    chars = {int(item_row(i).get("_param3") or 0)
             for bucket in pool.values() for i in bucket}
    riders = {cid for cid, _g in mirror_owners().items()
              if char_row(cid).get("_alignment") == 102}
    assert riders <= chars, sorted(riders - chars)
    assert len(riders) == 5, sorted(riders)
    # Mirrors exist at every grade, so the weighted table has real buckets to draw from.
    assert {5, 4, G.SOULMIRROR_GACHA_FALLBACK_GRADE} <= set(pool), sorted(pool)
    print("Rider pool covers", sorted(riders), "OK")


def test_draws_stay_in_the_pool():
    """Nothing outside the banner's own pool can come out of it."""
    st = _default(1000001)
    _seed_roster(st)
    for alignment in G.SOULMIRROR_GACHA_BOXES.values():
        allowed = {int(item_row(i).get("_param3") or 0)
                   for bucket in G._soulmirror_gacha_pool(alignment).values()
                   for i in bucket}
        rows = G._draw_soulmirrors(st, 60, alignment)
        assert len(rows) == 60, len(rows)
        for _obj_type, iid, amount, grade in rows:
            row = item_row(iid)
            assert int(row.get("_action") or 0) in SOULFRAG_ACTIONS, row
            assert int(row.get("_param3") or 0) in allowed, (alignment, row)
            assert char_row(int(row.get("_param3") or 0)), row
            assert amount == 1 and grade in (5, 4, G.SOULMIRROR_GACHA_FALLBACK_GRADE)
    print("60 draws per banner, all in-pool and character-backed OK")


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
    test_no_character_is_stranded()
    test_ownerless_mirrors_stay_out()
    test_shared_cast_rides_every_banner()
    test_rider_pool_covers_all_five()
    test_draws_stay_in_the_pool()
    test_banner_is_published_and_well_formed()
    print("all OK")

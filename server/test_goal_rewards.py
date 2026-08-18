#!/usr/bin/env python3
"""Goal-chain rewards actually arrive.

Reported 2026-08-18: "starshard rewards from the goal, specifically stage 29's goal,
are not being given ... despite the reward popup showing".

The Netherworld Note's Temple steps pay `_action 2` starshard BOXES. `GetItemSpace` has
no case for `_action 2`, so a box filed in the bag is invisible and unopenable -- the
popup named it and the player got nothing. Four separate defects were behind it, all
the same mistake: **trusting a guessed `_param1` encoding over the item's own name.**

  1. `random_rune_box` matched only `^52(\\d)(\\d)$`. 52 is Endearment's prefix, so
     **210 of the 240 per-suit boxes did not match at all**.
  2. For the 30 that did, it read the last digit as the SUIT when it is the SLOT --
     item 1014 "Random ★3 Endearment" handed over a ★3 **Slayer**.
  3. `RUNE_BUNDLES` listed 311..314 as `(None, star)` but the second field is a GRADE
     (`_param2` = rank + 1), so "Random ★3 LR Starshard" rolled any star at SR.
  4. Resolving a box to a real shard id then called `grant_reward`, which BAGS it. A
     starshard is an equipment INSTANCE and has to be rolled into storage 2.

The decoders are name-driven now. `_param1` is kept only as a cross-check, and this
file asserts why: it encodes suit+star for the first eight suits and then degenerates
into a flat counter at item 1241, where the name keeps working.

    python3 test_goal_rewards.py
"""
import os
import random
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

_TMP = tempfile.mkdtemp(prefix="sevensins-goalreward-test-")
os.environ["SEVENSINS_ACCOUNTS"] = _TMP

import battle as bt                                     # noqa: E402
import player_state as ps                               # noqa: E402
from player_state import shop as sh                      # noqa: E402

_fail = 0
# The Note's Temple steps, and what each is supposed to hand over.
TEMPLE_GOALS = {
    31028: ("★3 Endearment", 3, "Endearment", None),
    31036: ("Random ★3 Endearment", 3, "Endearment", None),
    31043: ("Random ★3 Endearment", 3, "Endearment", None),
    31048: ("Random ★3 Chaos", 3, "Chaos", None),
    31054: ("Random ★3 Chaos", 3, "Chaos", None),
    31061: ("Random ★3 LR Starshard", 3, None, 4),      # any suit, LR
    31068: ("★3 LR Starshard", 3, None, 4),             # selector, any suit, LR
}


def check(name, cond, detail=""):
    global _fail
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        _fail += 1


def suits():
    return {r["_id"]: r.get("_suitName_en")
            for r in bt.dd.rows("equip_suit").values()}


def shard_facts(item_id):
    """(star, rank, suit name) for a granted shard id."""
    row = bt.dd.row("item", item_id) or {}
    eq = bt.dd.row("equipment", row.get("_param1")) or {}
    return item_id % 10, (item_id // 10) % 10, suits().get(eq.get("_suitID"))


def check_the_box_decode_matches_every_name():
    """350 boxes decode; not one may disagree with its own printed name."""
    names, bad, n = suits(), [], 0
    for iid, row in bt.dd.rows("item").items():
        got = sh.random_rune_box(iid)
        if not got:
            continue
        n += 1
        star, suit = got
        label = row.get("_itemName_en") or ""
        star_txt = "I" if star == 1 else str(star)
        if names.get(suit) not in label or f"★{star_txt}" not in label:
            bad.append((iid, got, label))
    check("every per-suit box decodes to its own name's suit and star",
          not bad, f"{len(bad)} bad, e.g. {bad[:3]}")
    check("  ...and that is far more than the 30 the old regex reached",
          n > 300, str(n))
    # All eight prefix families, not just Endearment's 52.
    covered = {sh.random_rune_box(i)[1] for i in range(1001, 1241)
               if sh.random_rune_box(i)}
    check("all eight slotted-box suits decode, not only Endearment",
          covered == set(range(1, 9)), str(sorted(covered)))


def check_param1_is_the_unreliable_one():
    """Pins WHY the decoders read names: `_param1` stops encoding at item 1241."""
    agree = disagree = 0
    for iid, row in bt.dd.rows("item").items():
        got = sh.random_rune_box(iid)
        if not got:
            continue
        by_p1 = sh._box_param1_star_suit(row.get("_param1") or 0)
        if by_p1 is None:
            continue
        agree += (by_p1 == got)
        disagree += (by_p1 != got)
    check("`_param1` corroborates the name on the range it encodes", agree > 250,
          str(agree))
    check("  ...but not everywhere -- which is why the name wins", disagree > 0,
          str(disagree))
    # The specific cliff: 1241 is "Random ★3 Innocence" (suit 9), and by formula
    # `_param1` 6666 reads as Devotee ★6.
    row = bt.dd.row("item", 1241) or {}
    if row:
        check("item 1241 is the cliff: name says Innocence...",
              "Innocence" in (row.get("_itemName_en") or ""),
              str(row.get("_itemName_en")))
        check("  ...while `_param1` would say Devotee",
              sh._box_param1_star_suit(row.get("_param1"))[1] == 8,
              str(sh._box_param1_star_suit(row.get("_param1"))))
        check("  ...and the decoder follows the name",
              sh.random_rune_box(1241)[1] == 9, str(sh.random_rune_box(1241)))


def check_any_suit_boxes():
    for iid, want in ((291, (1, None)), (296, (6, None)), (311, (1, 2)),
                      (312, (2, 3)), (313, (3, 4)), (314, (4, 4))):
        row = bt.dd.row("item", iid)
        if not row:
            continue
        check(f"{iid} '{row.get('_itemName_en')}' decodes to {want}",
              sh.starshard_any_suit_box(iid) == want,
              str(sh.starshard_any_suit_box(iid)))
    pool = sh.rune_any_suit_pool(3, 4)
    check("an any-suit LR pool is non-empty", bool(pool))
    check("  ...and every member really is ★3 LR",
          all(i % 10 == 3 and (i // 10) % 10 == 4 for i in pool),
          str([i for i in pool if i % 10 != 3 or (i // 10) % 10 != 4][:3]))


def check_pool_excludes_event_variants():
    """Elements 720/726/727 carry a `_suitID` and used to leak in: handing over a
    fixed-stat "LR Chaos Starshard II_ATK" for a plain "★3 Chaos" box is the wrong
    item."""
    pool = sh.rune_star_suit_pool(3, 2)
    check("a per-suit pool is non-empty", bool(pool))
    outside = [i for i in pool if i // 1000 not in sh.STARSHARD_ELEMENT_BAND]
    check("  ...and contains only canonical shard elements", not outside,
          str(outside[:3]))


def check_every_temple_goal_pays_a_real_shard():
    for qid, (label, want_star, want_suit, want_rank) in TEMPLE_GOALS.items():
        row = bt.dd.row("quest", qid)
        if not row:
            continue
        st = ps.load(1000001)
        st["backpack"]["2"] = {}
        before_bag = dict(st["backpack"].get("1", {}))
        rewards, _chars = ps.complete_quests(st, [qid])
        got = list(st["backpack"].get("2", {}).values())
        if not got:
            check(f"q{qid} ('{label}') hands over a starshard", False, "NOTHING")
            continue
        star, rank, suit = shard_facts(got[0]["iid"])
        check(f"q{qid} ('{label}') hands over a starshard", True)
        check(f"  ...at ★{want_star}", star == want_star, f"★{star}")
        if want_suit:
            check(f"  ...of the {want_suit} set", suit == want_suit, str(suit))
        if want_rank is not None:
            check(f"  ...at rank {want_rank}", rank == want_rank, str(rank))
        # The popup must name the shard actually granted, not the box.
        check("  ...and the popup reports the real shard",
              rewards and rewards[0][1] == got[0]["iid"],
              f"{rewards} vs {got[0]['iid']}")
        # The box itself must never end up as a bag stack.
        src = row.get("_item_id")
        bagged = [e for e in st["backpack"].get("1", {}).values()
                  if e.get("iid") == src and str(src) not in
                  {str(v.get("iid")) for v in before_bag.values()}]
        check("  ...and the unopenable box is NOT left in the bag", not bagged,
              str(bagged[:1]))


def check_raw_shard_is_an_instance_not_a_stack():
    """grant_reward is the choke point every path goes through."""
    st = ps.load(1000001)
    st["backpack"]["2"] = {}
    bucket = ps.grant_reward(st, 201443, 1)          # ★3 LR Endearment IV
    check("granting a raw shard id reports the equipment bucket",
          bucket == "equipment", str(bucket))
    check("  ...and creates an instance in storage 2",
          len(st["backpack"].get("2", {})) == 1,
          str(len(st["backpack"].get("2", {}))))
    inst = list(st["backpack"]["2"].values())[0]
    check("  ...with a uid and rolled attributes",
          inst.get("uid") and inst.get("attr", {}).get("lv") is not None, str(inst.keys()))
    # item_bucket drives which sync gets pushed and MUST agree with grant_reward.
    check("item_bucket agrees, so the right sync is pushed",
          ps.item_bucket(201443) == "equipment", ps.item_bucket(201443))


def check_no_goal_pays_into_the_void():
    """Sweep EVERY claimable goal: nothing may report a reward and grant nothing.

    This is the general form of the reported bug, and the check that would have caught
    it without a player noticing.
    """
    rows = bt.dd.rows("quest")
    void = []
    for qid, row in rows.items():
        if row.get("_type") != 1 or not row.get("_item_id"):
            continue
        if int(row.get("_item_cnt") or 0) < 1:
            continue
        st = ps.load(1000001)
        for k in ("1", "2", "3", "4"):
            st["backpack"][k] = {}
        before = (dict(st["currency"]), {k: v.get("energy") for k, v in st["energy"].items()},
                  len(st["roster"]))
        rewards, chars = ps.complete_quests(st, [qid])
        moved = (
            any(st["backpack"].get(k) for k in ("1", "2", "3", "4"))
            or st["currency"] != before[0]
            or {k: v.get("energy") for k, v in st["energy"].items()} != before[1]
            or len(st["roster"]) != before[2]
            or bool(chars))
        if rewards and not moved:
            void.append((qid, row.get("_item_id"),
                         (bt.dd.row("item", row.get("_item_id")) or {}).get("_itemName_en")))
    check("no goal reports a reward it does not actually grant",
          not void, f"{len(void)} do, e.g. {void[:5]}")


def main():
    for fn in (check_the_box_decode_matches_every_name,
               check_param1_is_the_unreliable_one, check_any_suit_boxes,
               check_pool_excludes_event_variants,
               check_every_temple_goal_pays_a_real_shard,
               check_raw_shard_is_an_instance_not_a_stack,
               check_no_goal_pays_into_the_void):
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

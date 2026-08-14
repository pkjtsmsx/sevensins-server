#!/usr/bin/env python3
"""Selectors (`_action 7`) must answer their own Drop Info request and pay out.

Two things out of the binary drive this whole file:

  * `PanelItemInfo.OnPanelDirty` (0x15AA038) routes the Drop Info request by `_action`:
    `_action 2` -> PlayerBackpack.RequesQueryBoxList (cmd 129), but `_action 7` ->
    PlayerShop.RequesQueryCouponList (**cmd 273**), answered by HandleQueryCouponRply
    (**cmd 529**). We only ever answered 129, so tapping a selector's magnifier left a
    modal overlay up with nothing to dismiss it -- the dark-screen lockup.
  * `PlayerBackpack.GetItemSpace` (0x18EE554) has no case for `_action 7`, so a
    selector can NEVER sit in the bag. Granting one as an item would silently vanish;
    it has to resolve to something real at grant time.

    python3 test_selectors.py

Uses its own SEVENSINS_ACCOUNTS tempdir so it never touches real save data.
"""
import os
import random
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

_TMP = tempfile.mkdtemp(prefix="sevensins-selector-test-")
os.environ["SEVENSINS_ACCOUNTS"] = _TMP        # before player_state imports

import battle as bt           # noqa: E402
import player_state as ps     # noqa: E402
from player_state.core import _default, _seed_roster   # noqa: E402
from player_state.shop import (                        # noqa: E402
    COST_MANA_CRYSTAL,
    COST_PRIME_MANA_CRYSTAL,
    SELECTOR_ACTION,
    SIN_BUNREI,
    VIRTUE_BUNREI,
    DEFAULT_SHOP_GOODS,
    is_selector,
    is_sellable,
    selector_pool,
)

_fail = 0

SIN_BOX = 1432            # "★5 Sin Bunrei Selector Box"
VIRTUE_BOX = 1500002      # "Virtue Bunrei Selector Box"
AWAKER_MIRROR_BOX = 1382  # "★4 Awaker Soulmirror+ Selector Box" -- the one that locked


def check(name, cond, detail=""):
    global _fail
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        _fail += 1


def fresh():
    st = _default(1000001)
    _seed_roster(st)
    return st


def main():
    # ---- what a selector IS ------------------------------------------------
    for iid in (SIN_BOX, VIRTUE_BOX, AWAKER_MIRROR_BOX):
        row = bt.dd.row("item", iid) or {}
        check(f"{iid} is `_action 7`", row.get("_action") == SELECTOR_ACTION,
              str(row.get("_action")))

    # ---- Drop Info ---------------------------------------------------------
    # This is the payload of shop cmd 273 -> 529. The live popup for 1432 lists the
    # seven Sins' Bunrei, so ours must too.
    got = ps.box_contents(SIN_BOX)
    check("the Sin box offers seven choices", len(got) == 7, str(got))
    check("they are the seven Sins' Bunrei",
          [r[0] for r in got] == SIN_BUNREI, str(got))
    check("every row is [itemId, count]",
          all(len(r) == 2 and r[1] == 1 for r in got), str(got))
    # GetIconDataInBox reads each row positionally, so the ids must be real item rows.
    check("every choice resolves to a real item",
          all(bt.dd.row("item", r[0]) for r in got))
    check("and each is a `_action 1` cast item",
          all((bt.dd.row("item", r[0]) or {}).get("_action") == 1 for r in got))

    check("the Virtue box offers the seven Virtues",
          [r[0] for r in ps.box_contents(VIRTUE_BOX)] == VIRTUE_BUNREI)

    # **A selector we do not model must still ANSWER.** An empty list opens the popup
    # with an empty grid, which dismisses; no reply at all is the lockup.
    check("an unmodelled selector answers with an empty list",
          ps.box_contents(AWAKER_MIRROR_BOX) == [],
          str(ps.box_contents(AWAKER_MIRROR_BOX)))
    check("a plain item is not a selector", not is_selector(202))
    check("selector_pool of an unknown id is empty", selector_pool(999999) == [])

    # ---- what may be sold --------------------------------------------------
    check("a modelled selector is sellable", is_sellable(SIN_BOX))
    check("an unmodelled selector is NOT sellable", not is_sellable(AWAKER_MIRROR_BOX))
    check("ordinary items stay sellable", is_sellable(202) and is_sellable(736))

    # Every card in every shop must be payable-for.
    for sid, rows in DEFAULT_SHOP_GOODS.items():
        bad = [r[0] for r in rows if not is_sellable(r[5])]
        check(f"shop {sid} lists nothing unpayable", not bad, str(bad))

    # ---- granting ----------------------------------------------------------
    # A selector cannot be held, so buying one must produce the CAST, not a bag entry.
    st = fresh()
    roster_before = len(st["roster"])
    bag_before = sum(len(v) for v in st["backpack"].values())
    rng = random.Random(7)
    uids, popup_id, popup_cnt = ps.grant_goods(st, SIN_BOX, 1, rng)

    check("one cast was granted", len(uids) == 1, str(uids))
    check("the roster grew", len(st["roster"]) == roster_before + 1)
    check("nothing landed in the bag",
          sum(len(v) for v in st["backpack"].values()) == bag_before)
    check("the popup names one of the offered Bunrei", popup_id in SIN_BUNREI,
          str(popup_id))
    check("the popup counts one", popup_cnt == 1, str(popup_cnt))
    # `EnqueItemPopupInfo` strips `_action` 2/4/9; naming the SELECTOR would have been
    # stripped as well, so the reveal must name the `_action 1` item.
    check("the popup item survives the popup mask",
          (bt.dd.row("item", popup_id) or {}).get("_action") == 1)
    granted = st["roster"][uids[0]]
    check("the cast is the one the popup named",
          granted["id"] == (bt.dd.row("item", popup_id) or {}).get("_param1"),
          f"{granted} vs {popup_id}")

    # Buying several rolls several.
    st2 = fresh()
    uids2, _pid, cnt2 = ps.grant_goods(st2, SIN_BOX, 3, random.Random(11))
    check("buying three grants three casts", len(uids2) == 3, str(uids2))
    check("and reports three", cnt2 == 3, str(cnt2))

    # ---- the storefront ----------------------------------------------------
    altar = DEFAULT_SHOP_GOODS["3"]
    by_item = {r[5]: r for r in altar}
    check("the Soul Altar sells the Sin selector", SIN_BOX in by_item)
    check("the Soul Altar sells the Virtue selector", VIRTUE_BOX in by_item)
    for iid in (SIN_BOX, VIRTUE_BOX):
        if iid in by_item:
            check(f"{iid} costs Prime Mana Crystal",
                  by_item[iid][7] == COST_PRIME_MANA_CRYSTAL, str(by_item[iid][7]))
    check("the ★5 Awaker Orb card costs 70 Mana Crystal",
          any(r[7] == COST_MANA_CRYSTAL and r[8] == 70 for r in altar),
          str([(r[0], r[7], r[8]) for r in altar]))
    # Transcender Gremlins (items 111..115) carry no purchase cap.
    gremlins = [r for r in altar if r[5] in (111, 112, 113, 114, 115)]
    check("all five Gremlin cards are present", len(gremlins) == 5, str(len(gremlins)))
    check("and none of them is capped", all(r[4] == 0 for r in gremlins),
          str([(r[0], r[4]) for r in gremlins]))

    # A real purchase of an uncapped card must go through repeatedly.
    st3 = fresh()
    gid = gremlins[0][0]
    ps.grant_reward(st3, gremlins[0][7], 50)      # stock up on Pieces
    oks = [ps.buy_shop_goods(st3, gid, 1)[0] for _ in range(6)]
    check("an uncapped card can be bought over and over", all(oks), str(oks))

    print("\n" + ("ALL PASSED" if not _fail else f"{_fail} FAILED"))
    return 1 if _fail else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        import shutil
        shutil.rmtree(_TMP, ignore_errors=True)

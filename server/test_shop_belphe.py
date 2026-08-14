#!/usr/bin/env python3
"""Belphe's Grocery Store (shop 2), transcribed from footage of the live game.

The goods list was live-ops data -- `shop_goods` in the pack is a 44-byte stub with a
null script -- so this table is authored, and these checks are what keep it honest:
every id must exist in the design data, and the three payout kinds (plain item, bundle
box, cast) must each hand over the right thing.

    python3 test_shop_belphe.py
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

_TMP = tempfile.mkdtemp(prefix="sevensins-shop-test-")
os.environ["SEVENSINS_ACCOUNTS"] = _TMP

import battle as bt           # noqa: E402
import player_state as ps     # noqa: E402
from player_state import shop as sh                    # noqa: E402
from player_state.core import _default, _seed_roster   # noqa: E402

_fail = 0
SHOP = "2"


def check(name, cond, detail=""):
    global _fail
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        _fail += 1


def fresh():
    st = _default(1000001)
    _seed_roster(st)
    st["currency"]["1"] = 10 ** 7          # diamonds
    st["currency"]["64"] = 10 ** 7         # guild pt
    ps.grant_item(st, sh.COST_MEDAL, 10 ** 6)
    return st


def main():
    goods = sh.DEFAULT_SHOP_GOODS[SHOP]

    # ---- table shape ------------------------------------------------------
    check("every row has 21 fields", all(len(r) == 21 for r in goods))
    ids = [r[0] for r in goods]
    check("goods ids are unique", len(set(ids)) == len(ids))
    check("the four in-game tabs are present",
          {r[9] for r in goods} == {sh.FILTER_COINS, sh.FILTER_ITEMS,
                                    sh.FILTER_PVP, sh.FILTER_GUILD},
          str(sorted({r[9] for r in goods})))
    # The Diamonds tab is real-money IAP and cannot be expressed as CostID/CostCount.
    check("no IAP tab is advertised",
          all(r[9] != sh.FILTER_DIAMONDS for r in goods))

    # Every id the table names must be real, or the card renders as fallback art.
    for r in goods:
        gid, item_id, cost_id = r[0], r[5], r[7]
        if not bt.dd.row("item", item_id):
            check(f"goods {gid} sells a real item", False, f"item {item_id}")
        if not bt.dd.row("item", cost_id):
            check(f"goods {gid} costs a real item", False, f"item {cost_id}")
    check("all item and cost ids exist", True)

    # Costs are the three the storefront quotes, nothing invented.
    check("costs are diamonds / medals / guild pt",
          {r[7] for r in goods} == {sh.COST_DIAMOND, sh.COST_MEDAL, sh.COST_GUILD_PT},
          str(sorted({r[7] for r in goods})))
    # A capped card must say what it resets on, or it caps forever.
    for r in goods:
        if r[4] and r[4] < sh.GOODS_DEFAULT_LIMIT:
            check(f"capped goods {r[0]} has a reset cycle", r[3] != sh.RESET_NONE,
                  f"limit {r[4]} reset {r[3]}")
    check("capped cards all reset", True)

    # ---- payout kind 1: a bundle box pays what its card promises ----------
    st = fresh()
    coins_before = int(st["currency"]["16"])
    ok, sid, why, new = ps.buy_shop_goods(st, 2101, 1)      # Deluxe Coin Box, 490 gems
    check("bundle purchase succeeds", ok, why)
    check("bundle paid coins, not a box",
          int(st["currency"]["16"]) == coins_before + 1500000,
          f"{coins_before} -> {st['currency']['16']}")
    check("no invisible box reached the bag",
          not any(e.get("iid") == 736 for e in st["backpack"].get("1", {}).values()))
    check("bundle grants no cast", new == [], str(new))

    # ---- payout kind 2: a cast card grants the character ------------------
    st = fresh()
    before = len(st["roster"])
    ok, sid, why, new = ps.buy_shop_goods(st, 2301, 1)      # ★5 War Thyrza, 38000 medals
    check("cast purchase succeeds", ok, why)
    check("one cast granted", len(new) == 1, str(new))
    check("roster grew", len(st["roster"]) == before + 1)
    entry = st["roster"][new[0]]
    row = bt.dd.row("item", 110435) or {}
    check("granted the cast the card names", entry["id"] == row.get("_param1"),
          f"{entry} vs param1 {row.get('_param1')}")
    check("granted at ★5", entry["star"] == 5, str(entry))
    check("the cast did not land in the bag",
          not any(e.get("iid") == 110435 for e in st["backpack"].get("1", {}).values()))

    # ---- payout kind 3: a plain item still behaves ------------------------
    st = fresh()
    essence_before = ps.item_count(st, 30)
    ok, sid, why, new = ps.buy_shop_goods(st, 2409, 1)      # Soul Essence x500
    check("plain item purchase succeeds", ok, why)
    check("plain item reached the bag", ps.item_count(st, 30) == essence_before + 500,
          f"{essence_before} -> {ps.item_count(st, 30)}")

    # ---- caps and affordability ------------------------------------------
    st = fresh()
    ok, _s, _w, _n = ps.buy_shop_goods(st, 2201, 1)         # daily stamina, limit 1
    check("first capped buy works", ok, _w)
    ok2, _s, why2, _n = ps.buy_shop_goods(st, 2201, 1)
    check("second exceeds the daily cap", not ok2, why2)

    st = fresh()
    st["currency"]["1"] = 0
    ok3, _s, why3, _n = ps.buy_shop_goods(st, 2101, 1)
    check("cannot buy without diamonds", not ok3, why3)

    # ---- the bought payload uses the WIRE keys ----------------------------
    # GoodsBuyData is {cnt, reset}, not {Count, Reset}. Getting this wrong stops the
    # cap counters updating AND makes HandleShopBuy bail before the purchase popup.
    st = fresh()
    ps.buy_shop_goods(st, 2103, 2)
    wire = json.loads(ps.shop_bought_json(st, 2))
    check("bought data is keyed by goods id", "2103" in wire, str(wire))
    check("bought data uses cnt/reset", set(wire["2103"]) == {"cnt", "reset"},
          str(wire["2103"]))
    check("bought count is the real count", wire["2103"]["cnt"] == 2, str(wire["2103"]))

    # ---- the popup must name a DISPLAYABLE item ---------------------------
    # EnqueItemPopupInfo strips items whose _action is in the mask 0x214 (bits 2/4/9)
    # and returns false on an empty list, which kills the popup silently. Bundles are
    # _action 2, so the reply has to report the payout instead of the box.
    st = fresh()
    import battle as _bt
    MASK_DROPPED = {2, 4, 9}
    for r in goods:
        gid = r[0]
        rid, rcnt = ps.goods_reward(st, gid, 1)
        row = _bt.dd.row("item", rid) or {}
        act = row.get("_action")
        bad = act in MASK_DROPPED or (act == 0 and row.get("_class") == 2)
        if bad:
            check(f"goods {gid} popup item is displayable", False,
                  f"item {rid} action {act} class {row.get('_class')}")
        if rcnt <= 0:
            check(f"goods {gid} popup count is positive", False, str(rcnt))
    check("every card reports a displayable reward", True)
    check("the coin box reports Mira, not the box",
          ps.goods_reward(st, 2101, 1) == (2, 1500000),
          str(ps.goods_reward(st, 2101, 1)))
    # A cast must NOT be resolved away -- action 1 survives the mask and its tweenPopup
    # flag is what plays the reveal.
    check("a cast card still reports the cast item",
          ps.goods_reward(st, 2301, 1) == (110435, 1),
          str(ps.goods_reward(st, 2301, 1)))

    # ---- the sync payload -------------------------------------------------
    st = fresh()
    payload = ps.shop_goods_json(st, 2)
    check("goods sync has three strargs", len(payload) == 3, str(len(payload)))
    sent = json.loads(payload[1])
    check("sync carries the whole table", len(sent) == len(goods),
          f"{len(sent)} vs {len(goods)}")

    print("\n" + ("ALL PASSED" if not _fail else f"{_fail} FAILED"))
    return 1 if _fail else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        import shutil
        shutil.rmtree(_TMP, ignore_errors=True)

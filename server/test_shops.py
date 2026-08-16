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
MAMMON = "1"


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

    # Diamonds are two balances: free is spent first and PAID covers the shortfall,
    # exactly as PlayerCurrency.Balance(Cash) prices it for the client.
    st = fresh()
    st["currency"]["1"] = 0
    st["currency"][str(ps.CURRENCY_CASH_PAID)] = 10 ** 7
    okp, _s, whyp, _n = ps.buy_shop_goods(st, 2101, 1)
    check("paid diamonds cover an empty free balance", okp, whyp)
    check("and the charge came off the paid balance",
          int(st["currency"][str(ps.CURRENCY_CASH_PAID)]) < 10 ** 7)

    st = fresh()
    st["currency"]["1"] = 0
    st["currency"][str(ps.CURRENCY_CASH_PAID)] = 0
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

    # ---- Mammon's Premium Shop (shop 1) -----------------------------------
    mam = sh.DEFAULT_SHOP_GOODS[MAMMON]
    check("mammon rows have 21 fields", all(len(r) == 21 for r in mam))
    mids = [r[0] for r in mam]
    check("mammon goods ids are unique", len(set(mids)) == len(mids))
    check("mammon has the three reset tabs",
          {r[9] for r in mam} == {sh.FILTER_DAILY, sh.FILTER_WEEKLY, sh.FILTER_MONTHLY},
          str(sorted({r[9] for r in mam})))
    # Every tab IS a reset tier, so every card must carry the matching cycle.
    tab_cycle = {sh.FILTER_DAILY: sh.RESET_DAILY,
                 sh.FILTER_WEEKLY: sh.RESET_WEEKLY,
                 sh.FILTER_MONTHLY: sh.RESET_MONTHLY}
    for r in mam:
        if r[3] != tab_cycle[r[9]]:
            check(f"mammon goods {r[0]} cycle matches its tab", False,
                  f"tab {r[9]} cycle {r[3]}")
    check("every mammon card resets with its tab", True)
    for r in mam:
        if not bt.dd.row("item", r[5]) or not bt.dd.row("item", r[7]):
            check(f"mammon goods {r[0]} ids exist", False, f"{r[5]} / {r[7]}")
    check("mammon item and cost ids exist", True)

    # Multi-item bundles must pay EVERY line on the card, and headline a real item.
    st = fresh()
    st["currency"]["32"] = 10 ** 6                 # paid diamonds
    gems0 = int(st["currency"]["1"])
    coins0 = int(st["currency"]["16"])
    ok, _s, why, _n = ps.buy_shop_goods(st, 1101, 1)   # Daily Free Bundle
    check("a free bundle can be bought", ok, why)
    check("free bundle paid diamonds AND coins",
          int(st["currency"]["1"]) == gems0 + 10
          and int(st["currency"]["16"]) == coins0 + 25000,
          f"{gems0}->{st['currency']['1']} {coins0}->{st['currency']['16']}")

    st = fresh()
    t2 = ps.item_count(st, 102)
    t5 = ps.item_count(st, 105)
    ok, _s, why, _n = ps.buy_shop_goods(st, 1102, 1)   # Power-Leveling Bundle
    check("trainer bundle pays every tier", ok and
          ps.item_count(st, 102) == t2 + 24 and ps.item_count(st, 105) == t5 + 5,
          f"t2 {t2}->{ps.item_count(st,102)} t5 {t5}->{ps.item_count(st,105)}")

    st = fresh()
    st["currency"]["32"] = 10 ** 6
    ok, _s, why, _n = ps.buy_shop_goods(st, 1303, 1)   # costs PAID diamonds
    check("a paid-diamond card spends currency 32", ok and
          int(st["currency"]["32"]) == 10 ** 6 - 790, why or str(st["currency"]["32"]))

    for r in mam:
        rid, rcnt = ps.goods_reward(fresh(), r[0], 1)
        row = bt.dd.row("item", rid) or {}
        act = row.get("_action")
        if act in MASK_DROPPED or (act == 0 and row.get("_class") == 2) or rcnt <= 0:
            check(f"mammon goods {r[0]} headlines a displayable item", False,
                  f"item {rid} action {act} count {rcnt}")
    check("every mammon card headlines a displayable reward", True)

    # ---- multi-item bundles report every line -----------------------------
    # Reply 513 can only name ONE item, so a bundle's confirmation goes out as a
    # drop-item popup (Backpack 24) listing everything. Single-item cards must NOT
    # produce one, or the player gets two popups.
    st = fresh()
    ps.buy_shop_goods(st, 1102, 1)                 # Power-Leveling: 5 lines
    lines = ps.goods_bundle_lines(st, 1102)
    check("a bundle reports every line", len(lines) == 5, str(lines))
    check("bundle lines carry real counts",
          dict(lines).get(102) == 24 and dict(lines).get(2) == 250000, str(lines))
    check("bundle lines are all displayable",
          all((bt.dd.row("item", i) or {}).get("_action") not in MASK_DROPPED
              for i, _c in lines), str(lines))

    st = fresh()
    ps.buy_shop_goods(st, 1103, 1)                 # Soul Essence bundle: 2 lines
    check("a two-line bundle reports both", len(ps.goods_bundle_lines(st, 1103)) == 2,
          str(ps.goods_bundle_lines(st, 1103)))

    st = fresh()
    ps.buy_shop_goods(st, 2409, 1)                 # plain Soul Essence card
    check("a plain card reports no bundle lines",
          len(ps.goods_bundle_lines(st, 2409)) <= 1,
          str(ps.goods_bundle_lines(st, 2409)))

    st = fresh()
    ps.buy_shop_goods(st, 2101, 1)                 # single-line bundle (coin box)
    check("a one-line bundle stays on the 513 popup",
          len(ps.goods_bundle_lines(st, 2101)) == 1,
          str(ps.goods_bundle_lines(st, 2101)))

    # ---- Drop Info (Backpack 129 -> 130) ----------------------------------
    # GetIconDataInBox reads each inner list positionally: [0] ItemID, [1] ItemCount,
    # optional [2] DropWeight. Contents come from the same tables a purchase pays from,
    # so the preview cannot drift from what buying actually gives.
    for iid, want in ((901, 5), (3650, 4), (736, 1)):
        c = ps.box_contents(iid)
        check(f"box {iid} lists its contents", len(c) == want, str(c))
        check(f"box {iid} rows are [item, count]",
              all(len(e) == 2 and e[1] > 0 for e in c), str(c))
        check(f"box {iid} names real items",
              all(bt.dd.row("item", e[0]) for e in c), str(c))

    # A random starshard box previews display-only "SET" icons -- the
    # `Random ★N Element` items -- not concrete shards. Which ones depends on what the
    # card randomises, and both shapes appear in the live popups.
    lb = ps.box_contents(1200021)          # ★4 (UR-LR) Random SLOT Endearment
    check("an element box lists its UR and LR set icons", len(lb) == 2, str(lb))
    names = {(bt.dd.row("item", e[0]) or {}).get("_itemName_en") for e in lb}
    check("both are the same element and star", names == {"Random \u26054 Endearment"},
          str(names))

    slotbag = ps.box_contents(1200020)     # ★3 (UR-LR) Random Starshard (Slot 6)
    check("a slot box lists one icon per element", len(slotbag) == 6, str(len(slotbag)))
    els = [(bt.dd.row("item", e[0]) or {}).get("_itemName_en") for e in slotbag]
    check("covering all six elements",
          els == [f"Random \u26053 {el}" for el in sh.STARSHARD_ELEMENTS],
          str(els))

    # **Set icons are display-only and cannot be used**, so they must never be what a
    # purchase actually hands over -- the grant rolls a REAL shard (_action 111..116).
    st = fresh()
    before = len(st["backpack"].get("2", {}))
    ps.buy_shop_goods(st, 2401, 1)
    check("buying a luckybag grants a real shard",
          len(st["backpack"].get("2", {})) == before + 1)
    granted = [e["iid"] for e in st["backpack"]["2"].values()]
    check("and never a set icon",
          all((bt.dd.row("item", i) or {}).get("_action") in range(111, 117)
              for i in granted), str(granted))
    check("no set icon reached the bag",
          not any(i in {e[0] for e in lb} for i in granted), str(granted))

    orb = ps.box_contents(212)
    check("the awaker orb lists ★5 casts", len(orb) > 20, str(len(orb)))
    check("and no Bunrei variants",
          not any("Bunrei" in ((bt.dd.row("item", e[0]) or {}).get("_itemName_en") or "")
                  for e in orb))
    check("every orb entry is a cast item",
          all((bt.dd.row("item", e[0]) or {}).get("_action") == 1 for e in orb))
    check("an unknown box answers empty, not garbage", ps.box_contents(999999) == [])

    # Whatever Drop Info promises must be what the card actually pays.
    st = fresh()
    lines = dict(ps.box_contents(901))
    ps.buy_shop_goods(st, 1102, 1)
    paid = dict(ps.goods_bundle_lines(st, 1102))
    check("preview matches the payout", lines == paid, f"{lines} vs {paid}")

    # ---- no shop may list a SELECTOR --------------------------------------
    # `_action 7` opens a choose-your-reward flow the client handles itself (it never
    # asks the server), and with nothing behind it the panel leaves a permanent modal
    # overlay. Seen in game: the store became unusable until the app was restarted.
    for shop_id, rows in sh.DEFAULT_SHOP_GOODS.items():
        for r in rows:
            if not sh.is_sellable(r[5]):
                check(f"shop {shop_id} goods {r[0]} is not a selector", False,
                      f"item {r[5]} action 7")
    check("no shop sells a selector item", True)

    # ---- Asmodeus's Soul Altar (shop 3, partial) --------------------------
    altar = sh.DEFAULT_SHOP_GOODS["3"]
    check("altar rows have 21 fields", all(len(r) == 21 for r in altar))
    check("altar has all four tabs",
          {r[9] for r in altar} == {sh.FILTER_HOLY_BLOOD, sh.FILTER_SKILL_UP,
                                    sh.FILTER_ORBS, sh.FILTER_STAR_SHARDS},
          str(sorted({r[9] for r in altar})))

    # Skill Up is the fragment exchange: every card is bought with its OWN fragment.
    # Grimoires are item N <- fragment N+10; Inherit Gems use the 3000xx fragments.
    frag_of = {531: 541, 532: 542, 533: 543, 536: 546,
               12: 300011, 21: 300012, 13: 300013, 14: 300014, 23: 300015}
    for r in altar:
        if r[9] == sh.FILTER_SKILL_UP and r[5] in frag_of and r[7] != frag_of[r[5]]:
            check(f"skill-up goods {r[0]} costs its own fragment", False,
                  f"item {r[5]} costs {r[7]}, expected {frag_of[r[5]]}")
    check("every skill-up card costs its own fragment", True)

    # Grimoires are `_action 1` CASTS -- skill-up fodder you feed to another cast --
    # so they must land in the roster, not the bag.
    st = fresh()
    ps.grant_item(st, 543, 5000)
    n = len(st["roster"])
    ok, _s, why, new = ps.buy_shop_goods(st, 3203, 1)      # Grimoire of Rider
    check("a grimoire is granted as a cast",
          ok and len(new) == 1 and len(st["roster"]) == n + 1, why or str(new))
    check("and not bagged",
          not any(e.get("iid") == 533 for e in st["backpack"].get("1", {}).values()))

    # Holy Blood cards spend the Soul Altar's own currency.
    st = fresh()
    ps.grant_item(st, sh.COST_HOLY_BLOOD, 100000)
    hb = ps.item_count(st, sh.COST_HOLY_BLOOD)
    ok, _s, why, _n = ps.buy_shop_goods(st, 3103, 1)       # 500 Sin fragments
    check("a holy-blood card spends Holy Blood",
          ok and ps.item_count(st, sh.COST_HOLY_BLOOD) == hb - 20000, why)
    check("and pays the stated quantity", ps.item_count(st, 541) == 500,
          str(ps.item_count(st, 541)))
    for r in altar:
        if not bt.dd.row("item", r[5]) or not bt.dd.row("item", r[7]):
            check(f"altar goods {r[0]} ids exist", False, f"{r[5]} / {r[7]}")
        if not sh.is_sellable(r[5]):
            check(f"altar goods {r[0]} is not a selector", False, str(r[5]))
    check("altar ids exist and none is a selector", True)

    # **Every card must actually hand something over.** An orb whose pool is empty
    # would take the currency and grant nothing -- which is how the ★3 Minion orb
    # behaved when it was pooled over the playable alignments instead of the mobs.
    for iid in (212, 211, 210):
        check(f"orb {iid} has a pool", len(ps.box_contents(iid)) > 0)
    # **Awakers are NOT Sins / Virtues / Riders.** Those three casts are their own
    # thing; an Awaker orb that includes them is a Lucifer machine, not what the card
    # sells.
    for iid in (211, 212):
        bad = [e[0] for e in ps.box_contents(iid)
               if (bt.dd.row("char", (bt.dd.row("item", e[0]) or {}).get("_param1"))
                   or {}).get("_alignment") in (100, 101, 102)]
        check(f"awaker orb {iid} excludes Sins/Virtues/Riders", not bad, str(bad[:4]))
    check("awaker orbs draw only alignments 103/104",
          all((bt.dd.row("char", (bt.dd.row("item", e[0]) or {}).get("_param1"))
               or {}).get("_alignment") in sh.AWAKER_ALIGNMENTS
              for e in ps.box_contents(212)))

    check("the minion orb draws MINIONS",
          all("Gremlin" in ((bt.dd.row("item", e[0]) or {}).get("_itemName_en") or "")
              or (bt.dd.row("char",
                            (bt.dd.row("item", e[0]) or {}).get("_param1")) or {}
                  ).get("_alignment") in sh.MINION_ALIGNMENTS
              for e in ps.box_contents(210)))

    st = fresh()
    ps.grant_item(st, 300002, 100)
    n = len(st["roster"])
    ok, _s, why, new = ps.buy_shop_goods(st, 3603, 1)      # ★4 Awaker orb
    check("an orb grants a cast", ok and len(new) == 1 and len(st["roster"]) == n + 1,
          why or str(new))
    check("granted at the orb's star", st["roster"][new[0]]["star"] == 4,
          str(st["roster"][new[0]]))

    st = fresh()
    ps.grant_item(st, 304, 5)
    shards = len(st["backpack"].get("2", {}))
    ok, _s, why, _n = ps.buy_shop_goods(st, 3401, 1)       # ★4 LR shard ticket
    check("a shard ticket buys a real shard",
          ok and len(st["backpack"].get("2", {})) == shards + 1, why)

    test_quest_goods_ids()
    test_buy_quest_credit()

    print("\n" + ("ALL PASSED" if not _fail else f"{_fail} FAILED"))
    return 1 if _fail else 0



def test_quest_goods_ids():
    """Goods ids a QUEST names are not ours to choose.

    Quests 31017/31034 ("Go to Shop-Soul Altar and exchange Grimoire of ★4 Awaker")
    carry `_case_v1 = 3305`, and 31041 carries 3304 for the ★5. The GO! button sends
    that id verbatim as SendGoodsIDToShopIDCmd (cmd 261) and the server answers with
    the shop and tab to jump to -- so the card MUST be numbered what the quest says.
    """
    import battle as bt
    from player_state.shop import DEFAULT_SHOP_GOODS, FILTER_SKILL_UP
    from player_state.core import _default, _seed_roster
    import player_state as ps

    quest_goods = {}
    for qid, r in (bt.dd.rows("quest") or {}).items():
        if r.get("_case_id") == 2003 and r.get("_case_v1"):
            quest_goods.setdefault(int(r["_case_v1"]), []).append(qid)

    check("the quest table still names goods 3305", 3305 in quest_goods)
    check("the quest table still names goods 3304", 3304 in quest_goods)

    st = _default(1000001)
    _seed_roster(st)
    # 3305 -> Grimoire of ★4 Awaker, 3304 -> ★5, both on the Skill Up tab.
    for gid, item_id, cost_id, price in ((3305, 535, 545, 625), (3304, 534, 544, 750)):
        shop_id, row = ps.find_shop_goods(st, gid)
        check(f"goods {gid} exists", row is not None)
        if not row:
            continue
        check(f"goods {gid} is in the Soul Altar", shop_id == 3, str(shop_id))
        check(f"goods {gid} sells item {item_id}", row[5] == item_id, str(row[5]))
        check(f"goods {gid} is on the Skill Up tab", row[9] == FILTER_SKILL_UP,
              str(row[9]))
        check(f"goods {gid} costs {price}x item {cost_id}",
              (row[7], row[8]) == (cost_id, price), str((row[7], row[8])))
        # This pair is what cmd 261 answers with; both are required or
        # EnterSpecificStore is never reached.
        check(f"goods {gid} yields a complete (shop, tab) answer",
              bool(shop_id) and bool(row[9]))

    # On the SKILL UP tab, Grimoire N is bought with fragment N+10, without exception.
    # (The Holy Blood tab also sells two Grimoires, but for Holy Blood of Saint -- a
    # different tab, a different currency, so it is excluded here on purpose.)
    altar = DEFAULT_SHOP_GOODS["3"]
    grimoires = [r for r in altar
                 if r[5] in range(531, 537) and r[9] == FILTER_SKILL_UP]
    check("all six Grimoires are on the Skill Up tab", len(grimoires) == 6,
          str(sorted(r[5] for r in grimoires)))
    check("every Skill Up Grimoire costs its own fragment",
          all(r[7] == r[5] + 10 for r in grimoires),
          str([(r[5], r[7]) for r in grimoires]))

    # Goods ids must stay globally unique -- find_shop_goods returns the first match.
    ids = [r[0] for rows in DEFAULT_SHOP_GOODS.values() for r in rows]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    check("no duplicate goods ids anywhere", not dupes, str(dupes))


def test_buy_quest_credit():
    """Buying goods must credit the quest watching THAT goods id -- and only it.

    Case 2003 is "go to <shop> and exchange <item>", and `_case_v1` is the GOODS id, a
    discriminator rather than a parameter. Bumping on `_case_id` alone would credit all
    fifteen rows -- the Monthly Pass, four Step Gift Boxes, Mammon's three free bundles
    -- off one Grimoire. That is the same shape as the earlier bug where one Power-up
    armed steps 6/10/16/24 at once, so it is pinned here rather than left to inspection.

    Two rows legitimately share v1 3305 (quests 31017 and 31034: the same objective in
    two different chains). They are kept apart by `_pre_quest`, which bump_quest_counter
    honours for `_case_type 2` rows.
    """
    import battle as bt
    import player_state as ps
    from player_state.core import _default, _seed_roster, SP_QUEST_COMPLETE

    def fresh(done=()):
        st = _default(1000001)
        _seed_roster(st)
        for q in done:
            st["sp_quests"][str(q)] = {"id": q, "a_time": 0, "cnt": 1,
                                       "status": SP_QUEST_COMPLETE}
        for frag in (544, 545, 120):
            ps.grant_reward(st, frag, 5000)
        return st

    def armed(st, ignore):
        return {int(k) for k, v in st["sp_quests"].items()
                if k not in {str(i) for i in ignore}
                and v.get("status") != SP_QUEST_COMPLETE and v.get("cnt", 0) > 0}

    # The step the player is actually on advances...
    st = fresh(done=[31016])
    ok, _s, why, _n = ps.buy_shop_goods(st, 3305, 1)
    check("the Grimoire purchase went through", ok, why)
    check("step 31017 is credited", armed(st, [31016]) == {31017},
          str(armed(st, [31016])))
    check("and its counter reaches the requirement",
          st["sp_quests"]["31017"]["cnt"]
          >= (bt.dd.row("quest", 31017) or {}).get("_case_cnt", 1))
    check("the shared quest_db counter is untouched for an SP quest",
          not st["quest_db"], str(st["quest_db"]))

    # ...a step whose PREDECESSOR is not done does not, even sharing the same goods id.
    st = fresh()
    ps.buy_shop_goods(st, 3305, 1)
    check("no step arms when no predecessor is done", armed(st, []) == set(),
          str(armed(st, [])))

    # ...and a sibling step watching a DIFFERENT goods id never moves.
    st = fresh(done=[31016, 31040])
    ps.buy_shop_goods(st, 3305, 1)
    check("31041 (goods 3304) is not credited by buying 3305",
          armed(st, [31016, 31040]) == {31017}, str(armed(st, [31016, 31040])))

    # Both chains open at once is legitimate: same objective, two chains.
    st = fresh(done=[31016, 32001])
    ps.buy_shop_goods(st, 3305, 1)
    check("both chains credit when both are live",
          armed(st, [31016, 32001]) == {31017, 31034},
          str(armed(st, [31016, 32001])))

    # Mammon's daily free is watched by three rows; two are `_type 7`, a system we do
    # not run, and must stay untouched.
    st = fresh()
    ps.buy_shop_goods(st, 1101, 1)
    check("the daily-free purchase credits only the supported quest",
          armed(st, []) == {10033}, str(armed(st, [])))

    # A card no quest watches must move nothing whatsoever.
    st = fresh()
    ps.buy_shop_goods(st, 3605, 1)
    check("an unwatched card credits no quest at all",
          armed(st, []) == set() and not st["quest_db"],
          f"{armed(st, [])} {st['quest_db']}")

    # Guard the id alignment itself: every goods id a case-2003 quest names and we sell
    # must be deliberate, because selling it silently completes that quest.
    watched = {int(r["_case_v1"]) for r in (bt.dd.rows("quest") or {}).values()
               if r.get("_case_id") == 2003 and r.get("_case_v1")}
    from player_state.shop import DEFAULT_SHOP_GOODS
    ours = {g[0] for gs in DEFAULT_SHOP_GOODS.values() for g in gs}
    check("only the intended goods ids are quest-watched",
          (watched & ours) == {1101, 1201, 1301, 3304, 3305},
          str(sorted(watched & ours)))

if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        import shutil
        shutil.rmtree(_TMP, ignore_errors=True)

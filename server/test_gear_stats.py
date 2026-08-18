#!/usr/bin/env python3
"""Starshard rolls and the stats they are supposed to grant.

Two defects found from a device screenshot on 2026-08-18, both regression-guarded here:

  1. **CRI+999.0% on a ★1 shard.** `_bonus_rows_by_attr()` pooled the WHOLE 2043-row
     equipment_bonus table and picked uniformly, so a roll could land on row 907 --
     `_AttrType 6` (CRI), `_AttrInitV 9990`, and percentages are stored x10. The real
     pool is the piece's own `equipment._bonusID` group, which is what Soulmirrors had
     always used. Group 2001 (★1 Chaos) is 11 attributes x 3 quality tiers and its
     largest value is a 92 HP roll.

  2. **None of it reached battle.** `battle.Unit` summed `_grow(...)` plus the Soul
     Link book bonus and nothing else. The lobby showed ▲ deltas only because the
     CLIENT computes them (`CharData.RefreshEquipTotalValues`), so a fully geared cast
     fought at base stats and nobody could see why.

The value formulas are transcribed from `PlayerBackpack.GetEquipGrowValue` and are
three DIFFERENT ones, which is the easiest thing here to get wrong:
    primary  pid_i -> _AttrInitV + _AttrUpV * lv
    ihid     ihid  -> _AttrInitV                      (no level scaling)
    sub      bid_j -> (be_j + 1) * _AttrInitV         (no level scaling)

    python3 test_gear_stats.py
"""
import os
import random
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

_TMP = tempfile.mkdtemp(prefix="sevensins-gear-test-")
os.environ["SEVENSINS_ACCOUNTS"] = _TMP

import battle as bt                                    # noqa: E402
import player_state as ps                              # noqa: E402
from player_state import gear as gd                     # noqa: E402
from player_state.core import (                         # noqa: E402
    ATTR_ATK, ATTR_CRI, ATTR_PATK, ATTR_SPD,
    BONUS_TYPE_ATTRIBUTE, _bonus_group_rows, _bonus_rows_by_attr,
)

_fail = 0

# ★1 Chaos I..VI, the exact family from the screenshot. Suit 2, bonus group 2001.
CHAOS_1 = [202101, 202201, 202301, 202401, 202501, 202601]
# CharAttribute percent attrs are stored x10.
PERCENT_ATTRS = {6, 8, 10, 11, 101, 102, 103}


def check(name, cond, detail=""):
    global _fail
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        _fail += 1


def fresh():
    st = ps.load(1000001)
    st["backpack"]["2"] = {}
    for e in st["roster"].values():
        e["equips_list"] = [""] * 18
    return st


def displayed(row_id):
    """What the client would print for a bonus row at enhance 0, level 0."""
    row = bt.dd.row("equipment_bonus", row_id) or {}
    v = row.get("_AttrInitV", 0)
    return v / 10 if row.get("_AttrType") in PERCENT_ATTRS else v


def check_the_999_row_exists():
    """Pin the actual culprit, so nobody 'cleans up' the scoping and reopens it."""
    row = bt.dd.row("equipment_bonus", 907) or {}
    check("row 907 is still the CRI 9990 row", row.get("_AttrType") == ATTR_CRI
          and row.get("_AttrInitV") == 9990, str(row))
    check("  ...which would display as 999.0%", displayed(907) == 999.0)
    # It is a real Type-1 attribute row, so nothing about the row itself excludes it;
    # only the group scoping does.
    check("  ...and is a plain attribute row, not filtered by _Type",
          row.get("_Type") == BONUS_TYPE_ATTRIBUTE)
    check("it is NOT in any starshard's bonus group",
          907 not in set(_bonus_rows_by_attr(202501).get(ATTR_CRI, [])))


def check_roll_pool_is_scoped():
    st = fresh()
    rng = random.Random(1)
    worst, groups = 0, set()
    for _ in range(2000):
        r = ps.make_rune(st, 202501, 5, rng=rng)
        for key, rid in r["attr"].items():
            if not key.startswith(("pid_", "bid_")):
                continue
            groups.add((bt.dd.row("equipment_bonus", rid) or {}).get("_group"))
            worst = max(worst, displayed(rid))
    check("2000 rolls all come from the piece's own bonus group",
          groups == {2001}, str(sorted(groups)))
    # The bug produced 999.0; anything in this group tops out at a 92 HP roll.
    check("  ...so no roll can display anything absurd", worst <= 100, str(worst))

    # The unscoped pool is what the bug used -- prove the scoping is what excludes it.
    check("the UNSCOPED pool would still offer row 907",
          907 in set(_bonus_rows_by_attr().get(ATTR_CRI, [])))

    # Every star and every suit must be scoped, not just the one from the screenshot.
    for iid in (202101, 202601, 205501, 210101):
        row = bt.dd.row("item", iid)
        if not row:
            continue
        rune = ps.make_rune(st, iid, 1, rng=rng)
        want = (bt.dd.row("equipment", row.get("_param1")) or {}).get("_bonusID")
        got = {(bt.dd.row("equipment_bonus", v) or {}).get("_group")
               for k, v in rune["attr"].items() if k.startswith(("pid_", "bid_"))}
        check(f"item {iid} rolls inside group {want}", got == {want}, str(got))


def check_value_formulas():
    """The three formulas differ; a copy-paste between them is the likely regression."""
    st = fresh()
    rows = _bonus_group_rows(202501)
    sub_row = next(r for r in rows
                   if (bt.dd.row("equipment_bonus", r) or {}).get("_AttrUpV") == 0)
    init = bt.dd.row("equipment_bonus", sub_row)["_AttrInitV"]
    attr_type = bt.dd.row("equipment_bonus", sub_row)["_AttrType"]

    for enh in (0, 1, 4):
        entry = {"iid": 202501, "attr": {"lv": 0, f"bid_1": sub_row, f"be_1": enh}}
        got = gd._piece_attrs(entry).get(attr_type)
        check(f"a sub-stat at enhance {enh} is (be+1)*initV = {(enh + 1) * init}",
              got == (enh + 1) * init, str(got))

    # **Every real starshard/soulmirror group has `_AttrUpV == 0`**, so the level term
    # is dead in the shipped data and a piece's LEVEL grants no stats at all -- only
    # `be_` (enhance) does. That is worth asserting rather than assuming: if a later
    # pack gives these rows an increment, the primary formula suddenly starts mattering
    # and this test should be the thing that notices.
    ups = {(bt.dd.row("equipment_bonus", r) or {}).get("_AttrUpV") for r in rows}
    check("no shipped starshard row has a per-level increment", ups == {0}, str(ups))

    # Guard the transcription anyway, against a synthetic row shaped like one that has
    # an increment. init + up*lv for the primary; ihid ignores lv entirely.
    fake = {"_Type": BONUS_TYPE_ATTRIBUTE, "_AttrType": ATTR_ATK,
            "_AttrInitV": 100, "_AttrUpV": 7, "_AttrJob": 0}
    real_row = bt.dd.row
    try:
        bt.dd.row = lambda form, rid: (fake if form == "equipment_bonus"
                                       else real_row(form, rid))
        for lv in (0, 5, 15):
            got = gd._piece_attrs({"iid": 202501, "attr": {"lv": lv, "pid_1": 1}})
            check(f"a primary at lv {lv} is init + up*lv = {100 + 7 * lv}",
                  got.get(ATTR_ATK) == 100 + 7 * lv, str(got))
        got = gd._piece_attrs({"iid": 202501, "attr": {"lv": 15, "ihid": 1}})
        check("ihid does NOT scale with level", got.get(ATTR_ATK) == 100, str(got))
        got = gd._piece_attrs({"iid": 202501, "attr": {"lv": 15, "bid_1": 1, "be_1": 2}})
        check("a sub-stat ignores level too, and multiplies by (be+1)",
              got.get(ATTR_ATK) == 300, str(got))
    finally:
        bt.dd.row = real_row


def check_set_bonus():
    """The Chaos Set(4) from the screenshot: ATK+34.0% -> equip_suit 2, value 340."""
    suit = bt.dd.row("equip_suit", 2) or {}
    check("suit 2 is Chaos", suit.get("_suitName_en") == "Chaos", str(suit.get("_suitName_en")))
    check("  ...Set(4) on PATK worth 34.0%",
          suit.get("_n_number1") == 4 and suit.get("_n_attribute1") == ATTR_PATK
          and suit.get("_n_value1") == 340, str(suit))

    st = fresh()
    entry = st["roster"][next(iter(st["roster"]))]
    rng = random.Random(11)
    uids = [gd.grant_rune(st, CHAOS_1[s - 1], s, rng=rng)["uid"] for s in range(1, 7)]

    entry["equips_list"] = uids[:3] + [""] * 15
    check("three Chaos pieces do NOT trigger a Set(4)",
          gd.equipped_attr_totals(st, entry).get(ATTR_PATK, 0) < 340,
          str(gd.equipped_attr_totals(st, entry).get(ATTR_PATK)))

    entry["equips_list"] = uids + [""] * 12
    tot = gd.equipped_attr_totals(st, entry)
    check("six Chaos pieces DO trigger it",
          tot.get(ATTR_PATK, 0) >= 340, str(tot.get(ATTR_PATK)))


def check_battle_applies_gear():
    st = fresh()
    uid0 = next(iter(st["roster"]))
    entry = st["roster"][uid0]
    row = bt.dd.row("char", entry["id"])
    base = bt._grow(row, entry.get("star") or bt._default_star(row),
                    entry.get("lv", 1), 0)

    bare = ps.battle_team(st, 0)
    b0 = bt.Battle(1101, bare, st.get("team_level", 1), st.get("team_star"), 0, 0)
    u0 = next(u for u in b0.units.values()
              if u.team == bt.TEAM_PLAYER and u.uid == uid0)
    check("an ungeared cast still fights at exactly its base stats",
          (u0.atk, u0.defense, u0.max_hp, u0.spd)
          == (base["atk"], base["def"], base["hp"], base["spd"]),
          f"{u0.atk}/{u0.defense}/{u0.max_hp}/{u0.spd} vs {base}")

    rng = random.Random(3)
    entry["equips_list"] = [
        gd.grant_rune(st, CHAOS_1[s - 1], s, level=15, enhance=5, rng=rng)["uid"]
        for s in range(1, 7)] + [""] * 12
    geared = ps.battle_team(st, 0)
    me = next(p for p in geared if p.get("uid") == uid0)
    bonus = me.get("gear_bonus") or {}
    check("battle_team annotates the party with a gear bonus", bool(bonus), str(me.keys()))
    check("  ...and it is non-trivial", bonus.get("atk", 0) > 0 and bonus.get("hp", 0) > 0,
          str(bonus))

    b1 = bt.Battle(1101, geared, st.get("team_level", 1), st.get("team_star"), 0, 0)
    u1 = next(u for u in b1.units.values()
              if u.team == bt.TEAM_PLAYER and u.uid == uid0)
    check("the fight uses the geared ATK", u1.atk == base["atk"] + bonus["atk"],
          f"{u1.atk} vs {base['atk']}+{bonus['atk']}")
    check("  ...DEF", u1.defense == base["def"] + bonus["def"])
    check("  ...HP", u1.max_hp == base["hp"] + bonus["hp"])
    # SPD is CharAttribute 5, not 4 -- an earlier draft used 4 and dropped every roll.
    check("  ...and SPD, which is attribute 5 not 4",
          u1.spd == base["spd"] + bonus["spd"] and ATTR_SPD == 5,
          f"{u1.spd} vs {base['spd']}+{bonus['spd']}")
    check("gear actually raises damage output", u1.atk > u0.atk, f"{u1.atk} vs {u0.atk}")

    # Mobs have no backpack entry and must be untouched.
    mobs = [u for u in b1.units.values() if u.team == bt.TEAM_ENEMY]
    check("enemies get no gear bonus", all(not u.gear_bonus for u in mobs))


def check_repair_of_existing_saves():
    """Fixing the roll does nothing for shards already on disk -- and the reporting
    player has one equipped. The login repair is what actually clears them."""
    st = fresh()
    e = gd.grant_rune(st, 202501, 5, rng=random.Random(1))
    e["attr"]["bid_2"] = 907        # CRI 9990  -> the 999.0% from the screenshot
    e["attr"]["bid_3"] = 911        # PATK 662  -> a 66.2% ATK roll
    before = dict(e["attr"])

    n = ps.repair_equipment_rolls(st, rng=random.Random(5))
    check("the repair reports the piece", n == 1, str(n))
    allowed = set(_bonus_group_rows(202501))
    bad = [v for k, v in e["attr"].items()
           if k.startswith(("pid_", "bid_")) and v not in allowed]
    check("  ...and no impossible row survives", not bad, str(bad))

    # It should keep the shard's identity: a crit roll stays a crit roll.
    def attr_of(rid):
        return (bt.dd.row("equipment_bonus", rid) or {}).get("_AttrType")
    check("a replaced row keeps its attribute", attr_of(e["attr"]["bid_2"]) == ATTR_CRI,
          str(attr_of(e["attr"]["bid_2"])))
    check("  ...at a sane value", displayed(e["attr"]["bid_2"]) < 100,
          str(displayed(e["attr"]["bid_2"])))
    check("rows that were already valid are left alone",
          e["attr"]["bid_1"] == before["bid_1"] and e["attr"]["pid_1"] == before["pid_1"])
    check("running it again is a no-op", ps.repair_equipment_rolls(st) == 0)


def main():
    for fn in (check_the_999_row_exists, check_roll_pool_is_scoped,
               check_value_formulas, check_set_bonus, check_battle_applies_gear,
               check_repair_of_existing_saves):
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

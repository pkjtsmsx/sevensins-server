#!/usr/bin/env python3
"""Consonance stat rewards, the caster's DEF as a damage basis, and secondary stats.

    python3 test_consonance_stats.py

Three defects found reviewing a contributed hotfix, all of the same shape: the pack
states a number, the server reads it nowhere, and the panel promises a reward that no
code pays.

  * `char._flvBonus` -- the Karma reward ladder -- had ZERO reads anywhere in the tree.
  * `formula._basis_value` read the caster's DEF raw while ATK went through
    `effective_atk`, so a DEF-scaling kit got nothing from its own DEF buffs.
  * `battle.Unit` never set `cri`/`cdi`/`prc`/`ehit`, which `engine.formula` has always
    read, so no crit-damage scaling reached a fight and the stat popup sent zeros.

Every check here is anchored to BEHAVIOUR or to the pack's own rows, never to a
constant -- see CLAUDE.md section 6. The ladder checks in particular re-derive the
numbers from `char_flv` prose rather than asserting a table we typed in, because the
whole bug was that nobody had read the rows.

Uses its own SEVENSINS_ACCOUNTS tempdir so it never touches real save data.
"""
import os
import random
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

_TMP = tempfile.mkdtemp(prefix="sevensins-consonance-test-")
os.environ["SEVENSINS_ACCOUNTS"] = _TMP        # before player_state imports

import battle as bt                                            # noqa: E402
import design_data as dd                                       # noqa: E402
from engine import formula                                     # noqa: E402
from engine import status as _status                           # noqa: E402
from player_state import roster                                # noqa: E402
from player_state.core import _default as _mk                  # noqa: E402
from player_state.core import _seed_roster as _seed            # noqa: E402
from player_state.core import karma_of                         # noqa: E402

_fail = 0


def check(name, cond, detail=""):
    global _fail
    if not cond:
        _fail += 1
    print(f"  {'ok  ' if cond else 'FAIL'}  {name}" + (f"   [{detail}]" if detail else ""))


def _unit(char_id=10001, gear=None, lv=100, star=5):
    return bt.Unit(order="p0", char_id=char_id, team=0, index=0,
                   lv=lv, star=star, gear_bonus=gear)


# ---- the caster's DEF is a status-modified stat ------------------------------

def check_def_basis_is_status_aware():
    """A DEF buff on the ATTACKER must move a DEF-scaling cast's damage.

    Pre-fix this test fails on the second assertion: `_basis_value` returned
    `float(caster.defence)`, so Harden/Tough/Defense Tips and every DEF set bonus were
    cosmetic on exactly the casts built to stack DEF. The TARGET's DEF was already
    status-aware in `strike()`, which is why a DEF Break on a victim always worked and
    made the asymmetry hard to see from the outside.
    """
    caster, target = _unit(), _unit()
    flat = formula._basis_value("DEF", caster, target)
    check("an unbuffed DEF basis is the raw stat", flat == float(caster.defence),
          f"{flat} vs {caster.defence}")

    caster.statuses = [_status.Active(status_id=1, name="Harden", kind="stat_mod",
                                      category="buff", stat="DEF", remaining=2,
                                      magnitude=50.0)]
    buffed = formula._basis_value("DEF", caster, target)
    check("a +50% DEF buff raises a DEF-basis cast's damage stat by half",
          abs(buffed - flat * 1.5) < 1e-6, f"{flat} -> {buffed}")

    # ...and it must not have leaked into the other two bases.
    check("an ATK basis is untouched by a DEF buff",
          formula._basis_value("ATK", caster, target) == float(caster.atk))
    check("a MAX_HP basis still reads the TARGET's pool",
          formula._basis_value("MAX_HP", caster, target) == float(target.max_hp))

    # The mirror: a DEF DEBUFF on the caster must cut it, not raise it.
    caster.statuses = [_status.Active(status_id=2, name="DEF Break", kind="stat_mod",
                                      category="debuff", stat="DEF", remaining=2,
                                      magnitude=50.0)]
    broken = formula._basis_value("DEF", caster, target)
    check("a DEF break on the CASTER cuts its DEF-basis damage",
          abs(broken - flat * 0.5) < 1e-6, f"{flat} -> {broken}")


def check_def_basis_blast_radius():
    """This is not one character's fix -- say the real number out loud (section 7)."""
    import glob
    import json
    specs = {}
    for path in glob.glob(os.path.join(HERE, "battle_data/skills/cast/*.json")):
        specs.update(json.load(open(path)))
    for name in ("_other.json", "_sub_skill.json"):
        path = os.path.join(HERE, "battle_data/skills", name)
        if os.path.exists(path):
            specs.update(json.load(open(path)))
    groups, effects = set(), 0
    for spec in specs.values():
        for eff in spec.get("effects") or []:
            if eff.get("op") == "damage" and eff.get("basis") == "DEF":
                effects += 1
                groups.add(spec.get("group"))
    check("DEF-basis damage is a whole archetype, not an edge case",
          effects > 100 and len(groups) > 20,
          f"{effects} effects across {len(groups)} skill groups")


# ---- the Consonance ladder ---------------------------------------------------

def check_ladder_matches_the_pack():
    """Every rung of every ladder must agree with its own `char_flv` row's prose.

    The ladder is a packed string and the row is human-readable text; they are two
    independent statements of the same number, so agreeing on all 122 casts is real
    evidence that the format was read right. A mismatch means the parse is wrong.
    """
    flv_rows = {}
    for row in (dd.rows("char_flv") or {}).values():
        flv_rows.setdefault(int(row.get("_char_id") or 0), {})[
            int(row.get("_flv") or 0)] = row

    ladders = {int(c["_id"]): c["_flvBonus"] for c in (dd.rows("char") or {}).values()
               if c.get("_flvBonus")}
    check("every playable cast carries a ladder", len(ladders) > 100, str(len(ladders)))

    import re
    checked = missing = mismatched = 0
    for char_id, raw in ladders.items():
        for level, _attr, value in roster.parse_flv_bonus(raw):
            row = (flv_rows.get(char_id) or {}).get(level)
            if not row:
                missing += 1
                continue
            # "Lucifer's HP increases 3060" / "...CRT increases 15%". A percent-style
            # attribute prints the value divided by ten, which is the whole x10 story.
            text = row.get("_text_en") or ""
            nums = [int(n) for n in re.findall(r"(\d+)", text.replace("00FFFF", ""))]
            checked += 1
            if value not in nums and value // 10 not in nums:
                mismatched += 1
    check("every ladder rung has a matching char_flv row", missing == 0, str(missing))
    check("every rung's value appears in that row's own prose",
          mismatched == 0 and checked > 700, f"{checked} rungs, {mismatched} mismatched")


def check_ladder_attr_types_are_all_mapped():
    """An unmapped attr type would be silently dropped, so prove there are none."""
    seen = set()
    for char in (dd.rows("char") or {}).values():
        for _lv, attr, _v in roster.parse_flv_bonus(char.get("_flvBonus")):
            seen.add(attr)
    check("no ladder uses an attribute the map does not cover",
          seen and seen <= set(roster.FLV_ATTR_KEYS), str(sorted(seen)))


def check_ladder_is_cumulative_and_gated():
    """Rungs accumulate as Karma climbs, and none pay before their level."""
    ladder = roster.parse_flv_bonus(dd.row("char", 10001).get("_flvBonus"))
    first_level = min(lv for lv, _a, _v in ladder)
    check("nothing is paid below the first rung",
          roster.consonance_bonus(10001, first_level - 1) == {})

    # Monotonic: every stat is non-decreasing as Karma rises, and the top equals the
    # sum of the whole ladder rather than only its last rung.
    prev, monotonic = {}, True
    for level in range(0, 31):
        got = roster.consonance_bonus(10001, level)
        if any(got.get(k, 0) < v for k, v in prev.items()):
            monotonic = False
        prev = got
    check("a rising Karma rank never lowers a stat", monotonic)

    expected = {}
    for _lv, attr, value in ladder:
        key = roster.FLV_ATTR_KEYS[attr]
        expected[key] = expected.get(key, 0) + value
    check("a maxed ladder sums every rung", roster.consonance_bonus(10001, 30) == expected,
          str(roster.consonance_bonus(10001, 30)))

    # 106 of the 122 ladders repeat an attribute, so summing vs replacing is a real
    # question and not a hypothetical. Panagia pays HP at rung 3 AND rung 14.
    panagia = roster.parse_flv_bonus(dd.row("char", 10091).get("_flvBonus"))
    hp_rungs = [(lv, v) for lv, a, v in panagia if a == roster.FLV_ATTR_HP]
    check("Panagia really does have two separate HP rungs", len(hp_rungs) == 2,
          str(hp_rungs))
    check("both of Panagia's HP rungs are paid, not just the later one",
          roster.consonance_bonus(10091, 30)["hp"] == sum(v for _lv, v in hp_rungs),
          str(roster.consonance_bonus(10091, 30)["hp"]))


def check_ladder_reaches_a_fight():
    """End to end: raising Karma must move the stats a Unit actually fights with."""
    state = _mk("consonance-tester")
    _seed(state)
    entry = roster.battle_team(state, 0)[0]
    char_id = entry["id"]
    before = _unit(char_id, entry.get("gear_bonus"), entry.get("lv", 1), entry.get("star"))

    karma_of(state, char_id)["flv"] = 30
    entry = roster.battle_team(state, 0)[0]
    after = _unit(char_id, entry.get("gear_bonus"), entry.get("lv", 1), entry.get("star"))

    ladder = roster.consonance_bonus(char_id, 30)
    check("a maxed cast reports its Consonance separately from its gear",
          entry.get("consonance_bonus") == ladder, str(entry.get("consonance_bonus")))
    check("Consonance HP reaches the unit's max_hp",
          after.max_hp - before.max_hp == ladder.get("hp", 0),
          f"{before.max_hp} -> {after.max_hp}")
    check("Consonance ATK reaches the unit's atk",
          after.atk - before.atk == ladder.get("atk", 0), f"{before.atk} -> {after.atk}")
    check("Consonance DEF reaches the unit's defence",
          after.defence - before.defence == ladder.get("def", 0))
    check("Consonance SPD reaches the unit's spd",
          after.spd - before.spd == ladder.get("spd", 0))


def check_karma_level_is_read_from_the_right_field():
    """`karma_of` returns {"flv", "fxp"}, NOT an int.

    Guarding this because the contributed version passed the whole dict into
    `consonance_bonus`, where `int(dict)` raises TypeError -- outside the try, so it
    would have taken down every party build in the game the first time a cast had a
    ladder.
    """
    state = _mk("karma-shape-tester")
    _seed(state)
    got = karma_of(state, 10001)
    check("karma_of returns a mapping with an flv rank", isinstance(got, dict)
          and "flv" in got, str(got))
    entry = {"id": 10001, "gear_bonus": {}}
    roster._annotate_consonance(state, entry)     # must not raise
    check("annotating a party entry survives the real karma shape", True)


# ---- secondary stats ---------------------------------------------------------

def check_base_crit_survives_an_unpaid_cri():
    """The regression guard. `cri` unset means "use BASE_CRIT_RATE", not "zero".

    `formula.strike` reads `BASE_CRIT_RATE if crit_rate is None else float(crit_rate)`,
    so a Unit that assigns `self.cri = 0.0` for a cast nothing paid does not preserve
    behaviour -- it drops every unit in the game, mobs included, to a 0% crit rate.
    Measured rather than asserted on the attribute, because the attribute is not the
    thing that matters.
    """
    bare = _unit(gear=None)
    check("an ungeared unit leaves cri unset", bare.cri is None, repr(bare.cri))

    def crit_rate(unit, rolls=40000):
        rng, target, crits = random.Random(11), _unit(), 0
        for _ in range(rolls):
            _amount, detail = formula.strike(unit, target, 1.0, "ATK", rng)
            crits += bool(detail.get("crit"))
        return crits / rolls

    measured = crit_rate(bare)
    check("an ungeared unit still crits at the base rate",
          abs(measured - formula.BASE_CRIT_RATE) < 0.01,
          f"{measured:.4f} vs {formula.BASE_CRIT_RATE}")

    paid = crit_rate(_unit(gear={"cri": 150}))
    check("a cast paid CRI 150 crits at 15%", abs(paid - 0.15) < 0.02, f"{paid:.4f}")


def check_secondary_stats_are_arithmetic_safe():
    """The five that are added/subtracted must never be None -- that is a TypeError."""
    bare = _unit(gear=None)
    for attr in ("cdi", "cdr", "prc", "ehit", "eanti"):
        check(f"{attr} defaults to a number, not None",
              isinstance(getattr(bare, attr), float), repr(getattr(bare, attr)))
    # Exercise the paths that consume them rather than trusting the types.
    rng = random.Random(3)
    amount, _detail = formula.strike(bare, _unit(), 1.0, "ATK", rng)
    check("a bare unit can land a blow", isinstance(amount, int), repr(amount))
    check("a bare unit can land an effect",
          isinstance(formula.effect_lands(bare, _unit(), 0.5, rng), bool))


def check_crit_damage_scales():
    """CDI has to reach the multiplier, not just the wire."""
    def crit_mult(unit):
        rng = random.Random(5)
        for _ in range(4000):
            _amount, detail = formula.strike(unit, _unit(), 1.0, "ATK", rng)
            if detail.get("crit"):
                return detail["multiplier"]
        return None

    plain = crit_mult(_unit(gear={"cri": 1000}))               # always crits
    boosted = crit_mult(_unit(gear={"cri": 1000, "cdi": 500}))  # ...and +50% crit dmg
    check("crit damage responds to CDI", plain and boosted and boosted > plain * 1.4,
          f"{plain} -> {boosted}")


def check_wire_reports_what_the_engine_fought_with():
    """The stat popup used to send hardcoded zeros while the engine used real values."""
    paid = _unit(gear={"cri": 150, "cdi": 150})
    attrs = paid._attributes(current=False)
    check("cri round-trips back to the design scale", attrs["cri"] == 150, str(attrs["cri"]))
    check("cdi round-trips back to the design scale", attrs["cdi"] == 150, str(attrs["cdi"]))
    check("every BattleAttributeData field is an int",
          all(isinstance(v, int) for v in attrs.values()),
          str({k: type(v).__name__ for k, v in attrs.items()
               if not isinstance(v, int)}))
    bare = _unit(gear=None)._attributes(current=False)
    check("an unpaid cri goes out as 0 rather than None", bare["cri"] == 0)
    check("fields nothing pays yet stay 0",
          bare["tgn"] == 0 and bare["ddi"] == 0 and bare["ddr"] == 0)


def main():
    for fn in (check_def_basis_is_status_aware,
               check_def_basis_blast_radius,
               check_ladder_matches_the_pack,
               check_ladder_attr_types_are_all_mapped,
               check_ladder_is_cumulative_and_gated,
               check_ladder_reaches_a_fight,
               check_karma_level_is_read_from_the_right_field,
               check_base_crit_survives_an_unpaid_cri,
               check_secondary_stats_are_arithmetic_safe,
               check_crit_damage_scales,
               check_wire_reports_what_the_engine_fought_with):
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

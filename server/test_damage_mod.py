#!/usr/bin/env python3
"""Damage-taken / damage-dealt statuses: which side they land on, and which way.

`_damage_mult` was wrong on both axes at once, and the two errors partly hid each other.

  1. **SIDE.** The kind carries "damage taken" and "damage dealt" together and only the
     status distinguishes them, so the reader guessed from the English NAME --
     `"taken" in name or "reduction" in name or "受" in name`. That is wrong for **84 of
     the 148** damage_mod statuses, because the names do not say: `Fortitude`,
     `Legion Aegis`, `My Guardian` and `Wide Defense` are all damage-TAKEN modifiers and
     every one was being applied to the holder's damage DEALT.

  2. **DIRECTION.** `Active.sign` was never populated, so `signed_magnitude` fell back to
     the status's CATEGORY -- which answers "is this good for the holder", the opposite
     question. For the whole damage-taken family the two invert:

         Fortitude     category buff    受到的傷害-15%      the number goes DOWN
         Fragile       category debuff  受到傷害提升40%     the number goes UP

Together they turned `Fortitude` -- take 15% less damage -- into "deal 15% MORE damage".

The registry now carries `subject`, voted from the Chinese glossary where it speaks
(compile_statuses.py), and the sign comes from the prose. A status whose subject is
unresolved contributes NOTHING rather than falling back to the name heuristic: that
heuristic is the thing being replaced and it is wrong more often than right here.

    python3 test_damage_mod.py
"""
import json
import os
import sys

os.environ.setdefault("SEVENSINS_ACCOUNTS", "/tmp/sevensins-dmgmod-test")

from engine import status as est          # noqa: E402

_fail = 0


def check(label, ok, detail=""):
    global _fail
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f" -- {detail}"))
    if not ok:
        _fail += 1


def _holder(*statuses):
    u = type("U", (), {})()
    u.statuses = list(statuses)
    return u


def _st(name, subject, magnitude, sign, category):
    return est.Active(status_id=1, name=name, kind="damage_mod", category=category,
                      subject=subject, magnitude=magnitude, sign=sign)


def check_fortitude_is_a_defensive_buff():
    print("\ncheck_fortitude_is_a_defensive_buff:")
    # 受到的傷害-15%. The bug in one assertion: this used to read as +15% damage DEALT.
    u = _holder(_st("Fortitude", "taken", 15.0, -1, "buff"))
    check("takes 15% less damage",
          abs(est.damage_taken_multiplier(u) - 0.85) < 1e-9,
          str(est.damage_taken_multiplier(u)))
    check("  ...and its damage DEALT is untouched",
          est.damage_dealt_multiplier(u) == 1.0,
          str(est.damage_dealt_multiplier(u)))


def check_fragile_is_a_debuff_whose_number_goes_up():
    print("\ncheck_fragile_is_a_debuff_whose_number_goes_up:")
    # 受到傷害提升40% -- a debuff, and the magnitude RISES. The category says "debuff"
    # and would have subtracted, turning a vulnerability into a 40% damage reduction.
    u = _holder(_st("Fragile", "taken", 40.0, 1, "debuff"))
    check("takes 40% more damage",
          abs(est.damage_taken_multiplier(u) - 1.4) < 1e-9,
          str(est.damage_taken_multiplier(u)))


def check_sides_do_not_leak():
    print("\ncheck_sides_do_not_leak:")
    u = _holder(_st("Fortitude", "taken", 15.0, -1, "buff"),
                _st("Special Training (ATK)", "dealt", 20.0, 1, "buff"))
    check("the taken side sees only the taken status",
          abs(est.damage_taken_multiplier(u) - 0.85) < 1e-9,
          str(est.damage_taken_multiplier(u)))
    check("the dealt side sees only the dealt status",
          abs(est.damage_dealt_multiplier(u) - 1.2) < 1e-9,
          str(est.damage_dealt_multiplier(u)))


def check_unresolved_contributes_nothing():
    print("\ncheck_unresolved_contributes_nothing:")
    # 22 damage_mod statuses have no subject anywhere in the prose. Refusing to guess is
    # the same stance shield_size takes for a shield whose line states no size.
    u = _holder(_st("Something", None, 30.0, -1, "buff"))
    check("neither side picks it up",
          est.damage_taken_multiplier(u) == 1.0
          and est.damage_dealt_multiplier(u) == 1.0,
          f"{est.damage_taken_multiplier(u)} / {est.damage_dealt_multiplier(u)}")
    # ...and so does a resolved subject with no direction.
    u = _holder(est.Active(status_id=1, name="Something", kind="damage_mod",
                           category="misc", subject="taken", magnitude=30.0))
    check("  ...nor does a magnitude with no direction",
          est.damage_taken_multiplier(u) == 1.0,
          str(est.damage_taken_multiplier(u)))


def check_stacking_is_additive_and_floored():
    print("\ncheck_stacking_is_additive_and_floored:")
    a = _st("Kitty Bell", "taken", 3.0, -1, "buff")
    a.stacks = 20                       # 受到的傷害-3%，最多可堆疊20層
    check("20 stacks of -3% is -60%, not a product",
          abs(est.damage_taken_multiplier(_holder(a)) - 0.4) < 1e-9,
          str(est.damage_taken_multiplier(_holder(a))))
    b = _st("Huge", "taken", 400.0, -1, "buff")
    check("  ...and a reduction past 100% floors at 0 rather than inverting",
          est.damage_taken_multiplier(_holder(b)) == 0.0,
          str(est.damage_taken_multiplier(_holder(b))))


def check_the_registry_actually_carries_it():
    print("\ncheck_the_registry_actually_carries_it:")
    reg = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                      "battle_data", "statuses.json")))
    dm = [v for v in reg.values() if v.get("kind") == "damage_mod"]
    check("every damage_mod row has the field", all("subject" in v for v in dm),
          str(len(dm)))
    named = {v["name"]: v for v in dm if v.get("name")}
    # The four that name nothing useful and were the whole problem.
    for nm in ("Fortitude", "Legion Aegis", "My Guardian", "Wide Defense"):
        row = named.get(nm)
        check(f"  {nm} is a damage-TAKEN status",
              row is not None and row.get("subject") == "taken",
              str(row and row.get("subject")))
    # And the field is scoped: emitting it on kinds nothing reads would be noise.
    check("no other kind carries a subject",
          not any(v.get("subject") for v in reg.values()
                  if v.get("kind") != "damage_mod"))
    # Where the Chinese glossary speaks it is the source; `row_note` is the weak one.
    srcs = {v.get("subject_source") for v in dm}
    check("  ...and each row records where its subject came from",
          srcs <= {"zh_glossary", "en_glossary", "row_note", None}, str(srcs))


if __name__ == "__main__":
    check_fortitude_is_a_defensive_buff()
    check_fragile_is_a_debuff_whose_number_goes_up()
    check_sides_do_not_leak()
    check_unresolved_contributes_nothing()
    check_stacking_is_additive_and_floored()
    check_the_registry_actually_carries_it()
    print(f"\n{_fail} failure(s)")
    sys.exit(1 if _fail else 0)

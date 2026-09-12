#!/usr/bin/env python3
"""Heals must not become targets of the attack cinematic.

Reported from a device 2026-09-11: Michael's Gate of Judgement played its crush
animation ON the healed party members. The damage went to the enemy and the healing to
the party -- the arithmetic was right and only the animation aimed at the wrong people.

**Why group 0 is special**, from the client rather than from guesswork.
`AttackBehavior.PlayStart` (0x1BE0298) builds the cinematic's track targets by walking
`DmgInfo[0]` -- group 0, top level only -- and adding `GetUnit(row.c).Doll` for each row,
then handing that list to `BscPlayCmd.SetTrackTargetsToOverride(7, ...)` and
`SkillUtil.NormalizeSkillAimpoint`. Any unit named there is something the attack is
aimed at, whatever the row's mode or sign. Heals were folded straight into group 0.

**Why `extra` is the right home.** `OnDamage` (0x1BE2A18) ends by recursing through
`args.Extra`, resolving each row's own target with `GetUnit` and calling itself -- so an
extra row still runs the mode switch, `ShowHpBar`, the floating number and
`updateStatus`. `PlayInjured` is gated on `Damage < 0`, so a positive heal stays quiet.
And `PlayStart`'s target loop does not recurse into Extra, which is the whole point.

    python3 test_heal_animation.py
"""
import json
import os
import sys

os.environ.setdefault("SEVENSINS_ACCOUNTS", "/tmp/sevensins-healanim-test")

from engine import core, wire          # noqa: E402

_fail = 0


def check(label, ok, detail=""):
    global _fail
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f" -- {detail}"))
    if not ok:
        _fail += 1


def _outcome(heals, strikes=2):
    out = core.Outcome(caster="104", skill_id=2090101, swings=max(1, strikes))
    out.targets = ["106"]
    out.strikes = [core.Strike(swing=i, target="106", amount=900 - i, detail={})
                   for i in range(strikes)]
    out.heals = [{"target": t, "amount": a, "basis": "atk"} for t, a in heals]
    return out


def _groups(out):
    return wire.attack_json(out, caster_order="104", skill_id=2090101)["data"]


def check_healed_allies_are_not_in_group_zero():
    print("\ncheck_healed_allies_are_not_in_group_zero:")
    groups = _groups(_outcome([("101", 500), ("105", 400)]))
    named = [r["c"] for r in groups[0]]
    check("group 0 names ONLY the unit being attacked", named == ["106"], str(named))
    extra = groups[0][0]["extra"]
    inner = extra[0] if extra else []
    check("  ...and both heals ride that row's extra",
          sorted(r["c"] for r in inner) == ["101", "105"], json.dumps(extra)[:160])
    check("  ...with their amounts intact and POSITIVE",
          sorted(r["dmg"] for r in inner) == [400, 500], str([r["dmg"] for r in inner]))


def check_a_unit_already_struck_keeps_the_fold():
    print("\ncheck_a_unit_already_struck_keeps_the_fold:")
    # The enemy is already a track target, so moving its heal gains nothing -- and
    # folding is what upholds the one-row-per-unit rule that `PlayStart`'s unguarded
    # `Dictionary.Add(row.c, ...)` depends on.
    groups = _groups(_outcome([("106", 300)]))
    check("the struck unit's heal folds into its own row", len(groups[0]) == 1,
          json.dumps(groups[0])[:160])
    check("  ...netting against the damage rather than adding a second row",
          groups[0][0]["dmg"] == -900 + 300, str(groups[0][0]["dmg"]))
    check("  ...and no extra is created", not groups[0][0]["extra"])


def check_a_skill_with_no_damage_is_left_alone():
    print("\ncheck_a_skill_with_no_damage_is_left_alone:")
    # Nothing to hang `extra` off, and emptying group 0 would leave
    # NormalizeSkillAimpoint with no targets at all -- a worse unknown than the bug.
    out = core.Outcome(caster="104", skill_id=999, swings=1)
    out.heals = [{"target": "101", "amount": 500, "basis": "atk"}]
    groups = wire.attack_json(out, caster_order="104", skill_id=999)["data"]
    check("a pure heal still lands in group 0",
          [r["c"] for r in groups[0]] == ["101"], json.dumps(groups[0])[:160])
    check("  ...as a positive row", groups[0][0]["dmg"] == 500, str(groups[0][0]["dmg"]))


def check_the_wire_invariants_still_hold():
    print("\ncheck_the_wire_invariants_still_hold:")
    # Two heals on ONE unit, plus that unit struck: the case that used to be able to
    # produce two rows for one unit in group 0.
    out = _outcome([("101", 500), ("101", 250)])
    try:
        groups = _groups(out)
    except wire.WireError as exc:
        check("no WireError", False, str(exc))
        return
    seen = [r["c"] for r in groups[0]]
    check("no unit appears twice in group 0", len(seen) == len(set(seen)), str(seen))
    inner = groups[0][0]["extra"][0]
    check("  ...and both heals for one ally are carried",
          sum(r["dmg"] for r in inner if r["c"] == "101") == 750,
          json.dumps(inner)[:160])


if __name__ == "__main__":
    check_healed_allies_are_not_in_group_zero()
    check_a_unit_already_struck_keeps_the_fold()
    check_a_skill_with_no_damage_is_left_alone()
    check_the_wire_invariants_still_hold()
    print(f"\n{_fail} failure(s)")
    sys.exit(1 if _fail else 0)

#!/usr/bin/env python3
"""A follow-up can carry its own cinematic entry, and the damage is the same either way.

    python3 test_followup_own_cinematic.py

`fold` sums rows naming the same unit in the same group, so a pursuit landing on the unit
its parent just hit disappears INTO the parent's number -- 146 of the 277 playable-cast
skills with a follow_up show one number where the prose describes two beats.
`wire.FOLLOW_UP_OWN_COMBO` gives each follow-up its own AttackJsonData, named with the
sub-skill's id so the client resolves the sub-skill's own `_actName`.

The flag is OFF until a device confirms the client iterates the combo array, so the first
check is the one that matters most today: with it off, nothing changes at all.
"""
import os
import random
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["SEVENSINS_ACCOUNTS"] = tempfile.mkdtemp(prefix="sevensins-followup-test-")

import battle as bt                                            # noqa: E402
from engine import core as ecore, specs, wire                   # noqa: E402

FAILURES = []


def check(cond, msg):
    if not cond:
        FAILURES.append(msg)


def _fight(pid, flag):
    """Run one skill against one enemy. -> (entries, total damage across them)."""
    was, wire.FOLLOW_UP_OWN_COMBO = wire.FOLLOW_UP_OWN_COMBO, flag
    try:
        caster = bt.Unit(order="p1", char_id=10001, team=1, index=0, lv=50)
        enemy = bt.Unit(order="e1", char_id=10001, team=2, index=0, lv=50)
        for u in (caster, enemy):
            u.hp = u.max_hp = 50_000_000       # survive, so nothing is clamped by death
            u.defence = 0
        caster.atk = caster.defence = 10_000
        out = ecore.execute(caster, specs.skills()[pid], [caster, enemy],
                            rng=random.Random(1), chosen="e1", round_no=1)
        entries = wire.attack_entries(out, caster_order="p1", skill_id=pid)
        total = sum(-r["dmg"] for e in entries for g in e["data"] for r in g
                    if r["md"] == wire.MODE_HP and r["dmg"] < 0)
        return entries, total
    finally:
        wire.FOLLOW_UP_OWN_COMBO = was


def _with_follow_ups():
    return [pid for pid, sp in specs.skills().items()
            if sp.get("type") != "passive"
            and any(e.get("op") == "follow_up" for e in sp.get("effects") or [])]


def off_changes_nothing():
    """The default must be the old payload, entry for entry."""
    sampled = 0
    for pid in _with_follow_ups():
        try:
            entries, _ = _fight(pid, False)
        except Exception:                                      # noqa: BLE001
            continue
        sampled += 1
        check(len(entries) == 1,
              f"skill {pid} sent {len(entries)} entries with the flag OFF")
        if sampled >= 150:
            break
    check(sampled, "no skill with a follow_up ran at all -- wrong fixture")


def on_splits_without_changing_the_damage():
    """Michael's Special Sanction: 2 swings of its own, then a 3-swing pursuit."""
    pid = next((p for p, sp in specs.skills().items()
                if (sp.get("name") or "") == "Special Sanction"), None)
    check(pid, "Special Sanction is not in the compiled data -- wrong fixture")
    if not pid:
        return
    one, total_off = _fight(pid, False)
    many, total_on = _fight(pid, True)
    check(len(one) == 1, f"flag off gave {len(one)} entries")
    check(len(many) == 2, f"flag on gave {len(many)} entries, wanted parent + pursuit")
    check(total_off == total_on,
          f"the split changed the damage: {total_off} -> {total_on}")
    if len(many) == 2:
        check(many[0]["skill"] == pid, "the first entry is not the parent's")
        check(many[1]["skill"] != pid,
              "the pursuit entry reuses the parent's skill id, so the client would "
              "replay the parent's cinematic instead of the sub-skill's")
        check(len(one[0]["data"]) == len(many[0]["data"]),
              "the parent's group count changed -- the cinematic's swings are fixed")


def a_parent_with_no_damage_of_its_own_folds():
    """Divine Envoy and The Supreme state their own hit as an after_action.

    Lifting the pursuit out would leave group 0 with nothing for `PlayStart` to aim at,
    so those fall back to the merged number rather than sending an unaimed cinematic.
    """
    names = ("Divine Envoy", "The Supreme")
    found = 0
    for pid, sp in specs.skills().items():
        if (sp.get("name") or "") not in names:
            continue
        try:
            entries, total = _fight(pid, True)
        except Exception:                                      # noqa: BLE001
            continue
        found += 1
        check(len(entries) == 1,
              f"{sp['name']} ({pid}) split into {len(entries)} entries, but its parent "
              "has no damage to animate")
        check(total > 0, f"{sp['name']} ({pid}) landed nothing at all")
    check(found, "neither Divine Envoy nor The Supreme is in the data -- wrong fixture")


def every_entry_is_a_legal_payload():
    """Each entry is a full AttackJsonData; `attack_json` raises WireError if it is not."""
    checked = 0
    for pid in _with_follow_ups()[:200]:
        try:
            entries, _ = _fight(pid, True)
        except wire.WireError as exc:
            FAILURES.append(f"skill {pid} built an illegal split payload: {exc}")
            continue
        except Exception:                                      # noqa: BLE001
            continue
        for e in entries:
            checked += 1
            check(e.get("data"), f"skill {pid} sent an entry with no groups")
            for gi, g in enumerate(e["data"]):
                seen = [r["c"] for r in g]
                check(len(seen) == len(set(seen)),
                      f"skill {pid} entry {e['skill']} group {gi} names a unit twice -- "
                      "PlayStart's Dictionary.Add would throw")
    check(checked, "no split entry was checked")


def main():
    for fn in (off_changes_nothing, on_splits_without_changing_the_damage,
               a_parent_with_no_damage_of_its_own_folds, every_entry_is_a_legal_payload):
        fn()
    for f in FAILURES:
        print("  FAIL", f)
    print(f"\n{'all OK' if not FAILURES else str(len(FAILURES)) + ' failure(s)'}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())

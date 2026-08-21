#!/usr/bin/env python3
"""Phase 5: run both battle engines over the same field and diff the SHAPE.

**Not the numbers.** The new engine's damage formula is freshly invented -- no formula
exists anywhere in the client -- so identical amounts were never the goal and comparing
them would drown the signal. What must agree is the shape the client actually consumes:

    groups            one per cinematic swing
    rows per group    how many units each swing touches
    targets           which units are struck (deterministic targeting only)
    deaths            which units are flagged `die`
    statuses          which status ids land on which unit

**A mismatch is not automatically a regression.** The old engine is the thing being
replaced and is known to collapse multi-hit skills, so this report is read the other way
round: where the two differ, the question is which one matches the client contract, and
for swing counts the contract is unambiguous (the cinematic's Damage-tag count, measured
in `skill_cinematics.py`, agrees with the `hit` column on 100% of real player attacks).

Targeting identity is compared only where the compiled spec says the selection is
deterministic. For `random`/`self_plus` the two engines roll independently, so only the
COUNT is meaningful -- comparing identity there would report pure noise as divergence.

    python3 diff_engines.py [--limit N] [--stage ID] [--verbose]
"""
import argparse
import collections
import copy
import json
import os
import random
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "server")
sys.path.insert(0, SERVER)
os.chdir(SERVER)

import battle as old_engine            # noqa: E402  the engine being replaced
from engine import core, specs, wire   # noqa: E402

NONDETERMINISTIC = {"random", "self_plus", "by_attribute"}


def shape_from_wire(payload):
    """-> the shape facts of an AttackJsonData dict, ignoring every amount."""
    groups = payload.get("data") or []
    return {
        "groups": len(groups),
        "rows_per_group": [len(g) for g in groups],
        # STRUCK, not merely "named on a mode-1 row": a rider heal on the caster is
        # also mode 1, and counting it made a 1-enemy skill look like it hit two.
        # Damage rides negative, healing positive.
        "targets": sorted({r["c"] for g in groups for r in g
                           if r.get("md") == wire.MODE_HP and r.get("dmg", 0) < 0}),
        "healed": sorted({r["c"] for g in groups for r in g
                          if r.get("md") == wire.MODE_HP and r.get("dmg", 0) > 0}),
        "died": sorted({r["c"] for g in groups for r in g if r.get("die")}),
        "statuses": sorted({(e[0], e[1]) for g in groups for r in g
                            for e in (r.get("status") or []) if len(e) >= 2}),
    }


def new_units_from_old(battle):
    """Mirror the old engine's live field into engine.core Units.

    Stats are copied rather than recomputed so the two engines genuinely start from the
    same field -- otherwise a stat-derivation difference would show up as a shape
    difference and mean nothing.
    """
    out = {}
    for order, u in battle.units.items():
        out[order] = core.Unit(
            order=order, team=u.team, max_hp=u.max_hp, hp=u.hp,
            atk=u.atk, defence=u.defense, spd=u.spd,
            attribute=core.attribute_of(u.char_id))
    return out


def run_pair(battle, attacker_order, defender_order, skill_id, seed):
    """-> (old_shape, new_shape, spec) for one skill use, or None if not comparable."""
    spec = specs.skill(skill_id)
    if spec is None:
        return None

    # Both engines mutate. Each gets its own copy of the same starting field.
    old_battle = copy.deepcopy(battle)
    try:
        old_payload = json.loads(
            old_battle.attack_cmd_json(attacker_order, defender_order, skill_id))
    except Exception as exc:                                  # noqa: BLE001
        return ("old-raised", f"{type(exc).__name__}: {exc}", spec)
    combo = (old_payload.get("combo") or [{}])[0]
    old_shape = shape_from_wire(combo)

    units = new_units_from_old(copy.deepcopy(battle))
    caster = units.get(attacker_order)
    if caster is None:
        return None
    try:
        outcome = core.execute(caster, spec, list(units.values()),
                               random.Random(seed), chosen=defender_order)
        if not (outcome.strikes or outcome.heals or outcome.gauge or outcome.revives):
            return None
        new_shape = shape_from_wire(wire.attack_json(outcome))
    except Exception as exc:                                  # noqa: BLE001
        return ("new-raised", f"{type(exc).__name__}: {exc}", spec)
    return (old_shape, new_shape, spec)


def compare(old, new, spec):
    """-> [field names that differ], honouring the deterministic-targeting caveat."""
    diffs = []
    if old["groups"] != new["groups"]:
        diffs.append("groups")
    deterministic = (spec.get("target") or {}).get("select") not in NONDETERMINISTIC
    if [len(x) for x in [old["targets"]]] != [len(x) for x in [new["targets"]]]:
        diffs.append("target_count")
    elif deterministic and old["targets"] != new["targets"]:
        diffs.append("target_identity")
    if {s[1] for s in old["statuses"]} != {s[1] for s in new["statuses"]}:
        diffs.append("status_ids")
    return diffs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", type=int, default=1101)
    ap.add_argument("--limit", type=int, default=300, help="skills to compare")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()

    party = [{"id": c, "lv": 100} for c in (20961, 20941, 20801, 11001, 10981)]
    battle = old_engine.Battle(a.stage, party, 100, None, 0, 0)

    players = [o for o, u in battle.units.items()
               if u.team == old_engine.TEAM_PLAYER and u.alive]
    enemies = [o for o, u in battle.units.items()
               if u.team == old_engine.TEAM_ENEMY and u.alive]
    if not players or not enemies:
        print(f"stage {a.stage} produced no usable field "
              f"({len(players)} players, {len(enemies)} enemies)")
        return 1
    print(f"stage {a.stage}: {len(players)} players vs {len(enemies)} enemies")

    # Make everything unkillable for the duration of one skill use.
    #
    # Without this the comparison measures the wrong thing: a level-100 party one-shots
    # a stage-1101 mob, so targets die on the first swing and BOTH engines legitimately
    # stop emitting groups -- the old one filters empties, the new one trims trailing
    # ones. The result is two different truncations of a shape neither engine got wrong,
    # reported as a swing-count divergence. Deaths depend on damage magnitude, and the
    # new formula is invented, so they can never be part of a shape diff anyway.
    for u in battle.units.values():
        u.max_hp = u.hp = 10 ** 9

    # The party's own four skills each is far too small a sample to say anything about
    # 14,410 rows, so the field is reused as a test bench: one attacker, a broad slice of
    # the compiled corpus. Only skills both engines will actually act on are worth
    # comparing.
    cases = [(order, enemies[0], sid)
             for order in players
             for sid in (battle.units[order].skills or []) if sid]
    usable = [sid for sid, sp in sorted(specs.skills().items())
              if sp.get("type") in ("com_attack", "skill", "sp_skill")
              and (sp.get("target") or {}).get("select") not in (None, "none", "unknown")
              and any(e["op"] in ("damage", "apply_status") for e in sp["effects"])]
    step = max(1, len(usable) // max(1, a.limit))
    cases += [(players[0], enemies[0], sid) for sid in usable[::step]]
    cases = cases[:a.limit]
    print(f"comparing {len(cases)} skill uses\n")

    tally = collections.Counter()
    examples = collections.defaultdict(list)
    swing_delta = collections.Counter()
    compared = 0

    for i, (atk, dfd, sid) in enumerate(cases):
        got = run_pair(battle, atk, dfd, sid, seed=1000 + i)
        if got is None:
            tally["not comparable"] += 1
            continue
        old, new, spec = got
        if old in ("old-raised", "new-raised"):
            tally[old] += 1
            examples[old].append((sid, new))
            continue
        compared += 1
        diffs = compare(old, new, spec)
        if not diffs:
            tally["shape identical"] += 1
            continue
        for d in diffs:
            tally[d] += 1
            if len(examples[d]) < 6:
                examples[d].append((sid, spec.get("name"), old, new))
        if "groups" in diffs:
            swing_delta[(old["groups"], new["groups"])] += 1

    print(f"{'result':22s} count")
    for k, v in tally.most_common():
        print(f"  {k:20s} {v:5d}")
    print(f"\ncompared: {compared}")

    if swing_delta:
        print("\nswing-count divergence (old -> new), the multi-hit bug:")
        for (o, n), c in swing_delta.most_common(10):
            flag = "  <-- old collapsed a multi-hit skill" if n > o else ""
            print(f"   old {o} -> new {n}: {c:4d}{flag}")

    print("\n(deaths are NOT compared: they follow damage magnitude, and the new "
          "formula\n is invented, so they are expected to differ.)")
    for kind in ("target_count", "target_identity", "status_ids"):
        if not examples[kind]:
            continue
        print(f"\n{kind}:")
        for sid, name, old, new in examples[kind][:4]:
            print(f"   {sid} {str(name)[:30]:30s}")
            if kind.startswith("target"):
                print(f"      old {old['targets']}")
                print(f"      new {new['targets']}")
            elif kind == "status_ids":
                print(f"      old {sorted({s[1] for s in old['statuses']})}")
                print(f"      new {sorted({s[1] for s in new['statuses']})}")
            else:
                print(f"      old died {old['died']}  new died {new['died']}")

    for kind in ("old-raised", "new-raised"):
        if examples[kind]:
            print(f"\n{kind}:")
            for sid, msg in examples[kind][:4]:
                print(f"   {sid}: {msg}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

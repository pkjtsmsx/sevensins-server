#!/usr/bin/env python3
"""Calibrate the damage formula against the game's own star conditions.

**Why this signal.** No damage formula exists in the client, so the numbers in
`engine/formula.py` are invented and there is nothing to check them against -- except
that the designers already encoded their intended clear speed in the stage table.
`_rating_datas` rows are `[type, item_id, count, threshold]`, and type 3 is "clear within
`threshold` turns" (see `Battle.rating_flags`). Stage 1-1 wants 3 stars in 12 turns.

`_stagelv` is the MOB level for that stage -- confirmed against the live game, where 6-4
(stage 6104, `_stagelv` 43) fields level-43 mobs. It is not a party level: the same stage
exists at difficulty 1/2/3 as 6104/6204/6304 with `_stagelv` 43/113/221, and the column
runs past 600, well beyond the character cap. Only the difficulty-1 band under the cap is
sampled, where "party level == mob level" is a fair reading.

Pair that with `_stagelv` and there is a real target: **a party at `_stagelv` should clear most campaign stages inside the 3-star
turn limit, and miss it on the ones meant to be hard.** Too fast means the curve is hot,
too slow means it is cold.

This is deliberately run at `_stagelv`, not at level 100. A level-100 party on stage 1-1
is 100 levels over the content and one-shots everything -- which says nothing about the
formula.

    python3 calibrate_damage.py [--stages N] [--max-turns N] [--verbose]
"""
import argparse
import collections
import os
import random
import statistics
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "server")
sys.path.insert(0, SERVER)
os.chdir(SERVER)
os.environ["SEVENSINS_BATTLE_ENGINE"] = "new"

import battle as bt          # noqa: E402
import design_data as dd     # noqa: E402

RATING_CLEAR_WITHIN_TURNS = 3

# A level-appropriate party of five. Real accounts vary wildly; what matters for
# calibration is that the same party is used at every stage's own level, so the only
# thing changing is the content.
PARTY = [20961, 20941, 20801, 11001, 10981]

# Characters cap here, so a stage whose `_stagelv` exceeds it is not asking for levels.
LEVEL_CAP = 100


def turn_limits(stage_row):
    """-> the type-3 thresholds on this stage, tightest last."""
    out = []
    for i in range(1, 5):
        row = dd.csv_ints(stage_row.get(f"_rating_datas{i}"))
        if len(row) >= 4 and row[0] == RATING_CLEAR_WITHIN_TURNS:
            out.append(row[3])
    return sorted(out, reverse=True)


def simulate(stage_id, level, max_turns):
    """Run a whole fight on auto. -> (turns, outcome).

    Uses the old `Battle` for wave/turn flow -- it owns that, and phase 5 only moved
    skill resolution -- with SEVENSINS_BATTLE_ENGINE=new doing the resolving.
    """
    party = [{"id": c, "lv": level} for c in PARTY]
    try:
        b = bt.Battle(stage_id, party, level, None, 0, 0)
    except Exception as exc:                                  # noqa: BLE001
        return None, f"setup: {type(exc).__name__}"

    rng = random.Random(stage_id)
    for turn in range(1, max_turns + 1):
        if not [u for u in b.units.values()
                if u.team == bt.TEAM_PLAYER and u.alive]:
            return turn, "wipe"
        if not [u for u in b.units.values()
                if u.team == bt.TEAM_ENEMY and u.alive]:
            if b.wave >= b.wave_max:
                return turn, "clear"
            b.advance_wave()
            continue
        actor = b.acting_unit()
        if actor is None:
            b.end_turn()
            continue
        foe_team = bt.TEAM_ENEMY if actor.team == bt.TEAM_PLAYER else bt.TEAM_PLAYER
        move = b.auto_move(foe_team)
        if not move:
            b.end_turn()
            continue
        attacker, defender, skill, slot = move
        try:
            b.attack_cmd_json(attacker, defender, skill)
        except Exception as exc:                              # noqa: BLE001
            return turn, f"raised: {type(exc).__name__}: {exc}"[:70]
        b.spend_skill(attacker, slot)
        b.end_turn()
    return max_turns, "timeout"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stages", type=int, default=40)
    ap.add_argument("--max-turns", type=int, default=60)
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()

    stages = dd.rows("stage") or {}
    # Difficulty-1 campaign stages with `_stagelv` inside the character level cap.
    #
    # `_stagelv` is a stage POWER rating, not a party level: stage 6-4 exists at
    # difficulty 1/2/3 with _stagelv 43/113/221, and the table runs past 600. Past the
    # level cap the content expects rank, gear and transcendence rather than levels, so
    # "party level == _stagelv" is only a fair reading in the low band. Sampling outside
    # it builds level-609 characters and measures nothing.
    usable = [(sid, r) for sid, r in sorted(stages.items())
              if 1 <= (r.get("_stagelv") or 0) <= LEVEL_CAP
              and int(r.get("_difficulty") or 0) == 1
              and turn_limits(r) and (r.get("_mobGroup_datas") or "")]
    if not usable:
        print("no usable stages")
        return 1
    step = max(1, len(usable) // a.stages)
    sample = usable[::step][:a.stages]
    print(f"{len(usable)} campaign stages with a turn limit; sampling {len(sample)}\n")

    print(f"{'stage':>7} {'lv':>4} {'3star':>6} {'turns':>6}  {'result':24s} stage name")
    tally = collections.Counter()
    ratios = []
    for sid, r in sample:
        limits = turn_limits(r)
        tight = limits[-1]
        lv = int(r.get("_stagelv") or 1)
        turns, outcome = simulate(sid, lv, a.max_turns)
        if turns is None:
            tally["setup failed"] += 1
            continue
        if outcome == "clear":
            verdict = "3-STAR" if turns <= tight else (
                "cleared, missed 3-star" if turns <= limits[0] else "cleared, slow")
            tally[verdict] += 1
            ratios.append(turns / tight)
        else:
            verdict = outcome
            tally[outcome] += 1
        # The stage NAME matters when reading a wipe. Main campaign and side content are
        # mixed together in this table, and the side modes (Kizuna "Bond of X", the
        # arena ranking rounds, the "Time House of the Souls" challenge tower) are meant
        # to be hard and to want specific team comps -- a wipe there is not evidence the
        # damage curve is wrong.
        name = str(r.get("_stage_name_en") or r.get("_stage_name") or "")[:28]
        print(f"{sid:>7} {lv:>4} {tight:>6} {turns:>6}  {verdict:24s} {name}")

    print(f"\n{'result':28s} count")
    for k, v in tally.most_common():
        print(f"  {k:26s} {v:4d}")

    if ratios:
        med = statistics.median(ratios)
        print(f"\nturns / 3-star limit -- median {med:.2f}, "
              f"min {min(ratios):.2f}, max {max(ratios):.2f}")
        print("  (1.0 = exactly on the 3-star pace)")
        if med < 0.35:
            print("  VERDICT: damage is HOT -- clears far inside the intended pace.")
        elif med > 1.1:
            print("  VERDICT: damage is COLD -- misses the intended pace.")
        else:
            print("  VERDICT: within the intended band.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

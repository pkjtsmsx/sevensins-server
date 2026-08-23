#!/usr/bin/env python3
"""Tier-1 move selection: the behaviours, not the arithmetic.

    python3 test_battle_ai.py

Every check here is something the OLD chooser got wrong -- it ranked skills by a
percentage grepped out of localized prose and hit whoever came first in the unit dict.
See docs/BATTLE_AI_PLAN.md.

Scenarios are built by taking a REAL battle off a real stage and then overwriting the
handful of fields under test (hp, skills, statuses), so `Battle`'s own machinery --
usable_slots, _forced_target, the turn queue -- is the machinery being exercised. Skills
are synthetic where the point is a controlled comparison (equal coefficients, differing
only in breadth or basis); the compiled pack has no clean pair to make some of those
points with.

Uses its own SEVENSINS_ACCOUNTS tempdir so it never touches real save data.
"""
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

_TMP = tempfile.mkdtemp(prefix="sevensins-ai-test-")
os.environ["SEVENSINS_ACCOUNTS"] = _TMP        # before player_state imports

import battle as bt                                            # noqa: E402
import battle_ai                                               # noqa: E402
import player_state as ps                                      # noqa: E402
from player_state.core import _default as _mk, _seed_roster as _seed   # noqa: E402
from engine import specs as _specs                             # noqa: E402
from engine import status as _status                           # noqa: E402

_fail = 0


def check(name, cond, detail=""):
    global _fail
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        _fail += 1


# --- scenario plumbing --------------------------------------------------------------

ENEMY = "enemy_single"
SINGLE = {"count": 1, "group": "enemy", "label": "1 enemy", "select": "count"}
ALL_ENEMIES = {"count": None, "group": "enemy", "label": "All enemies", "select": "all"}


def _spec(sid, name, effects, target, swings=1):
    """A minimal compiled-skill spec, registered so `specs.skill(sid)` finds it."""
    spec = {"id": sid, "name": name, "cd": 0, "charge": 0, "cinematic": None,
            "effects": effects, "group": 0, "lv": 1, "swings": swings,
            "swings_from": "test", "target": target, "type": 1}
    _specs.skills()[sid] = spec
    return sid


def _damage(coefficient, basis="ATK"):
    return {"op": "damage", "basis": basis, "coefficient": coefficient,
            "prose_times": None, "source": "test"}


# Synthetic ids well outside the pack's range so they cannot collide with a real row --
# which also means `dd.row("skill", id)` misses and their cooldown reads 0, keeping the
# comparisons about the thing under test.
S_HIT = _spec(990000001, "Test Strike", [_damage(1.0)], SINGLE)
S_AOE = _spec(990000002, "Test Sweep", [_damage(1.0)], ALL_ENEMIES)
S_ATK = _spec(990000003, "Test Big Swing", [_damage(2.0)], SINGLE)
S_MAXHP = _spec(990000004, "Test Percent Nuke", [_damage(0.5, "MAX_HP")], SINGLE)

# Real skills, where a real one makes the point better than a synthetic one.
R_HEAL = 2000171         # Northern Cross -- heal only, ally target
R_REVIVE = 2094162       # Serum Injection -- revive (+ a status), no damage
R_STUN = 100000101       # Blink Slash -- damage + Stun (610) on one enemy
STUN_ID = 610
TAUNT_ID = 608


def field(acting="101"):
    """A real 2v3 battle with `acting` at the head of the queue."""
    st = _mk(1000001)
    _seed(st)
    b = bt.Battle(1600002, ps.battle_team(st, 0), 100, None, 0, 0)
    order = [acting] + [o for o in b.units if o != acting]
    b.turn_order = order
    return b


def arm(unit, *skills):
    unit.skills = list(skills)
    unit.cooldowns = [0] * len(unit.skills)


def afflict(unit, status_id, name, kind, source=None, remaining=3):
    unit.statuses.append(_status.Active(
        status_id=status_id, name=name, kind=kind, category="debuff",
        remaining=remaining, source_order=source))


def twins(b, hp_low, hp_full, atk=2000, low="104"):
    """Make '103' and '104' identical except for HP, and take '105' off the field.

    `low` defaults to '104', the SECOND of the pair, because '103' is what the greedy
    chooser's `targets[0]` lands on by accident -- putting the killable enemy there would
    let a test pass on a coincidence.
    """
    full = "103" if low == "104" else "104"
    for order in ("103", "104"):
        u = b.units[order]
        u.max_hp, u.atk, u.defence = 50000, atk, 100
        arm(u, S_HIT)
    b.units[low].hp = hp_low
    b.units[full].hp = hp_full
    b.units["105"].hp = 0
    return b.units[low], b.units[full]


# --- the behaviours -----------------------------------------------------------------

print("\nlethal and focus fire:")
b = field()
low, full = twins(b, hp_low=200, hp_full=50000)
arm(b.units["101"], S_HIT)
b.units["101"].atk = 800
move = b.auto_move(2)
check("the killing blow is chosen over chip damage on a healthy target",
      move[1] == "104", f"targeted {move[1] if move else None}")
# The old chooser is what this replaces; assert it really did get this wrong, so the
# test is anchored to a behaviour change rather than to a number.
old = b._auto_move_greedy(2)
check("  ...which the greedy chooser did not", old[1] == "103",
      f"greedy hit {old[1]}, so this scenario proves nothing")

# Sub-lethal focus fire, which a DISCRETE kill bonus does not produce: neither target can
# be finished this turn, so both score the same raw damage and the pick falls to the
# tie-break unless partial progress toward a kill is credited too.
b = field()
hurt, fresh = twins(b, hp_low=20000, hp_full=50000)
arm(b.units["101"], S_HIT)
b.units["101"].atk = 400                       # cannot kill either one this turn
move = b.auto_move(2)
check("damage that kills nobody still goes to the weakest enemy",
      move[1] == "104", f"targeted {move[1] if move else None}")


print("\nhealing:")
b = field()
twins(b, hp_low=50000, hp_full=50000)
arm(b.units["101"], S_HIT, R_HEAL)
b.units["101"].atk = 1
for order in ("101", "102"):
    b.units[order].hp = b.units[order].max_hp          # nobody is hurt
move = b.auto_move(2)
check("a healer at full party HP attacks instead of healing", move[3] == 0,
      f"chose slot {move[3] if move else None}")
b.units["102"].hp = 1                                   # ...and now someone is
move = b.auto_move(2)
check("  ...but heals once an ally is actually hurt", move[3] == 1,
      f"chose slot {move[3] if move else None}")


print("\nrevival:")
b = field()
twins(b, hp_low=50000, hp_full=50000)
arm(b.units["101"], S_HIT, R_REVIVE)
b.units["101"].atk = 1
move = b.auto_move(2)
check("a revive is not cast with nobody down", move[3] == 0,
      f"chose slot {move[3] if move else None}")
b.units["102"].hp = 0
move = b.auto_move(2)
check("  ...and is cast once an ally has fallen", move[3] == 1,
      f"chose slot {move[3] if move else None}")


print("\nredundant control:")
b = field()
low, full = twins(b, hp_low=50000, hp_full=50000)
arm(b.units["101"], R_STUN)
# '103' sorts FIRST, so a tie-break alone would pick it -- the un-stunned '104' can only
# win on the status term.
afflict(b.units["103"], STUN_ID, "Stun", "control")
move = b.auto_move(2)
check("a stun goes to the unstunned enemy, not the already-stunned one",
      move[1] == "104", f"targeted {move[1] if move else None}")


print("\nbreadth:")
b = field()
for order in ("103", "104", "105"):
    u = b.units[order]
    u.max_hp = u.hp = 50000
    u.defence = 100
arm(b.units["101"], S_HIT, S_AOE)
move = b.auto_move(2)
check("an all-enemies skill outranks the same coefficient on one target",
      move[3] == 1, f"chose slot {move[3] if move else None}")
# With only one enemy left there is nothing for breadth to buy, and the two are equal.
for order in ("104", "105"):
    b.units[order].hp = 0
move = b.auto_move(2)
check("  ...and stops being preferred when only one enemy is left",
      move[3] in (0, 1), f"chose slot {move[3] if move else None}")


print("\ndamage basis:")
b = field()
boss = b.units["103"]
boss.max_hp = boss.hp = 400000                 # 0.5 x max_hp dwarfs 2.0 x ATK
boss.defence = 100
for order in ("104", "105"):
    b.units[order].hp = 0
arm(b.units["101"], S_ATK, S_MAXHP)
b.units["101"].atk = 2000
move = b.auto_move(2)
check("a %-of-max-HP nuke beats a bigger printed ATK coefficient on a boss",
      move[3] == 1, f"chose slot {move[3] if move else None}")


print("\ntaunt:")
b = field()
low, full = twins(b, hp_low=200, hp_full=50000)
arm(b.units["101"], S_HIT)
b.units["101"].atk = 800
afflict(b.units["101"], TAUNT_ID, "Taunt", "control", source="104")
move = b.auto_move(2)
check("a taunted attacker hits the taunter, not the killable enemy",
      move[1] == "104", f"targeted {move[1] if move else None}")
check("  ...and the taunter was the only candidate scored",
      battle_ai._candidates(b, b.units["101"], [low, full], b.units["104"])
      == [(0, "104", _specs.skill(S_HIT))])


print("\ndeterminism:")
b = field()
twins(b, hp_low=200, hp_full=50000)
arm(b.units["101"], S_HIT, S_AOE)
first = b.auto_move(2)
check("the same state gives the same move", b.auto_move(2) == first)
saved = b.to_state()
restored = bt.restore_battle(saved)
for order in ("103", "104", "105"):
    restored.units[order].max_hp = b.units[order].max_hp
    restored.units[order].atk = b.units[order].atk
    arm(restored.units[order], *b.units[order].skills)
arm(restored.units["101"], *b.units["101"].skills)
check("  ...and survives a save/restore", restored.auto_move(2) == first,
      f"{restored.auto_move(2)} != {first}")

# An enemy runs the softmax policy, so it is the case most at risk of being irreproducible.
b = field(acting="103")
twins(b, hp_low=50000, hp_full=50000)
arm(b.units["103"], S_HIT, S_AOE)
enemy_first = b.auto_move(1)
check("an enemy's imperfect pick is reproducible too", b.auto_move(1) == enemy_first)


print("\nbudget:")
b = field()
for order in ("103", "104", "105"):
    b.units[order].max_hp = b.units[order].hp = 50000
# The stage's OWN skills, deliberately: a stripped two-skill unit measures nothing, and
# what has to stay affordable is a real cast's real candidate set.
N = 200
t = time.perf_counter()
for _ in range(N):
    b.auto_move(2)
per_ms = (time.perf_counter() - t) / N * 1000.0
# Desktop bound. The device ratio was measured at 4.0x (docs/BATTLE_AI_PLAN.md), so 1.5 ms
# here is the ~6 ms on-phone budget the plan commits to. A regression guard: the scorer
# will be tempted to grow.
check(f"a decision costs under 1.5 ms on desktop ({per_ms:.2f} ms)", per_ms < 1.5)


print(f"\n{_fail} failure(s)" if _fail else "\nALL PASSED")
sys.exit(1 if _fail else 0)

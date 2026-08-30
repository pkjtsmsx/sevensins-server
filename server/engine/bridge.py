"""Run a turn on the new engine against the OLD engine's live `Battle`. Phase-5 cutover.

This exists so the two engines can be swapped under a running server without forking the
whole battle state machine. The old `Battle` still owns waves, turn order, cooldowns,
rewards and the socket; only the *resolution of one skill use* moves across.

    SEVENSINS_BATTLE_ENGINE=old     # escape hatch; the new engine is the default now

The new engine owns damage, targeting, swing structure and the `data` payload.

**There is nothing to mirror.** `battle.Unit` subclasses `engine.core.Unit`, so both
engines operate on the same objects: `execute` mutates the battle's own units directly
and this module only records the old engine's separate bookkeeping (the damage tallies
the clear-rating stars read, and the move gauge, which travels in `sync` rather than in
`data`).

**Statuses this path applies are still COSMETIC.** They reach the wire -- the client
shows the icon and counts the duration down itself -- and now they also land on the
unit's own `statuses` list, since it is one shared object. But nothing ticks or reads
them on the new path: expiry, the stat and damage modifiers, and turn skipping all live
in the old engine's `battle_effects`, which is keyed to its own status representation.
So a Freeze shows, and the target still acts.

That is the largest remaining gap, and the shared unit model is what makes closing it a
deletion rather than another sync: the state is already in the right place.
"""
import random

from . import core, specs, status as _status, wire

SCV_FULL = 100.0


def _write_back(battle, outcome):
    """Record what the turn did on the battle's own bookkeeping.

    HP no longer needs copying -- `execute` already mutated these exact objects. What
    still has to happen here is the OLD engine's own accounting: the clear-rating stars
    read the damage tallies, so skipping them would silently change star awards.
    """
    # Cooldowns, like the gauge, are server-authoritative and reach the client through
    # the skill-list payload rather than as a DamageInfo row. Clamped at 0 -- a refresh
    # cannot make a skill "more than ready".
    for c in _flat_cooldowns(outcome):
        u = battle.units.get(c["target"])
        if u is None or not getattr(u, "cooldowns", None):
            continue
        for i in range(len(u.cooldowns)):
            u.cooldowns[i] = max(0, u.cooldowns[i] + int(c["turns"]))

    # The move gauge travels in `sync`, NOT as a mode-4 DamageInfo row (see wire.py).
    # Applied here so the next BattleCmd carries the new Scv.
    for g in _flat_gauge(outcome):
        u = battle.units.get(g["target"])
        if u is None or g.get("percent") is None:
            continue
        if g["target"] == outcome.caster:
            # The caster is mid-turn at a full bar, and `end_turn` is about to zero it.
            # Stashed instead, and applied there once the bar has been spent -- which is
            # what "After the action, the caster's Move Gauge will increase N%" means.
            if float(g["percent"]) > 0 and _status.blocks_gauge_gain(u):
                continue
            u.pending_scv = getattr(u, "pending_scv", 0.0) + float(g["percent"])
        else:
            delta = float(g["percent"])
            # Symmetric refusals: Steady ("will not decrease") blocks a cut, Headwind
            # ("will not increase") blocks a boost. A gauge GAIN bypasses fill_gauge
            # entirely, so it has to be checked here too.
            if delta < 0 and _status.blocks_gauge_loss(u):
                continue
            if delta > 0 and _status.blocks_gauge_gain(u):
                continue
            u.scv = max(0.0, min(SCV_FULL, float(u.scv) + delta))

    dealt = outcome.total_damage()
    caster = battle.units.get(outcome.caster)
    if caster is not None:
        caster.dmg_done += dealt
    battle.damage_sum += dealt
    for st in _flat_strikes(outcome):
        victim = battle.units.get(st.target)
        if victim is not None:
            victim.dmg_taken += st.amount


def _flat_gauge(outcome):
    out = list(outcome.gauge)
    for ch in outcome.children:
        out.extend(_flat_gauge(ch))
    return out

def _flat_cooldowns(outcome):
    out = list(outcome.cooldowns)
    for child in outcome.children:
        out += _flat_cooldowns(child)
    return out



def _flat_strikes(outcome):
    out = list(outcome.strikes)
    for ch in outcome.children:
        out.extend(_flat_strikes(ch))
    return out


def attack_combo(battle, attacker_order, defender_order, skill_id, rng=None):
    """-> the `combo` entry for one skill use, or None if the new engine cannot run it.

    Returning None is a deliberate escape hatch rather than a raise: a skill the new
    engine has no spec for should fall back to the old path, not kill the battle.
    """
    spec = specs.skill(skill_id)
    if spec is None:
        return None
    # The battle's OWN units go straight in: `battle.Unit` subclasses `core.Unit`, so
    # there is one model and nothing to copy. Damage, heals and statuses land on the
    # objects the old engine also reads -- which is the whole point of the shared model.
    caster = battle.units.get(attacker_order)
    if caster is None:
        return None

    # The battle's round goes in because 奇數/偶數回合 gates are only answerable with it,
    # and this is the one call site that has a battle behind it. The AI's dry runs and
    # the fuzzer pass nothing, so those gates read unevaluatable there rather than
    # guessing round 1 and firing every odd-round clause in the game.
    outcome = core.execute(caster, spec, list(battle.units.values()),
                           rng or random.Random(), chosen=defender_order,
                           round_no=getattr(battle, "round", None))
    if not (outcome.strikes or outcome.heals or outcome.gauge or outcome.revives
            or outcome.statuses):
        return None

    payload = wire.attack_json(outcome, caster_order=attacker_order, skill_id=skill_id)
    _write_back(battle, outcome)
    return payload

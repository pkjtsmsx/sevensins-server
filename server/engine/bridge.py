"""Run a turn on the new engine against the OLD engine's live `Battle`. Phase-5 cutover.

This exists so the two engines can be swapped under a running server without forking the
whole battle state machine. The old `Battle` still owns waves, turn order, cooldowns,
rewards and the socket; only the *resolution of one skill use* moves across.

    SEVENSINS_BATTLE_ENGINE=new

**Scope, stated plainly.** The new engine owns damage, targeting, swing structure and the
**Statuses this path applies are COSMETIC.** They are written to the wire -- the client
shows the icon and counts the duration down itself -- but nothing writes them into the
old `Battle`'s own status list, so the server holds no record and they have no mechanical
effect. A Freeze lands, the icon appears, and the target acts on its next turn anyway.

That is the largest remaining gap and the reason this flag is A/B work rather than a
replacement: on the new path the engine applies statuses to ~21,400 sites that the old
one largely ignored, and today every one of them is decoration. Fixing it means moving
status STATE across (apply, tick, expire, and the stat/turn effects that read it), which
is a bigger change than the resolution move was -- the old engine ticks statuses inside
`end_turn` and reads them in damage, targeting and turn skipping.

The mirror is deliberately one-way per call: build `core.Unit` mirrors, resolve, then
write back only HP. Anything else written back would be the new engine quietly reaching
into a model it does not own.
"""
import random

from . import core, specs, wire

SCV_FULL = 100.0


def _mirror(battle):
    """-> {order: core.Unit} reflecting the old battle's live field."""
    out = {}
    for order, u in battle.units.items():
        out[order] = core.Unit(
            order=order, team=u.team, max_hp=u.max_hp, hp=u.hp,
            atk=u.atk, defence=u.defense, spd=u.spd,
            attribute=core.attribute_of(u.char_id))
    return out


def _write_back(battle, mirrors, outcome):
    """Push HP back onto the old units, and nothing else.

    Damage totals are also fed to the old battle's tallies because the clear-rating
    stars read them; skipping that would silently change star awards on the new path.
    """
    for order, m in mirrors.items():
        u = battle.units.get(order)
        if u is None:
            continue
        u.hp = max(0, int(m.hp))

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
            u.pending_scv = getattr(u, "pending_scv", 0.0) + float(g["percent"])
        else:
            u.scv = max(0.0, min(SCV_FULL, float(u.scv) + float(g["percent"])))

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
    mirrors = _mirror(battle)
    caster = mirrors.get(attacker_order)
    if caster is None:
        return None

    outcome = core.execute(caster, spec, list(mirrors.values()),
                           rng or random.Random(), chosen=defender_order)
    if not (outcome.strikes or outcome.heals or outcome.gauge or outcome.revives):
        return None

    payload = wire.attack_json(outcome, caster_order=attacker_order, skill_id=skill_id)
    _write_back(battle, mirrors, outcome)
    return payload

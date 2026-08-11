#!/usr/bin/env python3
"""Runtime skill-effect engine -- SPINE.

Consumes the two data artifacts the mapping produced --
`battle_data/status_catalog.json` (what each buff/debuff DOES) and
`battle_data/skill_effects.json` (each skill as an ordered list of effect ops) -- and
resolves a skill USE into concrete outcomes: damage numbers, status applications, heals,
gauge/CD changes. The battle is server-authoritative (the client holds no mechanics; see
memory sevensins-battle), so this is where those mechanics live.

This module is the spine: data loaders, the Status model, stat/target helpers, the
outcome shape, and `execute_skill`/`run_phase`. The OPS themselves live in `ops.py` and
register into `registry.OPS`; `_apply_op` here just dispatches. Adding an op never touches
this file -- see docs/BATTLE_SKILL_PLAN.md.

Design goals:
  * DECOUPLED from battle.py's Unit/Battle -- the engine talks through a tiny protocol
    (a unit exposes .atk/.defense/.hp/.max_hp/.team/.spd/.statuses/.alive/.order), so it
    unit-tests against mocks and wires into battle.py without a circular import.
  * SAFE-BY-DEFAULT -- only skills flagged `complete` are trusted; anything else falls
    back to the caller's simple-damage path.
  * Never throws on unknown data -- an unrecognized status/target/op degrades to a no-op.
"""
import json
import os
import random

from .registry import OPS

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(os.path.dirname(HERE), "battle_data")

# Chance-gated effects (e.g. "40% chance to drain the gauge") roll against this. Use
# set_rng() to make a battle reproducible in a test (reassigning the module global from
# outside the package would not affect the dispatcher, which reads this name directly).
_rng = random.Random()


def set_rng(rng):
    """Replace the RNG the chance-gate rolls against (tests pass a seeded/stub Random)."""
    global _rng
    _rng = rng


_catalog = None
_skills = None
_status_icons = None


def catalog():
    global _catalog
    if _catalog is None:
        with open(os.path.join(DATA, "status_catalog.json"), encoding="utf-8") as f:
            _catalog = json.load(f)
    return _catalog


def skill_effects(skill_id):
    """-> the parsed record for a skill id, or None. Keys are strings in the file."""
    global _skills
    if _skills is None:
        with open(os.path.join(DATA, "skill_effects.json"), encoding="utf-8") as f:
            _skills = json.load(f)
    return _skills.get(str(skill_id))


def is_complete(skill_id):
    """True iff every clause of the skill parsed -- the engine trusts only these."""
    rec = skill_effects(skill_id)
    return bool(rec and rec.get("complete"))


def status_skill_id(name):
    """The DesignSkillForm row id the client's DamageInfo.status needs to draw `name`'s
    icon, or None if we have no graphic for it (built by tools/build_status_icons.py --
    the visual lives on a _type-6 STATUS skill whose _statusID picks the DesignStatusForm
    graphic). None -> the caller emits no icon entry, never a wrong one."""
    global _status_icons
    if _status_icons is None:
        try:
            with open(os.path.join(DATA, "status_icons.json"), encoding="utf-8") as f:
                _status_icons = json.load(f)
        except FileNotFoundError:
            _status_icons = {}
    return _status_icons.get(name)


# ---- status instances on a unit --------------------------------------------
class Status:
    """One active status on a unit: its catalog name, remaining turns and stack count."""

    __slots__ = ("name", "remaining", "stacks", "definition")

    def __init__(self, name, remaining, definition):
        self.name = name
        self.remaining = remaining          # int turns, or "battle" for permanent
        self.stacks = 1
        self.definition = definition or {}

    def tick(self):
        """-> True if the status expired this tick."""
        if self.remaining == "battle":
            return False
        self.remaining -= 1
        return self.remaining <= 0

    def wire(self):
        """The BattleDoll status entry: catalog needs iconId; the runtime carries the
        name + remaining so the client shows the right icon and turn count."""
        return {"name": self.name,
                "turns": self.remaining if self.remaining != "battle" else -1,
                "stacks": self.stacks}


def _duration(effect_dur, status_def):
    """A skill can override a status's duration ('inflicts Fracture for 3 turns'); else
    use the catalog default; else 1."""
    if effect_dur not in (None, ""):
        return effect_dur
    d = status_def.get("duration")
    return d if d is not None else 1


STATS = ("ATK", "DEF", "SPD")


def stat_multiplier(statuses, stat):
    """Combined multiplier (1.0 = unchanged) for a stat from a unit's active statuses.
    Percent mods stack additively -- ATK-35% and ATK-35% -> x0.30 -- which matches how
    the descriptions read ('stacks up to N times')."""
    pct = 0
    for st in statuses:
        for mod in st.definition.get("stat_mods", []):
            if mod.get("stat") == stat and mod.get("unit") == "pct":
                pct += mod["value"] * st.stacks
    return max(0.0, 1.0 + pct / 100.0)


def flat_bonus(statuses, stat):
    """Summed FLAT stat delta (e.g. Entangled SPD-200)."""
    total = 0
    for st in statuses:
        for mod in st.definition.get("stat_mods", []):
            if mod.get("stat") == stat and mod.get("unit") == "flat":
                total += mod["value"] * st.stacks
    return total


def damage_taken_multiplier(statuses):
    """How much MORE/less damage the unit takes, from damage_taken mods + flags."""
    pct = 0
    for st in statuses:
        for mod in st.definition.get("stat_mods", []):
            if mod.get("stat") == "damage_taken":
                pct += mod["value"] * st.stacks
        if "damage_taken_up" in st.definition.get("flags", []):
            pct += 25                      # qualitative "increases damage taken" (Stun)
    return max(0.0, 1.0 + pct / 100.0)


def is_immobilized(statuses):
    """Skips its action this turn (Stun/Freeze/Daze)."""
    for st in statuses:
        flags = st.definition.get("flags", [])
        if "immobilize" in flags or "skip_action" in flags:
            return True
    return False


# ---- target resolution ------------------------------------------------------
def resolve_targets(token, attacker, primary, allies, enemies):
    """Map a parsed target token to concrete units.

    token: e.g. "self", "enemy_target", "all_allies", "highest_spd_enemy".
    attacker: the acting unit. primary: the chosen primary enemy (may be None).
    allies/enemies: unit lists from the attacker's point of view.
    """
    if token == "self":
        return [attacker]
    if token == "all_allies":
        return [u for u in allies if u.alive]
    if token == "all_enemies":
        return [u for u in enemies if u.alive]
    if token and token.startswith("highest_"):
        stat = token[len("highest_"):].rsplit("_", 1)[0]      # highest_spd_enemy -> spd
        pool = [u for u in enemies if u.alive]
        if not pool:
            return []
        key = {"hp": lambda u: u.hp, "atk": lambda u: u.atk,
               "spd": lambda u: u.spd, "def": lambda u: u.defense}.get(stat)
        return [max(pool, key=key)] if key else [pool[0]]
    # "enemy_target" / default: the unit the caster chose. EVERY op of the skill stays on
    # that same unit -- even if an earlier hit in the same combo killed it (the follow-on
    # debuff is then simply wasted). Re-picking a live enemy per-op would scatter one
    # combo's damage and debuffs across different units. Only with no chosen target at all
    # (a passive/AoE with no primary) do we fall back to the first live enemy.
    if primary is not None:
        return [primary]
    live = [u for u in enemies if u.alive]
    return live[:1]


# ---- immunity + status application -----------------------------------------
# Crowd-control statuses, for "immunity to crowd control" (a class, not one status).
CC_STATUSES = {"Stun", "Freeze", "Daze", "Silence", "Seal", "Sleep", "Petrify",
               "Paralyze", "Entangle", "Entangled", "Confuse", "Confusion", "Fear",
               "Taunt", "Charm", "Frostbite", "Immobilize"}


def _immune_to(unit, name):
    """True if an active immunity on the unit blocks the named status. `immune_to` may be
    a specific status, "all", or "CrowdControl" (blocks the CC_STATUSES class)."""
    for st in unit.statuses:
        imm = st.definition.get("immune_to")
        if not imm or not (st.remaining == "battle" or st.remaining > 0):
            continue
        if imm in (name, "all") or (imm == "CrowdControl" and name in CC_STATUSES):
            return True
    return False


def grant_immunity(unit, name, duration):
    """Guard a unit against a named status for a number of turns (e.g. Freeze immunity
    from a battle-start passive). Stored as an ordinary Status so it ticks and expires
    with the unit's own turns, like every other timed effect."""
    dur = duration if duration not in (None, "") else 1
    st = Status("Immune:" + str(name), dur,
                {"immune_to": name, "flags": ["immunity"]})
    unit.statuses.append(st)
    return st


def apply_status(unit, name, duration_override=None):
    """Add or refresh a status on a unit, honouring stack limits and unstackable.
    Returns the Status, or None if the name is not in the catalog OR an active immunity
    on the unit blocks it."""
    cat = catalog()
    definition = cat.get(name)
    if definition is None:
        return None                        # unknown status: no-op, never crash
    if _immune_to(unit, name):
        return None                        # blocked by an active immunity
    dur = _duration(duration_override, definition)
    flags = definition.get("flags", [])
    for st in unit.statuses:
        if st.name == name:
            if "unstackable" in flags:
                st.remaining = dur          # just refresh the timer
            else:
                st.stacks = min(st.stacks + 1,
                                definition.get("max_stacks", st.stacks + 1))
                st.remaining = dur
            return st
    st = Status(name, dur, definition)
    unit.statuses.append(st)
    return st


# ---- execution: context, dispatch, and the two entry points -----------------
def _default_reduce(_target):
    return 0.0


def _new_outcome(trusted=True):
    # `gauge`/`cd` collect turn-flow changes the caller applies to its own Unit fields
    # (scv, cooldowns), which the engine's unit protocol deliberately does not expose.
    # `status_events` = each named status applied this action, for DamageInfo.status icons.
    return {"hits": [], "self": {"heal": 0, "statuses": [], "gauge": 0},
            "gauge": [], "cd": [], "status_events": [], "deferred": [], "trusted": trusted}


class Ctx:
    """Everything an op handler needs for one skill execution. Handlers take (eff, ctx),
    resolve their own targets via ctx.targets(), and fold results into ctx.outcome."""

    __slots__ = ("attacker", "primary", "allies", "enemies", "reduce",
                 "per_target", "outcome")

    def __init__(self, attacker, primary, allies, enemies, reduce, per_target, outcome):
        self.attacker = attacker
        self.primary = primary
        self.allies = allies
        self.enemies = enemies
        self.reduce = reduce
        self.per_target = per_target
        self.outcome = outcome

    def targets(self, token):
        return resolve_targets(token, self.attacker, self.primary,
                               self.allies, self.enemies)

    def hit_entry(self, u):
        """The per-target damage/status row for unit u (created on first touch), so a
        skill's multiple hits/statuses on one unit fold into a single DamageInfo."""
        e = self.per_target.get(u.order)
        if e is None:
            e = {"target": u, "damage": 0, "died": False, "statuses": []}
            self.per_target[u.order] = e
        return e


def _apply_op(eff, ctx):
    """Dispatch ONE already-triggered effect to its registered handler. The chance gate is
    shared here so every op honours "N% chance". Unknown ops degrade to a no-op."""
    chance = eff.get("chance")
    if chance not in (None, "") and _rng.random() > chance / 100.0:
        return
    fn = OPS.get(eff.get("op"))
    if fn is not None:
        fn(eff, ctx)


def _run(effects, attacker, primary, allies, enemies, reduce, *, trusted=True):
    outcome = _new_outcome(trusted)
    ctx = Ctx(attacker, primary, allies, enemies, reduce, {}, outcome)
    for eff in effects:
        _apply_op(eff, ctx)
    outcome["hits"] = list(ctx.per_target.values())
    return outcome


# Triggers that fire as part of USING the skill (in listed order). battle_start /
# on_counter / conditional are deferred to the turn-flow layer.
IMMEDIATE_TRIGGERS = ("on_use", "before_action", "after_action", "after_attack")


def execute_skill(attacker, primary, allies, enemies, skill_id, *, damage_reduce=None):
    """Run a skill's IMMEDIATE effects (on_use/before_action/after_action/after_attack).
    -> outcome dict:

        {"hits": [ {"target": unit, "damage": int, "died": bool,
                    "statuses": [status_name, ...]}, ... ],
         "self": {"heal": int, "statuses": [...], "gauge": int},
         "gauge": [{"unit", "pct"}, ...],     # charge-gauge changes for the caller
         "cd":    [{"unit", "delta"}, ...],   # cooldown changes for the caller
         "status_events": [{"unit", "name", "round"}, ...],  # for DamageInfo.status icons
         "deferred": [effect, ...],           # battle_start/on_counter/conditional
         "trusted": bool}                     # whether the skill was `complete`

    battle_start and on_counter belong to a PASSIVE skill and are event-driven, so they
    are returned in `deferred` for the turn-flow layer to fire via run_phase().
    `conditional` effects have no machine-readable condition (the gating text was lost in
    parsing) and are returned unfired -- applying them blindly would be a guess.
    """
    reduce = damage_reduce or _default_reduce
    rec = skill_effects(skill_id)
    if not rec:
        return _new_outcome(trusted=False)
    immediate, deferred = [], []
    for block in rec.get("blocks", []):
        for eff in block.get("effects", []):
            (immediate if eff.get("trigger", "on_use") in IMMEDIATE_TRIGGERS
             else deferred).append(eff)
    outcome = _run(immediate, attacker, primary, allies, enemies, reduce,
                   trusted=bool(rec.get("complete")))
    outcome["deferred"] = deferred
    return outcome


def run_phase(skill_id, phase, attacker, primary, allies, enemies, *, damage_reduce=None):
    """Fire the effects of one skill for a single TRIGGER phase (battle_start /
    on_counter / after_action / ...), returning the execute_skill outcome shape (minus
    `deferred`). This is how a unit's PASSIVE skills act on battle events: the turn-flow
    layer calls it at unit spawn (battle_start) and when a unit is struck (on_counter),
    with the passive's owner as `attacker`."""
    reduce = damage_reduce or _default_reduce
    rec = skill_effects(skill_id)
    if not rec:
        return _new_outcome(trusted=False)
    effects = [e for b in rec.get("blocks", []) for e in b.get("effects", [])
               if e.get("trigger") == phase]
    return _run(effects, attacker, primary, allies, enemies, reduce,
                trusted=bool(rec.get("complete")))

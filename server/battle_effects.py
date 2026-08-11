#!/usr/bin/env python3
"""Runtime skill-effect engine (step 3 of the battle-engine revamp).

Consumes the two data artifacts the mapping produced --
`battle_data/status_catalog.json` (what each buff/debuff DOES) and
`battle_data/skill_effects.json` (each skill as an ordered list of effect ops) -- and
resolves a skill USE into concrete outcomes: damage numbers, status applications, heals,
gauge/CD changes. The battle is server-authoritative (the client holds no mechanics; see
memory sevensins-battle), so this is where those mechanics live.

Design goals:
  * DECOUPLED from battle.py's Unit/Battle -- the engine talks through a tiny protocol
    (a unit exposes .atk/.defense/.hp/.max_hp/.team/.spd/.statuses and a couple of
    helpers), so it can be unit-tested against mocks and wired into battle.py without a
    circular import.
  * SAFE-BY-DEFAULT -- only skills flagged `complete` in skill_effects.json are trusted;
    for anything else the caller falls back to the existing simple-damage path.
  * Never throws on unknown data -- an unrecognized status/target degrades to a no-op
    with the raw info preserved, so a half-understood skill can't crash a live battle.
"""
import json
import os
import random

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "battle_data")

# Chance-gated effects (e.g. "40% chance to drain the gauge") roll against this. It is
# a module-level Random so a battle stays server-authoritative and a test can seed it.
_rng = random.Random()

_catalog = None
_skills = None


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

    Args:
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
    # "enemy_target" / default: the chosen primary, else the first live enemy.
    if primary and primary.alive:
        return [primary]
    live = [u for u in enemies if u.alive]
    return live[:1]


# ---- applying statuses and executing a skill --------------------------------
# Triggers that fire as part of USING the skill (in listed order). battle_start /
# on_counter / conditional are deferred to the turn-flow layer (not yet wired).
IMMEDIATE_TRIGGERS = ("on_use", "before_action", "after_action", "after_attack")


def _immune_to(unit, name):
    """True if an active immunity on the unit blocks the named status."""
    for st in unit.statuses:
        imm = st.definition.get("immune_to")
        if imm in (name, "all") and (st.remaining == "battle" or st.remaining > 0):
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


def _default_reduce(_target):
    return 0.0


def _new_outcome(trusted=True):
    # `gauge`/`cd` collect turn-flow changes the caller applies to its own Unit fields
    # (scv, cooldowns), which the engine's unit protocol deliberately does not expose.
    return {"hits": [], "self": {"heal": 0, "statuses": [], "gauge": 0},
            "gauge": [], "cd": [], "deferred": [], "trusted": trusted}


def _apply_op(eff, attacker, primary, allies, enemies, reduce, per_target, outcome):
    """Apply ONE already-triggered effect, mutating units and folding results into
    `outcome`. Shared by immediate skill use and the phased passive triggers so the two
    paths can never drift. Chance-gated effects roll here and no-op when they miss."""
    op = eff.get("op")
    chance = eff.get("chance")
    if chance not in (None, "") and _rng.random() > chance / 100.0:
        return
    targets = resolve_targets(eff.get("target"), attacker, primary, allies, enemies)

    def hit_entry(u):
        e = per_target.get(u.order)
        if e is None:
            e = {"target": u, "damage": 0, "died": False, "statuses": []}
            per_target[u.order] = e
        return e

    if op == "damage":
        pct = eff.get("pct_atk", 0)
        times = eff.get("times", 1) or 1
        # Effective attacker ATK: base scaled by the attacker's own ATK statuses
        # (Keen +, Fracture -) plus any flat mod.
        eff_atk = (attacker.atk * stat_multiplier(attacker.statuses, "ATK")
                   + flat_bonus(attacker.statuses, "ATK"))
        for u in targets:
            for _ in range(times):
                base = eff_atk * pct / 100.0 * (1.0 - reduce(u))
                dmg = max(1, int(base * damage_taken_multiplier(u.statuses)))
                u.hp = max(0, u.hp - dmg)
                e = hit_entry(u)
                e["damage"] += dmg
                e["died"] = not u.alive
    elif op == "apply_status":
        for u in targets:
            st = apply_status(u, eff.get("status"), eff.get("duration"))
            if st:
                (outcome["self"]["statuses"] if u is attacker
                 else hit_entry(u)["statuses"]).append(st.name)
    elif op == "heal":
        amt = int(attacker.max_hp * eff.get("pct_maxhp", 0) / 100.0)
        for u in targets:
            healed = min(amt, u.max_hp - u.hp)
            u.hp += healed
            if u is attacker:
                outcome["self"]["heal"] += healed
    elif op == "cleanse":
        names = set(eff.get("statuses", []))
        for u in targets:
            u.statuses = [s for s in u.statuses if s.name not in names]
    elif op == "shield":
        for u in targets:
            st = apply_status(u, "Shield", eff.get("duration"))
            if st:
                st.definition = dict(st.definition, shield_amount=eff.get("amount"))
    elif op == "stat_mod":
        # a bare stat buff/debuff with no named status -> synthesize one
        for u in targets:
            synth = {"stat_mods": [{"stat": eff["stat"], "value": eff["pct"],
                                    "unit": "pct"}],
                     "duration": eff.get("duration") or 1}
            u.statuses.append(Status(f"{eff['stat']}{eff['pct']:+d}%",
                                     synth["duration"], synth))
    elif op == "move_gauge":
        # pct signed: negative drains the target's charge gauge, positive fills it.
        for u in targets:
            outcome["gauge"].append({"unit": u, "pct": eff.get("pct", 0)})
    elif op == "skill_cd":
        # delta signed: positive delays the target's skills, negative refreshes them.
        for u in targets:
            outcome["cd"].append({"unit": u, "delta": eff.get("delta", 0)})
    elif op == "immunity":
        for u in targets:
            grant_immunity(u, eff.get("status"), eff.get("duration"))
    elif op == "extend_status":
        # No target field in the data; the named status is usually a self-buff ("extend
        # Fear Nothing"), occasionally on the struck enemy. Extend it wherever it lives
        # among {caster, primary target} -- absent elsewhere, this is a safe no-op.
        name = eff.get("status")
        add = eff.get("duration") or 0
        pool = [attacker] + resolve_targets("enemy_target", attacker, primary,
                                            allies, enemies)
        for u in pool:
            for s in u.statuses:
                if s.name == name and s.remaining != "battle":
                    s.remaining += add


def _run(effects, attacker, primary, allies, enemies, reduce, *, trusted=True):
    outcome = _new_outcome(trusted)
    per_target = {}
    for eff in effects:
        _apply_op(eff, attacker, primary, allies, enemies, reduce, per_target, outcome)
    outcome["hits"] = list(per_target.values())
    return outcome


def execute_skill(attacker, primary, allies, enemies, skill_id, *, damage_reduce=None):
    """Run a skill's IMMEDIATE effects (on_use/before_action/after_action/after_attack).
    -> outcome dict:

        {"hits": [ {"target": unit, "damage": int, "died": bool,
                    "statuses": [status_name, ...]}, ... ],
         "self": {"heal": int, "statuses": [...], "gauge": int},
         "gauge": [{"unit", "pct"}, ...],     # charge-gauge changes for the caller
         "cd":    [{"unit", "delta"}, ...],   # cooldown changes for the caller
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

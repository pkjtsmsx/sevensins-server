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

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "battle_data")

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


def apply_status(unit, name, duration_override=None):
    """Add or refresh a status on a unit, honouring stack limits and unstackable.
    Returns the Status, or None if the name is not in the catalog."""
    cat = catalog()
    definition = cat.get(name)
    if definition is None:
        return None                        # unknown status: no-op, never crash
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


def execute_skill(attacker, primary, allies, enemies, skill_id, *, damage_reduce=None):
    """Run a skill's IMMEDIATE effects. -> outcome dict:

        {"hits": [ {"target": unit, "damage": int, "died": bool,
                    "statuses": [status_name, ...]}, ... ],
         "self": {"heal": int, "statuses": [...], "gauge": int},
         "deferred": [effect, ...],          # battle_start/on_counter/conditional
         "trusted": bool}                    # whether the skill was `complete`

    Damage per target = attacker.atk * pct/100 * (1 - damage_reduce(target)) scaled by the
    target's damage-taken multiplier, min 1. Only mutates hp/statuses; the caller turns
    `hits` into DamageInfo and re-syncs.
    """
    reduce = damage_reduce or _default_reduce
    rec = skill_effects(skill_id)
    outcome = {"hits": [], "self": {"heal": 0, "statuses": [], "gauge": 0},
               "deferred": [], "trusted": bool(rec and rec.get("complete"))}
    if not rec:
        return outcome

    # Accumulate per-target so multiple hits/statuses on one unit fold into one entry.
    per_target = {}

    def hit_entry(u):
        e = per_target.get(u.order)
        if e is None:
            e = {"target": u, "damage": 0, "died": False, "statuses": []}
            per_target[u.order] = e
        return e

    for block in rec.get("blocks", []):
        for eff in block.get("effects", []):
            trig = eff.get("trigger", "on_use")
            if trig not in IMMEDIATE_TRIGGERS:
                outcome["deferred"].append(eff)
                continue
            op = eff.get("op")
            targets = resolve_targets(eff.get("target"), attacker, primary,
                                      allies, enemies)
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
                    # carry the concrete absorb amount on the instance
                    if st:
                        st.definition = dict(st.definition, shield_amount=eff.get("amount"))
            elif op == "stat_mod":
                # a bare stat buff/debuff with no named status -> synthesize one
                for u in targets:
                    synth = {"stat_mods": [{"stat": eff["stat"], "value": eff["pct"],
                                            "unit": "pct"}],
                             "duration": eff.get("duration") or 1}
                    st = Status(f"{eff['stat']}{eff['pct']:+d}%", synth["duration"], synth)
                    u.statuses.append(st)
            elif op in ("move_gauge", "skill_cd", "immunity", "extend_status"):
                # gauge/cd/immunity affect turn flow -> defer to the flow layer
                outcome["deferred"].append(eff)

    outcome["hits"] = list(per_target.values())
    return outcome

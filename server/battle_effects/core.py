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
    (a unit exposes .atk/.defence/.hp/.max_hp/.team/.spd/.statuses/.alive/.order), so it
    unit-tests against mocks and wires into battle.py without a circular import.
  * SAFE-BY-DEFAULT -- only skills flagged `complete` are trusted; anything else falls
    back to the caller's simple-damage path.
  * Never throws on unknown data -- an unrecognized status/target/op degrades to a no-op.
"""
import json
import os
import random

from .conditions import eval_cond
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


def any_incomplete_aoe():
    """A skill id whose parse is INCOMPLETE but whose design row says all-enemies.

    For tests: the population of incomplete parses shrinks as the matchers improve, so
    pinning one id means the fixture eventually stops testing what it claims to (a
    pinned pick did exactly that once a verb matcher completed it). Ask the data.
    """
    skill_effects(0)                       # force the table to load
    for sid in sorted(_skills or {}, key=int):
        if (not _skills[sid].get("complete")
                and target_range(sid) == (0, 2)
                and aoe_damage(sid)):
            return int(sid)
    raise AssertionError("no incomplete all-enemies skill left -- update the fixture")


# ---- design-declared targeting ---------------------------------------------
# **The design row says how many units a skill hits, and it is authoritative.**
# `DesignSkillRow.GetTargetGroup()` is `_target / 100` and `GetTargetRange()` is
# `_target % 100` (0x1AACCA4 / 0x1AACCC4), and the panel's "Range" label is text
# `23000 + _target` -- which is how these ranges were read off:
#
#     group 0 = enemies, 1 = allies
#     1 -> 1 target      2 -> ALL        6 -> 2      7 -> 3      8 -> 4
#     9..12 -> 1..4 RANDOM               13/14 -> highest/lowest HP
#     15/16 -> highest/lowest DEF        17/18 -> highest/lowest ATK   19 -> highest SPD
#
# This beats reading the prose. Descriptions say "inflicts Confuse on the target" even
# for a skill the panel labels "Range 2 enemies" (Phantom Star Ring III, `_target` 6),
# so a text parser cannot see the spread at all. By this field 5681 enemy-group skills
# are multi-target while only 309 were spreading -- the rest, `complete` ones included,
# were landing on a single enemy.
#
# The CLIENT already has all of this: the design pack ships with it, which is how the
# panel draws the label without asking us. It does not enforce it, though -- it just
# renders whatever DamageInfo rows the server sends.
TARGET_GROUP_ENEMY = 0
RANGE_ALL = 2
RANGE_FIXED = {1: 1, 6: 2, 7: 3, 8: 4}
RANGE_RANDOM = {9: 1, 10: 2, 11: 3, 12: 4}
# range -> (unit attribute, take the biggest?)
RANGE_PICK = {13: ("hp", True), 14: ("hp", False),
              15: ("defence", True), 16: ("defence", False),
              17: ("atk", True), 18: ("atk", False), 19: ("spd", True)}


def target_range(skill_id):
    """-> (group, range) straight off the design row, mirroring the two accessors."""
    from design_data import row as _row          # local: keeps this module mock-testable
    t = int(((_row("skill", int(skill_id)) or {}).get("_target")) or 0)
    return t // 100, t % 100


def design_enemy_targets(skill_id, primary, enemies):
    """The enemies the design row says this skill strikes, or None when it does not
    describe an enemy spread worth widening to.

    None (rather than [primary]) for the single-target and unmodelled cases, so callers
    can tell "the design says one" from "the design says three" and leave existing
    behaviour untouched in the former.
    """
    group, rng = target_range(skill_id)
    if group != TARGET_GROUP_ENEMY:
        return None                       # ally-group skills are not ours to widen
    live = [u for u in enemies if u.alive]
    if not live:
        return None
    if rng == RANGE_ALL:
        return live
    n = RANGE_FIXED.get(rng)
    if n is not None:
        if n <= 1:
            return None                   # "1 enemy" -- the chosen target already
        # The player's chosen target leads, then the rest in field order. Deterministic
        # on purpose: a fixed-count range is not a random one (9..12 are).
        rest = [u for u in live if u is not primary]
        if primary in live:
            return [primary] + rest[:n - 1]
        return rest[:n]
    n = RANGE_RANDOM.get(rng)
    if n is not None:
        pool = list(live)
        _rng.shuffle(pool)
        return pool[:n]
    pick = RANGE_PICK.get(rng)
    if pick:
        stat, biggest = pick
        chosen = (max if biggest else min)(live, key=lambda u: getattr(u, stat, 0))
        return [chosen]
    return None


def hit_count(skill_id):
    """How many separate SWINGS the skill plays -- DesignSkillRow._count, whose design
    column is literally "hit".

    This is a wire-shape requirement, not flavour. `AttackJsonData.data` is a
    List<List<DamageInfo>> with ONE INNER LIST PER SWING: the skill's cinematic fires a
    BscTagKind-5 tag per hit and `AttackBehavior.BscTag` (0x1BE3924) hands
    `DmgInfo[0]` to OnDamageAndNumber and then `RemoveAt(0)`s it. Ship a 3-hit skill as
    a single group and the client draws one damage number and silently drops the other
    two swings.
    """
    from design_data import row as _row          # local: keeps this module mock-testable
    n = int(((_row("skill", int(skill_id)) or {}).get("_count")) or 0)
    return max(1, n)


def aoe_damage(skill_id):
    """True if this skill's FIRST damage op strikes every enemy.

    Deliberately readable from an INCOMPLETE record. Only `complete` skills go through
    the effect engine; everything else falls to Battle's simple single-hit path, which
    could not spread damage however plainly the description said "to all enemies". That
    is 226 skills whose parse already found the AoE and got it thrown away, against 114
    that work -- so the fallback consulting just this one field roughly triples AoE
    coverage without trusting the rest of a parse we know is partial.

    The FIRST damage op is the one to read: a skill often opens with an AoE hit and then
    adds a single-target follow-up ("...then deals 200% ATK to the enemy with the
    highest HP"), and the fallback models only that opening hit. Taking any-op-is-AoE
    would splash the follow-ups across the whole enemy team.
    """
    rec = skill_effects(skill_id) or {}
    for block in rec.get("blocks") or []:
        for eff in block.get("effects") or []:
            if eff.get("op") == "damage":
                return eff.get("target") == "all_enemies"
    return False


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
    """One active status on a unit: its catalog name, remaining turns and stack count.

    `shield_hp`/`dot_atk`/`taunt_source` are PER-INSTANCE runtime state, not catalog
    data (two units' Shields absorb independently even though they share `definition`):
      shield_hp    -- remaining absorb capacity (Phase 3 shield mechanic).
      dot_atk      -- the inflicter's ATK, snapshotted at apply time, for this status's
                      damage-over-time tick (so a DoT still hits for the caster's power
                      even after the caster's own buffs expire).
      taunt_source -- the order of the unit this status forces its holder to attack
                      (Taunt/Taunt UL -- the `forced_target` flag)."""

    __slots__ = ("name", "remaining", "stacks", "definition",
                 "shield_hp", "dot_atk", "taunt_source")

    def __init__(self, name, remaining, definition):
        self.name = name
        self.remaining = remaining          # int turns, or "battle" for permanent
        self.stacks = 1
        self.definition = definition or {}
        self.shield_hp = 0
        self.dot_atk = None
        self.taunt_source = None

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


def has_flag(unit, flag):
    """True if any of the unit's active statuses carries this catalog flag (Phase 3
    enforcement points: heal_block, ability_seal, forced_target, confused_targeting,
    cd_reduction_block, ...). Statuses in `.statuses` are always live -- expired ones are
    dropped by Status.tick()'s caller -- so no remaining-turns check is needed here."""
    return any(flag in st.definition.get("flags", []) for st in unit.statuses)


def effective_atk(unit):
    """The unit's ATK after its own stat_mod statuses (Keen+, Fracture-, flat bonuses).
    Shared by damage, DoT-tick snapshotting, and shield sizing so they agree on what
    'the caster's ATK' means at the moment of the effect."""
    return unit.atk * stat_multiplier(unit.statuses, "ATK") + flat_bonus(unit.statuses, "ATK")


def absorb_shield(unit, dmg):
    """Drain the unit's Shield statuses (oldest first) against an incoming hit, ->
    the damage that gets through. Multiple stacked shields drain in application order;
    a shield with 0 capacity left (or no shield_hp at all) is simply skipped."""
    for st in unit.statuses:
        cap = st.shield_hp
        if cap <= 0:
            continue
        used = min(cap, dmg)
        st.shield_hp = cap - used
        dmg -= used
        if dmg <= 0:
            break
    return dmg


def tick_dot_hot(unit):
    """Apply this unit's damage/heal-over-time ticks for the START of its own turn
    ('when a turn starts, deals/restores...' -- every DoT/HoT catalog entry reads this
    way). -> (dot_total, hot_total). DoT ticks bypass the target's DEF and any Shield
    (a flat ATK-scaled tick, per the catalog text) but still respect damage_taken mods
    (Stun's +25%, etc); HoT ticks respect heal_block. Stops once the unit dies mid-tick
    so a later HoT never revives it."""
    dot = hot = 0
    blocked = has_flag(unit, "heal_block")
    for st in list(unit.statuses):
        if not unit.alive:
            break
        d = st.definition
        if d.get("tick_pct_atk"):
            src_atk = st.dot_atk if st.dot_atk is not None else unit.atk
            amt = max(1, int(src_atk * d["tick_pct_atk"] * st.stacks / 100.0))
            amt = int(amt * damage_taken_multiplier(unit.statuses))
            unit.hp = max(0, unit.hp - amt)
            dot += amt
        if d.get("heal_pct_maxhp") and not blocked:
            heal = int(unit.max_hp * d["heal_pct_maxhp"] / 100.0)
            healed = min(heal, unit.max_hp - unit.hp)
            unit.hp += healed
            hot += healed
    return dot, hot


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
               "spd": lambda u: u.spd, "def": lambda u: u.defence}.get(stat)
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


def apply_status(unit, name, duration_override=None, *, source=None, flat_shield=None):
    """Add or refresh a status on a unit, honouring stack limits and unstackable.
    Returns the Status, or None if the name is not in the catalog OR an active immunity
    on the unit blocks it.

    `source` is the inflicting unit (usually the caster) -- snapshotted as the DoT tick's
    ATK and used to size a %ATK shield; self-buffs naturally pass the unit itself.
    `flat_shield` overrides that sizing with an explicit amount (the `shield` op's
    "open a 7000 Shield"). Re-applying a shield status always resets its pool to the
    fresh amount, matching "open a shield" reading as a new shield, not a top-up."""
    cat = catalog()
    definition = cat.get(name)
    if definition is None:
        return None                        # unknown status: no-op, never crash
    if _immune_to(unit, name):
        return None                        # blocked by an active immunity
    dur = _duration(duration_override, definition)
    flags = definition.get("flags", [])
    shield_hp = None
    if flat_shield is not None:
        shield_hp = flat_shield
    elif definition.get("shield_pct_atk"):
        shield_hp = int(effective_atk(source) * definition["shield_pct_atk"] / 100.0) \
            if source is not None else 0
    for st in unit.statuses:
        if st.name == name:
            if "unstackable" in flags:
                st.remaining = dur          # just refresh the timer
            else:
                st.stacks = min(st.stacks + 1,
                                definition.get("max_stacks", st.stacks + 1))
                st.remaining = dur
            if shield_hp is not None:
                st.shield_hp = shield_hp
            if definition.get("tick_pct_atk") and source is not None:
                st.dot_atk = effective_atk(source)
            return st
    st = Status(name, dur, definition)
    if shield_hp is not None:
        st.shield_hp = shield_hp
    if definition.get("tick_pct_atk"):
        st.dot_atk = effective_atk(source) if source is not None else unit.atk
    if "forced_target" in flags and source is not None:
        st.taunt_source = source.order
    unit.statuses.append(st)
    return st


# ---- execution: context, dispatch, and the two entry points -----------------
def _default_reduce(_target):
    return 0.0


def _new_outcome(trusted=True):
    # `gauge`/`cd` collect turn-flow changes the caller applies to its own Unit fields
    # (scv, cooldowns), which the engine's unit protocol deliberately does not expose.
    # `status_events` = each named status applied this action, for DamageInfo.status icons.
    # `strikes` is every INDIVIDUAL blow in swing order -- {"target", "damage", "seq"}
    # -- kept alongside the per-target fold in `hits` because the client needs one
    # DamageInfo group per swing to animate a multi-hit skill (see hit_count).
    return {"hits": [], "strikes": [],
            "self": {"heal": 0, "statuses": [], "gauge": 0},
            "gauge": [], "cd": [], "status_events": [], "deferred": [], "trusted": trusted}


class Ctx:
    """Everything an op handler needs for one skill execution. Handlers take (eff, ctx),
    resolve their own targets via ctx.targets(), and fold results into ctx.outcome."""

    __slots__ = ("attacker", "primary", "allies", "enemies", "reduce",
                 "per_target", "outcome", "env", "design")

    def __init__(self, attacker, primary, allies, enemies, reduce, per_target, outcome,
                 env=None, design=None):
        self.attacker = attacker
        self.primary = primary
        self.allies = allies
        self.enemies = enemies
        self.reduce = reduce
        self.per_target = per_target
        self.outcome = outcome
        self.env = env or {}
        # Units the skill's own design row says it strikes; None when it says one, or
        # when the row is not an enemy spread we model.
        self.design = design

    def targets(self, token):
        # **The design row outranks the singular default.** `enemy_target` is what the
        # text parser emits both for a genuine single-target hit and for a description
        # that merely SAYS "the target" while the panel reads "Range 3 enemies". An
        # explicit token (all_enemies, self, highest_hp_enemy) is more specific than
        # the row and is left alone.
        if token in (None, "", "enemy_target") and self.design:
            return list(self.design)
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


def _run(effects, attacker, primary, allies, enemies, reduce, *, trusted=True, env=None,
         design=None):
    """Unconditional effects first, then the {"when": ...}-gated ones -- a gate like
    'if this attack defeats an enemy' must see the outcome of the plain hits."""
    outcome = _new_outcome(trusted)
    ctx = Ctx(attacker, primary, allies, enemies, reduce, {}, outcome, env, design)
    for eff in effects:
        if not eff.get("when"):
            _apply_op(eff, ctx)
    for eff in effects:
        if eff.get("when") and eval_cond(eff["when"], ctx):
            _apply_op(eff, ctx)
    outcome["hits"] = list(ctx.per_target.values())
    return outcome


# Triggers that fire as part of USING the skill (in listed order). battle_start /
# on_counter / conditional are deferred to the turn-flow layer.
IMMEDIATE_TRIGGERS = ("on_use", "before_action", "after_action", "after_attack")


def execute_skill(attacker, primary, allies, enemies, skill_id, *, damage_reduce=None,
                  env=None):
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
    An effect carrying {"when": cond} is gated: it runs AFTER the plain effects, and only
    if the cond evaluates True against the live state + `env` ({"turn": n, "crit": bool}
    from the turn-flow layer). Legacy `conditional`-trigger effects (no machine-readable
    gate) stay in `deferred`, unfired -- applying them blindly would be a guess.
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
                   trusted=bool(rec.get("complete")), env=env,
                   design=design_enemy_targets(skill_id, primary, enemies))
    outcome["deferred"] = deferred
    return outcome


def run_phase(skill_id, phase, attacker, primary, allies, enemies, *, damage_reduce=None,
              env=None):
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
                trusted=bool(rec.get("complete")), env=env)

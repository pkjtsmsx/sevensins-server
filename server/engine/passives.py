"""Hand-written passive skills, as a declarative rule table.

**Why these cannot be derived.** A passive's opcodes say only WHICH statuses exist; the
logic is in prose and nowhere else. Gabriel's Campus Correction is four numbered clauses
-- "at the start of the turn, if you have a Commendation, you gain CC Immunity", "when HP
>= 70%, SPD +10% and ATK +35% before action", "when HP <= 30%, after taking damage,
forcibly gain a Major Merit and inflict a Major Demerit on the attacker" -- with trigger
timings, HP thresholds and promotion chains that `_action` does not encode at all.

**So this is a table, not a pile of special cases.** Every passive in the game has the
same shape: *at TRIGGER, if CONDITION, apply STATUS to SELECTION*. Writing that shape
once means the next cast is a few lines of data rather than new code, which matters --
Gabriel is not unusual, she is just the one we read first.

What a rule cannot express yet is stated in `UNMODELLED` per cast, so the gap is visible
rather than silently missing.

Numbers come from the compiled spec where the effect carries them, and from the prose
otherwise; the status IDS are read out of the passive's own effect list by name, so a
rule never invents a status the skill does not actually reference.
"""
import dataclasses
from typing import Callable, Optional

from . import specs, status as _status

# --- trigger points ----------------------------------------------------------------
BATTLE_START = "battle_start"
TURN_START = "turn_start"          # the holder's own turn, before it acts
AFTER_ACTION = "after_action"      # the holder has just acted
ON_DAMAGE_DEALT = "on_damage_dealt"
ON_DAMAGE_TAKEN = "on_damage_taken"

ALL_TRIGGERS = (BATTLE_START, TURN_START, AFTER_ACTION,
                ON_DAMAGE_DEALT, ON_DAMAGE_TAKEN)


# --- selections --------------------------------------------------------------------
#
# A selector takes (holder, units, ctx) and returns the units to act on. Named rather
# than inlined so the table reads like the prose it came from.

def SELF(holder, units, ctx=None):
    return [holder]


def ALLIES(holder, units, ctx=None):
    return [u for u in units if u.team == holder.team and u.alive]


def ENEMIES(holder, units, ctx=None):
    return [u for u in units if u.team != holder.team and u.alive]


def ATTACKER(holder, units, ctx=None):
    """Whoever just hit the holder -- only meaningful on ON_DAMAGE_TAKEN."""
    src = (ctx or {}).get("attacker")
    return [src] if src is not None and src.alive else []


def _top(pool, key, n):
    return sorted(pool, key=key, reverse=True)[:n]


def ALLIES_TOP_ATK(n):
    return lambda h, u, c=None: _top(ALLIES(h, u), lambda x: x.atk, n)


def ENEMIES_TOP_ATK(n):
    return lambda h, u, c=None: _top(ENEMIES(h, u), lambda x: x.atk, n)


def ENEMIES_TOP_SPD(n):
    return lambda h, u, c=None: _top(ENEMIES(h, u), lambda x: x.spd, n)


def ENEMIES_TOP_HP(n):
    return lambda h, u, c=None: _top(ENEMIES(h, u), lambda x: x.hp, n)


# --- conditions --------------------------------------------------------------------

def ALWAYS(holder, units, ctx=None):
    return True


def hp_at_least(frac):
    return lambda h, u, c=None: h.max_hp and h.hp / h.max_hp >= frac


def hp_at_most(frac):
    return lambda h, u, c=None: h.max_hp and h.hp / h.max_hp <= frac


def holds(name):
    """The holder has a status whose name contains `name` (loose, as elsewhere)."""
    def _f(h, u, c=None):
        want = name.lower()
        return any(want in str(getattr(s, "name", "") or "").lower()
                   for s in h.statuses)
    return _f


# What a rule DOES. Most clauses apply a status, but not all -- Raphael cuts a gauge,
# Jacqueline heals herself on a hit -- and those have no status row to point at.
APPLY_STATUS = "status"
HEAL = "heal"
DAMAGE = "damage"
GAUGE = "gauge"

# What a magnitude is a percentage OF.
OF_SELF_ATK = "self_atk"
OF_SELF_MAX_HP = "self_max_hp"
OF_OTHER_ATK = "other_atk"          # the other party to the event (attacker or victim)


@dataclasses.dataclass
class Rule:
    """One clause of one passive.

    `status` is the NAME as the passive's own effect list spells it; the id is resolved
    from that list, so a rule cannot reference a status the skill does not carry.
    `duration`/`magnitude` override the compiled numbers when the prose states something
    the glossary line does not.
    """
    trigger: str
    status: Optional[str] = None
    to: Callable = SELF
    when: Callable = ALWAYS
    duration: Optional[int] = None
    magnitude: Optional[float] = None
    once: bool = False                 # fire at most once per battle
    permanent: bool = False            # explicitly "for the entire battle"
    effect: str = APPLY_STATUS
    basis: str = OF_SELF_ATK           # what `magnitude` is a percentage of
    chance: Optional[float] = None      # None = certain
    note: str = ""


# --- the table ---------------------------------------------------------------------
#
# Keyed by the passive's skill GROUP so every level shares one entry. Where a number
# changes per level the compiled effect carries it and the rule leaves it None.

PASSIVES = {
    # GABRIEL -- Campus Correction
    2096131: [
        Rule(BATTLE_START, "Admonition (Reduce CRT)", ENEMIES,
             note="4. Clear Rewards and Punishments: grant all enemies an Admonition"),
        Rule(BATTLE_START, "Admonition (Reduce SPD)", ENEMIES),
        Rule(BATTLE_START, "Commendation (Crit Damage)", ALLIES,
             note="...and grant all allies a Commendation"),
        Rule(BATTLE_START, "Commendation (SPD)", ALLIES),
        Rule(TURN_START, "Incarnation of Order I", SELF, when=holds("Commendation"),
             note="1. Maintain Order: at the start of the turn, if you have a "
                  "Commendation, you gain CC Immunity"),
    ],

    # METATRON -- Field Hospital
    2094131: [
        Rule(BATTLE_START, "Field Shield", ALLIES, duration=3,
             note="1. grant all allies 2,500 points of Field Shield, lasting 3 turns"),
        Rule(BATTLE_START, "Field Angel (Health)", ALLIES, permanent=True,
             note="2. Field Angel: +15% Max HP, +10% SPD, entire battle"),
        Rule(BATTLE_START, "Field Angel (Speed)", ALLIES, permanent=True),
        Rule(BATTLE_START, "CC Immunity (Field Nurse)", SELF, permanent=True,
             note="3. Self-Sacrificial Rescue: Metatron is constantly immune to "
                  "immobilization -- so this one is permanent, not the 2 turns the "
                  "glossary line states for the ordinary case"),
        Rule(BATTLE_START, "Serum Injection assessment I", ALLIES_TOP_ATK(1), once=True,
             note="4. Initial Syringe: the first acting ally casts Serum Injection on "
                  "1 ally with the highest ATK, limited to once"),
    ],

    # JACQUELINE -- Bike Lady
    1100131: [
        Rule(TURN_START, "Body Strike II", SELF, when=hp_at_least(0.90),
             note="Healthy Strike II: before the action, if HP>90%, ATK+25%"),
        Rule(TURN_START, "Healthy Guard II", SELF, when=hp_at_least(0.90),
             note="Healthy Guard II: before the action, if HP>90%, DEF+30%"),
        Rule(ON_DAMAGE_DEALT, effect=HEAL, to=SELF, magnitude=9.0,
             basis=OF_SELF_MAX_HP, chance=0.25, once=True,
             note="Life Steal II: 25% chance to recover 9% HP when dealing damage. "
                  "This effect can only be triggered 1 time."),
    ],

    # LUCIFER -- Abyssal Prime
    2080131: [
        Rule(BATTLE_START, "The Divine", SELF, permanent=True,
             note="Angel Blood: grant the caster The Divine for the entire battle"),
        Rule(TURN_START, "CC Immunity", SELF, when=holds("Divine"), duration=1,
             note="Throughout Heaven and Earth: if affected by The Divine, grants CC "
                  "Immunity for one turn at start of a turn"),
    ],

    # MICHAEL -- Solar Prime
    2090131: [
        Rule(BATTLE_START, "Stun/Confuse Immunity", SELF, permanent=True,
             note="Heavenly Focus: permanently immune to Confuse and Stun"),
        Rule(BATTLE_START, "Return", ENEMIES, permanent=True,
             note="Sword Draw: inflicts Return on all enemies. Dispelled only when "
                  "Solar Prime Michael dies, so no duration"),
        Rule(BATTLE_START, "Restraint", ENEMIES_TOP_SPD(2), duration=3,
             note="Shadowless Judgment: the two enemies with the highest SPD, SPD-600 "
                  "for three turns"),
        Rule(BATTLE_START, "Swift Blade", SELF, permanent=True,
             note="Swift Blade: while dealing damage, if SPD exceeds the target's by "
                  "750, the target's Move Gauge is reduced by 15%"),
    ],

    # RAPHAEL (raid boss) -- Zero Cal (SP)
    100001431: [
        Rule(BATTLE_START, "Power Attack Seal", ENEMIES,
             note="Discipline: inflicts Power Attack Seal and Special Move Seal on all "
                  "enemies at battle start"),
        Rule(BATTLE_START, "Special Move Seal", ENEMIES),
        Rule(BATTLE_START, "Elite", SELF, permanent=True,
             note="the boss's own Elite marker"),
        Rule(BATTLE_START, "CC Immunity (SP)", SELF, permanent=True,
             note="AND its CC immunity -- which is why a raid boss should never have "
                  "been stun-lockable in the first place"),
        Rule(AFTER_ACTION, effect=GAUGE, to=ENEMIES_TOP_HP(1), magnitude=-30.0,
             note="After the action, reduces the Move Gauge of the enemy with the "
                  "highest HP by 30%."),
    ],
}

# Clauses the rule shape cannot express yet. Listed per passive so the gap is visible
# instead of quietly absent.
UNMODELLED = {
    2096131: ["2. HP>=70% -> SPD+10%/ATK+35% before action (no status row to apply)",
              "3. HP<=30% -> Major Merit to self and Major Demerit to the attacker",
              "the Admonition -> Minor Demerit -> Major Demerit promotion chain",
              "the second-tier Holy Arbiter / Shadow Ruler classes (Fetish unlocks)"],
    2094131: ["the Serum Injection death-triggered revive"],
    1100131: [],
    2080131: ["The Fallen branches (Keen/Teardown, and stripping the target's buffs)"],
    2090131: ["Swift Blade's gauge cut (needs the attacker's SPD vs the target's)",
              "Return's reflect lives in status.REFLECT -- it is a status the ENEMY "
              "holds, so it cannot be a rule in MICHAEL's table: fire_all runs a "
              "passive for its own holder"],
    100001431: [],
}


def _status_ids(spec):
    """{name: (id, compiled numbers)} from the passive's own effect list."""
    out = {}
    for e in spec.get("effects") or []:
        if e["op"] != "apply_status":
            continue
        st = e.get("status") or {}
        if st.get("name"):
            out[st["name"]] = (st["id"], e.get("numbers") or {})
    return out


def rules_for(skill_id):
    """-> the rules for a passive, by its group (all levels share one entry)."""
    spec = specs.skill(skill_id) or {}
    return PASSIVES.get(spec.get("group") or skill_id, [])


def _amount(rule, holder, target, ctx):
    """-> the size of a non-status effect, from its magnitude and basis.

    A negative magnitude is a reduction; `GAUGE` uses it directly as gauge points, since
    the bar is already a 0..100 scale and "reduce the Move Gauge by 30%" means 30 points.
    """
    mag = float(rule.magnitude or 0)
    if rule.effect == GAUGE:
        return mag
    if rule.basis == OF_SELF_MAX_HP:
        base = holder.max_hp
    elif rule.basis == OF_OTHER_ATK:
        other = (ctx or {}).get("attacker") or (ctx or {}).get("victim") or target
        base = getattr(other, "atk", 0)
    else:
        base = holder.atk
    return int(abs(base) * mag / 100.0)


def fire(trigger, holder, passive_skill_id, units, ctx=None, fired=None):
    """Run one passive's rules for one trigger. -> [(unit, Active)] actually applied.

    `fired` is a per-battle set the caller keeps, so `once` rules stay once.
    """
    spec = specs.skill(passive_skill_id)
    if not spec:
        return []
    ids = _status_ids(spec)
    applied = []
    for i, rule in enumerate(rules_for(passive_skill_id)):
        if rule.trigger != trigger:
            continue
        if rule.effect == APPLY_STATUS and not rule.status:
            continue          # only a status rule needs a status to name
        key = (passive_skill_id, i)
        if rule.once and fired is not None and key in fired:
            continue
        if not rule.when(holder, units, ctx):
            continue
        if rule.chance is not None and (ctx or {}).get("rng") is not None:
            if (ctx or {}).get("rng").random() >= rule.chance:
                continue

        if rule.effect != APPLY_STATUS:
            for target in rule.to(holder, units, ctx):
                amount = _amount(rule, holder, target, ctx)
                if rule.effect == HEAL:
                    target.hp = min(target.max_hp, target.hp + amount)
                elif rule.effect == DAMAGE:
                    target.hp = max(0, target.hp - amount)
                elif rule.effect == GAUGE:
                    target.scv = max(0.0, min(
                        100.0, float(getattr(target, "scv", 0.0)) + amount))
                applied.append((target, rule.effect, amount))
            if rule.once and fired is not None:
                fired.add(key)
            continue

        found = ids.get(rule.status)
        if not found:
            continue                       # the skill does not carry that status
        sid, numbers = found
        row = specs.status(sid) or {}
        duration = rule.duration if rule.duration is not None else numbers.get("duration")
        if rule.permanent:
            duration = None
        elif duration is None and not numbers.get("permanent"):
            # Same rule as status.apply_event: unstated is NOT permanent.
            duration = _status.DEFAULT_DURATION
        for target in rule.to(holder, units, ctx):
            active = _status.Active(
                status_id=sid, name=row.get("name") or rule.status,
                kind=row.get("kind"), category=row.get("category"),
                stat=row.get("stat"), remaining=duration,
                magnitude=(rule.magnitude if rule.magnitude is not None
                           else numbers.get("magnitude")),
                stack_cap=row.get("stack_cap"),
                unremovable=bool(row.get("unremovable")),
                source_atk=int(getattr(holder, "atk", 0) or 0))
            existing = next((s for s in target.statuses
                             if isinstance(s, _status.Active)
                             and s.status_id == sid), None)
            if existing is not None:
                if active.stack_cap:
                    existing.stacks = min(int(active.stack_cap), existing.stacks + 1)
                continue
            target.statuses.append(active)
            applied.append((target, active))
        if rule.once and fired is not None:
            fired.add(key)
    return applied


def fire_all(trigger, holders, units=None, ctx=None, fired=None):
    """Run the passives of `holders` for this trigger. -> [(unit, ...)].

    `holders` is who OWNS the passive; `units` is the field its selectors see. They are
    separate on purpose: an after-action rule like Raphael's "reduce the Move Gauge of
    the enemy with the highest HP" is held by one unit but selects across the whole
    field, and passing only the holder would make ENEMIES resolve to nothing.

    A unit's passive is its fourth skill slot -- com_attack / skill / sp_skill / PASSIVE
    is the layout every cast uses.
    """
    field = list(units if units is not None else holders)
    applied = []
    for holder in list(holders):
        if not holder.alive:
            continue
        for sid in (getattr(holder, "skills", None) or []):
            if not sid:
                continue
            spec = specs.skill(sid)
            if spec and spec.get("type") == "passive":
                applied += fire(trigger, holder, sid, field, ctx, fired)
    return applied

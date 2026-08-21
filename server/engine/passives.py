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
ON_DEATH = "on_death"              # a unit died; ctx carries "victim"

ALL_TRIGGERS = (BATTLE_START, TURN_START, AFTER_ACTION,
                ON_DAMAGE_DEALT, ON_DAMAGE_TAKEN, ON_DEATH)


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


def DEAD_ALLIES(holder, units, ctx=None):
    """Fallen allies of the holder -- the pool a revive draws from."""
    return [u for u in units if u.team == holder.team and not u.alive]


def RANDOM_DEAD_ALLY(holder, units, ctx=None):
    pool = DEAD_ALLIES(holder, units, ctx)
    rng = (ctx or {}).get("rng")
    if not pool:
        return []
    return [rng.choice(pool) if rng is not None else pool[0]]


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


def holds(name, on="holder"):
    """`on` has a status whose name contains `name` (loose, as elsewhere).

    `on` selects whose statuses to read: the passive's holder, or the other party to
    the event (the attacker on ON_DAMAGE_TAKEN, the victim on ON_DAMAGE_DEALT).
    """
    def _f(h, u, c=None):
        who = h if on == "holder" else _other(h, c)
        want = name.lower()
        return who is not None and any(
            want in str(getattr(s, "name", "") or "").lower() for s in who.statuses)
    return _f


def stacks_at_least(name, n, on="other"):
    """`on` holds at least `n` stacks of a status.

    The shape behind every promotion chain in the game -- Gabriel's "if the target has
    three stacks of Admonition, additionally give a Minor Demerit". Counts a status's
    `stacks` field, and also counts separate rows sharing the name, since a pack often
    splits one displayed status across several rows (Admonition is two: CRT and SPD).
    """
    def _f(h, u, c=None):
        who = h if on == "holder" else _other(h, c)
        if who is None:
            return False
        want = name.lower()
        total = 0
        for st in who.statuses:
            if want in str(getattr(st, "name", "") or "").lower():
                total += max(1, int(getattr(st, "stacks", 1) or 1))
        return total >= n
    return _f


def stat_exceeds(stat, margin, on="other"):
    """The holder's `stat` exceeds `on`'s by at least `margin`.

    Michael's Swift Blade: "if the caster's SPD is more than 750 higher than the
    target's".
    """
    def _f(h, u, c=None):
        who = h if on == "holder" else _other(h, c)
        if who is None:
            return False
        return getattr(h, stat, 0) - getattr(who, stat, 0) > margin
    return _f


def _other(holder, ctx):
    """The other party to the current event, whichever side the holder is on."""
    ctx = ctx or {}
    for key in ("attacker", "victim", "target"):
        who = ctx.get(key)
        if who is not None and who is not holder:
            return who
    return None


def all_of(*conds):
    return lambda h, u, c=None: all(f(h, u, c) for f in conds)


# What a rule DOES. Most clauses apply a status, but not all -- Raphael cuts a gauge,
# Jacqueline heals herself on a hit -- and those have no status row to point at.
APPLY_STATUS = "status"
HEAL = "heal"
DAMAGE = "damage"
GAUGE = "gauge"
REVIVE = "revive"

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
    # Synthesise a status the pack has no row for. Gabriel's "when HP >= 70%, SPD +10%
    # and ATK +35% before action" is a real, numbered effect with nothing to point at,
    # and refusing to model it would lose the clause entirely.
    kind: Optional[str] = None
    stat: Optional[str] = None
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
        Rule(TURN_START, "Hold On Classmates (SPD)", SELF, when=hp_at_least(0.70),
             kind="stat_mod", stat="SPD", magnitude=10.0, duration=1,
             note="2. Hold On, Classmates: when HP >= 70%, SPD +10% before action. "
                  "SYNTHESISED -- the pack has no status row for this clause"),
        Rule(TURN_START, "Hold On Classmates (ATK)", SELF, when=hp_at_least(0.70),
             kind="stat_mod", stat="ATK", magnitude=35.0, duration=1,
             note="...and ATK +35%"),
        Rule(ON_DAMAGE_TAKEN, "Major Merit", SELF, when=hp_at_most(0.30),
             note="3. Disciplinary Privilege: when HP <= 30%, after taking damage, "
                  "forcibly gain a Major Merit... (the row lives on Mandatory Penalty "
                  "and Reward, reached through the cast tier)"),
        Rule(ON_DAMAGE_TAKEN, "Major Demerit", ATTACKER, when=hp_at_most(0.30),
             note="...and inflict a Major Demerit on the attacker"),
        Rule(ON_DAMAGE_DEALT, "Minor Demerit", ENEMIES,
             when=stacks_at_least("Admonition", 3),
             note="the promotion chain: three stacks of Admonition on a struck target "
                  "promote to a Minor Demerit"),
        Rule(ON_DAMAGE_DEALT, "Major Demerit", ENEMIES,
             when=holds("Minor Demerit", on="other"),
             note="...and a Minor Demerit promotes to a Major Demerit"),
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
        Rule(ON_DEATH, effect=REVIVE, to=RANDOM_DEAD_ALLY, magnitude=50.0,
             when=stacks_at_least("Serum Injection", 2, on="other"),
             note="...if an ally with 2 stacks of Serum Injection dies from direct "
                  "damage, a random dead ally is revived at 50% of their maximum HP"),
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
        Rule(TURN_START, "Keen", SELF, when=holds("Fallen"), duration=2,
             kind="stat_mod", stat="CRI", magnitude=35.0,
             note="I alone am honored: if affected by The Fallen, grants Keen "
                  "(CRT+35%) for two turns at each start of a turn. SYNTHESISED"),
        Rule(TURN_START, "Teardown", SELF, when=holds("Fallen"), duration=2,
             kind="damage_mod", magnitude=75.0,
             note="...and Teardown (Crit. DMG +75%)"),
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
        Rule(ON_DAMAGE_DEALT, effect=GAUGE, to=ENEMIES, magnitude=-15.0,
             when=all_of(holds("Swift Blade"), stat_exceeds("spd", 750)),
             note="Swift Blade: while dealing damage, if the caster's SPD is more than "
                  "750 higher than the target's, the target's Move Gauge is cut 15%"),
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
    # NOT a gap: the Fetish system NEVER SHIPPED. Its section in the Relics panel reads
    # "Coming Soon" (text 111223 / 敬請熱切關注!!, or 36 "This feature will be available
    # soon"), and the client has no subsystem, panel class or prefab bundle for it -- 25
    # Player* subsystems and none is Fetish. Only two skills in the whole pack mention
    # it, Gabriel's and Metatron's passives, both saying second-tier classes "can be
    # unlocked at the Fetish and Consonance interface".
    #
    # So these tiers were unreachable in the live game too, and modelling them would ADD
    # behaviour the real server never had. Left unimplemented deliberately.
    2096131: ["Holy Arbiter / Shadow Ruler -- Fetish unlocks, and Fetish never shipped "
              "(Relics panel says Coming Soon); unreachable in the live game as well"],
    2094131: [],
    1100131: [],
    2080131: ["Stay Low: if affected by The Fallen, strip the target's unstackable "
              "buffs BEFORE dealing damage -- needs a pre-damage hook"],
    2090131: ["Return's reflect lives in status.REFLECT -- it is a status the ENEMY "
              "holds, so it cannot be a rule in MICHAEL's table: fire_all runs a "
              "passive for its own holder"],
    100001431: [],
}


_CAST_STATUS_CACHE = {}


def _from_effects(spec, out):
    for e in (spec or {}).get("effects") or []:
        if e["op"] != "apply_status":
            continue
        st = e.get("status") or {}
        if st.get("name"):
            out.setdefault(st["name"], (st["id"], e.get("numbers") or {}))
    return out


def _status_ids(spec, holder=None):
    """{name: (id, numbers)} a rule may reference, widest useful scope.

    Three tiers, and the widening is deliberate rather than convenient:

      1. the passive's OWN effect list -- always correct;
      2. any OTHER skill of the same CAST. Gabriel's passive inflicts a Major Demerit
         but only her basic attack carries that status row, and a cast's kit is written
         as one set. Without this the clause simply cannot be expressed.
      3. nothing wider. A rule may still SYNTHESISE a status by giving `kind`/`stat`
         explicitly, which is honest about inventing one; silently borrowing a row from
         an unrelated cast would not be.
    """
    out = _from_effects(spec, {})
    skills = list(getattr(holder, "skills", None) or [])
    if not skills:
        return out
    key = tuple(skills)
    if key not in _CAST_STATUS_CACHE:
        merged = {}
        for sid in skills:
            if sid:
                _from_effects(specs.skill(sid), merged)
        _CAST_STATUS_CACHE[key] = merged
    for name, val in _CAST_STATUS_CACHE[key].items():
        out.setdefault(name, val)
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
    if rule.effect == REVIVE:
        # A revive's percentage is of the REVIVED unit's own pool, not the holder's.
        return int(target.max_hp * mag / 100.0)
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
    ids = _status_ids(spec, holder)
    applied = []

    # Conditions are evaluated against the state at the START of the trigger, then all
    # the effects are applied. Otherwise rules see each other's work and order decides
    # the outcome: Gabriel's chain applies a Minor Demerit and the very next rule, whose
    # condition is "the target has a Minor Demerit", promotes it to Major in the same
    # hit. The prose means the state the hit LANDED on, and simultaneous resolution is
    # the general answer rather than hand-ordering every table.
    pending = []
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

        pending.append((i, rule, list(rule.to(holder, units, ctx))))

    for i, rule, chosen in pending:
        key = (passive_skill_id, i)
        if rule.effect != APPLY_STATUS:
            for target in chosen:
                amount = _amount(rule, holder, target, ctx)
                if rule.effect == HEAL:
                    target.hp = min(target.max_hp, target.hp + amount)
                elif rule.effect == DAMAGE:
                    target.hp = max(0, target.hp - amount)
                elif rule.effect == GAUGE:
                    target.scv = max(0.0, min(
                        100.0, float(getattr(target, "scv", 0.0)) + amount))
                elif rule.effect == REVIVE:
                    if target.alive:
                        continue
                    target.hp = max(1, amount)
                applied.append((target, rule.effect, amount))
            if rule.once and fired is not None:
                fired.add(key)
            continue

        found = ids.get(rule.status)
        if not found and not rule.kind:
            continue          # not in the cast's kit, and the rule does not synthesise
        sid, numbers = found if found else (None, {})
        row = (specs.status(sid) or {}) if sid else {}
        duration = rule.duration if rule.duration is not None else numbers.get("duration")
        if rule.permanent:
            duration = None
        elif duration is None and not numbers.get("permanent"):
            # Same rule as status.apply_event: unstated is NOT permanent.
            duration = _status.DEFAULT_DURATION
        for target in chosen:
            active = _status.Active(
                status_id=sid if sid is not None else -abs(hash(rule.status or "") % 10**6),
                name=row.get("name") or rule.status,
                kind=rule.kind or row.get("kind"),
                category=row.get("category") or ("buff" if rule.kind else None),
                stat=rule.stat or row.get("stat"), remaining=duration,
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

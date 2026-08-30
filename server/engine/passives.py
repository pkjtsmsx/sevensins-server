"""Passive skills, as a declarative rule table -- mostly DERIVED, a few hand-written.

Every passive has the same shape: *at TRIGGER, if CONDITION, apply STATUS to SELECTION*.

**A passive's opcodes carry only WHICH statuses exist**; the logic is in prose. This file
used to conclude from that "so they cannot be derived" and hand-write the shape per cast.
Six got written, against 1,516 passive groups in the game -- so `rules_for` returned an
empty list 1,510 times and a raid boss holding `CC Immunity (SP)` in its own effect list
was chain-frozen 19 times in one fight on a real device.

The conclusion was wrong in a specific way. Four fields of the five were ALREADY compiled
per effect off the clause naming the status -- the status, the recipient, the condition
and the numbers. Only the TRIGGER was never read, and it is stated in that same sentence
("When a battle starts, inflict Diligence on the 1 ally with the highest DEF"). So
`tools/compile_skills.py:annotate_passive` reads it, and `_compiled_rules` below builds
the same `Rule` objects this table holds: 936 groups, 1,953 rules. See
docs/BATTLE_ENGINE_PLAN.md phase 10 for the traps that came with it.

**The hand table still wins where it exists**, and is where the genuinely underivable
clauses live -- Gabriel's Campus Correction is HP thresholds and a promotion chain ("if
the target has three stacks of Admonition, additionally give a Minor Demerit") that no
single sentence states. Six entries is now a floor, not a ceiling: add one when the prose
defeats the compiler, not when the compiler merely has not been checked.

What a rule cannot express yet is stated in `UNMODELLED` per cast, and what the compiler
refused to derive is in the spec's own `unmodelled` list -- so both gaps are visible
rather than silently missing.

Numbers come from the compiled spec where the effect carries them, and from the prose
otherwise; the status IDS are read out of the passive's own effect list by name, so a
rule never invents a status the skill does not actually reference.
"""
import dataclasses
import re
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


def TOP(group, stat, n):
    """`group` allies or enemies, ranked by `stat`, top `n`.

    The named helpers below are the hand table's vocabulary; this is the one the compiled
    rules use, because "the 2 enemies with the highest SPD" is a shape the prose produces
    in every combination and enumerating them by hand is exactly what this work removes.
    """
    pick = ALLIES if group == "ally" else ENEMIES
    key = str(stat or "").lower()
    return lambda h, u, c=None: _top(pick(h, u), lambda x: getattr(x, key, 0) or 0, n)


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
        Rule(TURN_START, "Hold On, Classmates (SPD)", SELF, when=hp_at_least(0.70),
             kind="stat_mod", stat="SPD", magnitude=10.0, duration=1,
             note="2. Hold On, Classmates: when HP >= 70%, SPD +10% before action. "
                  "Rows 2645/2646 -- kept as a rule rather than a nested script because "
                  "the gate is an HP threshold, not holding a marker"),
        Rule(TURN_START, "Hold On, Classmates (ATK)", SELF, when=hp_at_least(0.70),
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
        # "Throughout Heaven and Earth" (CC Immunity while Divine), "I alone am honored"
        # (Keen) and Teardown are NOT rules any more: The Divine's own row is
        # `apply 701 CC Immunity` and The Fallen's is `apply 2007 Keen, apply 2009
        # Teardown`, and status.run_nested fires those every turn the marker is held.
        # Keeping the hand-written copies would refresh each one twice a turn.
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
    2090131: ["Return's on-hit damage lives in status.ON_HIT_EXTRA -- it is a status "
              "the ENEMY holds, so it cannot be a rule in MICHAEL's table: fire_all "
              "runs a passive for its own holder"],
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


_REGISTRY_BY_NAME = None


def _name_key(name):
    """Join key for a status name: punctuation-insensitive, qualifier-preserving."""
    n = (name or "").strip().lower()
    n = re.sub(r"[,'\u2019.!?\u3001]", "", n)
    n = re.sub(r"\s*\(\s*", "(", n)
    n = re.sub(r"\s*\)", ")", n)
    return " ".join(n.split())


def registry_id(name):
    """-> the status row id whose name is exactly `name`, or None.

    Tier 3, added after the synthesised ids turned out to be unnecessary for almost
    every rule that used them. A status is a GLOBAL entity, not a per-cast one: Lucifer's
    passive grants "Keen", and Keen is row 2007 -- in fact The Fallen's own row applies
    2007 and 2009 through its nested opcodes, so the rows were always the right answer
    and the hand-written rules were duplicating them under invented ids. A synthesised id
    has no design row, which costs the status its icon and its tooltip on the client.

    Matched on a key that normalises punctuation and spacing but KEEPS the parenthetical
    qualifier. The two halves matter:

      * loose matching folds "Keen", "Keen UL", "Keen(SP)" and "Keen(SP2)" together, and
        picking one of four arbitrarily is how a rule silently gets the wrong magnitude;
      * exact matching is too strict to survive transcription. The pack writes
        "Hold On, Classmates (ATK)" and this table wrote it without the comma, so the
        only rules left synthesising ids were the ones with a typo -- which is precisely
        the failure this lookup exists to prevent.

    An ambiguous name resolves to nothing and falls through to synthesising, as before.
    """
    global _REGISTRY_BY_NAME
    if _REGISTRY_BY_NAME is None:
        seen = {}
        for sid, row in (specs.statuses() or {}).items():
            nm = _name_key(row.get("name"))
            if nm:
                seen.setdefault(nm, []).append(int(sid))
        _REGISTRY_BY_NAME = {n: v[0] for n, v in seen.items() if len(v) == 1}
    return _REGISTRY_BY_NAME.get(_name_key(name))


# `defence` on the unit, `DEF` in the prose. Everything else spells the same.
_STAT_ATTR = {"DEF": "defence"}

_COMPILED = {}


def _selector(eff, category):
    """-> the selection a compiled effect acts on.

    `select` is the exact phrasing the compiler recovered ("the 1 ally with the highest
    DEF", "on the attacker"); `recipient` is the coarse fallback it has always emitted.
    When neither says, the STATUS decides: a passive that grants a buff grants it to
    itself, and one that inflicts a debuff inflicts it on the other side. Guessing the
    wrong way here is the bug that put a party buff on the raid boss, so the default is
    read off the thing being applied rather than off the skill's target.
    """
    sel = eff.get("select") or {}
    if sel.get("who") == "attacker":
        return ATTACKER
    if sel.get("top"):
        stat = _STAT_ATTR.get(sel["top"], sel["top"].lower())
        return TOP("ally" if sel.get("who") == "ally" else "enemy",
                   stat, int(sel.get("n") or 1))
    # `select.who` is read from the fragment governing THIS status, and cross-checked
    # against the original Chinese; `recipient` is the older sentence-wide guess. Prefer
    # the narrower reading -- a sentence that grants one status to the caster and inflicts
    # another on the enemy otherwise gives both of them the same answer, which is how
    # Beelzebub came to Headwind herself.
    #
    # NOTE: there is deliberately no "a passive may not harm its own caster" guard here.
    # Self-cost is a real mechanic -- Metatron's passive is called Sacrifice and triggers
    # on her own defeat, and an entire raid-boss family is built on it -- so such a guard
    # would silently delete correct rules to hide incorrect ones.
    if sel.get("who") in ("self", "ally", "enemy"):
        return {"self": SELF, "ally": ALLIES, "enemy": ENEMIES}[sel["who"]]
    recipient = eff.get("recipient")
    if recipient == "caster":
        return SELF
    if recipient == "allies":
        return ALLIES
    return ENEMIES if category in ("debuff", "damage_over_time") else SELF


def _denies_turns(eff):
    """Would this status stop its holder from taking turns? -> bool.

    Read from the registry rather than from a name list: a gauge-gain blocker is derived
    by `status._gauge_block_ids` from the rows' own wording, and `control` is the kind the
    immobilisers carry.
    """
    sid = (eff.get("status") or {}).get("id")
    if not sid:
        return False
    gain, _ = _status._gauge_block_ids()
    if sid in gain:
        return True
    return ((_status.specs.status(sid) or {}).get("kind") or "") == "control"


def _gauge_to(eff):
    """Who a compiled move-gauge change lands on.

    The move gauge is not a status and has no category to read a side off, and passing a
    stand-in category to `_selector` resolved 58 of the 91 derived gauge rules to ENEMIES
    -- including "after the action, increase the Move Gauge of all allies by 10%", which
    became a free 10% of a turn for the opposition, every turn. Reported from a device as
    units acting on a bar that was not full, which is exactly what an out-of-band gauge
    gain looks like.

    So the clause decides, and where the clause is silent the SIGN does: a gain goes to
    the caster's own side and a cut to the other one. Nothing in this game hands the
    enemy free gauge or drains its own.
    """
    sel = (eff.get("select") or {}).get("who")
    if sel in ("self", "ally", "enemy"):
        return {"self": SELF, "ally": ALLIES, "enemy": ENEMIES}[sel]
    target = eff.get("target")
    if target == "caster":
        return SELF
    if target == "allies":
        return ALLIES
    if target == "targets":
        return ENEMIES
    return ALLIES if float(eff.get("percent") or 0) > 0 else ENEMIES


def _compiled_rules(spec):
    """-> rules derived from the compiled spec, for a passive with no hand-written entry.

    Every field but the trigger was already compiled per effect; `tools/compile_skills.py`
    now reads the trigger out of the same clause. This is the whole reason the table
    below stops at six casts: what it expresses, the pack already states, and stating it
    twice means 1,510 passives that silently do nothing.

    Conventions are core.execute's, deliberately -- an effect should not behave
    differently for being on a passive:

      * a probability the prose states (`chance_pct`) is rolled as stated, and op 113
        with none stated falls back to core's neutral `UNSTATED_CHANCE`;
      * a `requires` clause becomes a real condition;
      * a conditional with nothing to evaluate follows CONDITIONAL_POLICY, which rolls.
    """
    rules = []
    # A status the passive already grants INDIRECTLY needs no rule of its own. Lucifer's
    # passive lists [The Divine, CC Immunity] and The Divine's own row nests CC Immunity,
    # which `status.run_nested` applies every turn the marker is held -- so a rule for it
    # would apply the same immunity a second time, on a different clock. This is the one
    # place the derived rules contradicted the hand-written table, and it is the hand
    # table that was right.
    nested = set()
    for eff in (spec or {}).get("effects") or []:
        sid = (eff.get("status") or {}).get("id")
        for sub in ((specs.status(sid) or {}).get("nested") or []) if sid else []:
            if sub.get("op") == "apply_status" and sub.get("status"):
                nested.add(int(sub["status"]))

    for eff in (spec or {}).get("effects") or []:
        trigger = eff.get("trigger")
        if trigger not in ALL_TRIGGERS:
            continue
        if (eff.get("status") or {}).get("id") in nested:
            continue
        if eff.get("trigger_source") == "implicit_trait" and _denies_turns(eff):
            # An UNDOCUMENTED status becomes a permanent always-on trait, which is right
            # for `Elite` and `CC Immunity (SP)` -- what a raid boss simply IS. It is
            # never right for something that takes the holder's turns away: no unit "is"
            # permanently stunned or permanently unable to fill its gauge. Zero Cal V
            # carries an undescribed `Headwind`, and defaulting it made the unit sit out
            # every fight -- permanently, so even the battle-clock ageing that catches an
            # ordinary gauge block could not free it.
            #
            # This is NOT a rule against a passive harming its caster; authored self-cost
            # is real (Metatron's passive is called Sacrifice) and reaches the engine
            # through the prose path untouched. It is a rule against INVENTING one.
            continue
        nums = eff.get("numbers") or {}
        requires = eff.get("requires") or {}
        when = ALWAYS
        # `holds` is the ONLY gate this path can express. Anything else -- an HP
        # threshold, a round parity, "if this attack killed" -- has no Rule form, and
        # the code used to fall through to ALWAYS for those: a rule the prose gates
        # behind a condition fired on every trigger, unconditionally, which is the exact
        # failure `requires` exists to prevent. Treated as unevaluatable instead, so it
        # takes CONDITIONAL_POLICY below like any other condition we cannot answer.
        unevaluatable = bool(requires) and not requires.get("status")
        if requires.get("status"):
            when = holds(requires["status"],
                         on="holder" if requires.get("on") == "caster" else "other")
        # Imported here, not at module scope: core imports THIS module, so reading the
        # policy at call time is what keeps the two from importing each other.
        from . import core as _core
        # A stated probability beats op 113's stand-in: the opcode only says "this one
        # is chancy", while the prose says how chancy. `chance_pct` is a PERCENT on
        # every path now (it used to be a fraction on passives alone, which read
        # correctly here and would have read as "always" the moment any other compiler
        # path wrote the key -- which `status_chance` now does); Rule.chance is a
        # fraction, so divide at the point of use.
        chance = eff.get("chance_pct")
        chance = None if chance is None else float(chance) / 100.0
        if chance is None and eff.get("chance"):
            chance = _core.UNSTATED_CHANCE
        if chance is None and (unevaluatable
                               or (eff.get("conditional") and not requires)):
            if _core.CONDITIONAL_POLICY == "skip":
                continue
            if _core.CONDITIONAL_POLICY == "roll":
                chance = _core.CONDITIONAL_CHANCE
        op = eff.get("op")
        if op == "apply_status":
            st = eff.get("status") or {}
            if not st.get("name"):
                continue
            rules.append(Rule(
                trigger, st["name"], _selector(eff, (st.get("category") or "").lower()),
                when=when, chance=chance,
                duration=nums.get("duration"),
                permanent=bool(nums.get("permanent")),
                magnitude=nums.get("magnitude"), stat=nums.get("stat"),
                note=f"compiled: {eff.get('trigger_source')}"))
        elif op == "modify_gauge" and eff.get("percent") is not None:
            rules.append(Rule(trigger, effect=GAUGE, to=_gauge_to(eff),
                              when=when, chance=chance,
                              magnitude=float(eff["percent"]), note="compiled"))
        elif op == "heal" and eff.get("percent") is not None:
            rules.append(Rule(trigger, effect=HEAL,
                              to=SELF if eff.get("target") == "caster" else ALLIES,
                              when=when, chance=chance, basis=OF_SELF_MAX_HP,
                              magnitude=float(eff["percent"]), note="compiled"))
        elif op == "revive" and eff.get("percent") is not None:
            rules.append(Rule(trigger, effect=REVIVE, to=RANDOM_DEAD_ALLY,
                              when=when, chance=chance,
                              magnitude=float(eff["percent"]), note="compiled"))
    return rules


def rules_for(skill_id):
    """-> the rules for a passive, by its group (all levels share one entry).

    The hand-written table wins where it exists -- it is read from prose by a human and
    covers clauses the compiler cannot express -- and everything else is derived. Before
    the fallback, 1,510 of 1,516 passive groups had no rules at all, which is why a raid
    boss with `CC Immunity (SP)` in its own effect list could be chain-frozen.
    """
    spec = specs.skill(skill_id) or {}
    group = spec.get("group") or skill_id
    hand = PASSIVES.get(group)
    if hand:
        return hand
    if group not in _COMPILED:
        _COMPILED[group] = _compiled_rules(spec)
    return _COMPILED[group]


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
    """Run one passive's rules for one trigger. -> what was actually applied.

    The list is MIXED, and the shape says which rule produced the row: a status rule
    yields `(unit, Active)`, a damage/heal/gauge/revive rule yields
    `(unit, effect, amount)`. This docstring claimed the first shape for both, which is
    how `core._report` came to unpack every row as a triple and crash the turn.

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
                    # Steady refuses a reduction; a gain still lands.
                    if amount < 0 and _status.blocks_gauge_loss(target):
                        continue
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
        if not found:
            # The cast's own kit did not name it, but the pack may still HAVE the row.
            reg = registry_id(rule.status)
            if reg is not None:
                found = (reg, {})
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

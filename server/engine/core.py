"""Target resolution and skill execution. Pure: no JSON, no sockets, no globals.

`execute` returns an `Outcome` describing what happened. It does NOT mutate anything the
caller did not hand it, and it does not know how the client wants any of this shaped --
that is phase 4's job, and keeping the two apart is what makes the wire invariants
assertable in one place.
"""
import dataclasses
import random
from typing import Any, Dict, List, Optional

from . import formula, passives as _passives, specs, status as _status

TEAM_PLAYER, TEAM_ENEMY = 1, 2

_char_rows = None


def attribute_of(char_id):
    """-> the char's attribute (`formula.STR` etc.), or 0 when it has none.

    Read from the design pack's `char._job`. Imported lazily so the engine stays usable
    -- and unit-testable -- without the design pack present; only this one function needs
    it, and only when a caller actually builds units from real characters.
    """
    global _char_rows
    if _char_rows is None:
        import design_data
        _char_rows = design_data.rows("char") or {}
    return int((_char_rows.get(int(char_id)) or {}).get("_job") or 0)

# A follow-up is a full skill spec that can itself follow up, and the compiled graph is
# checked acyclic by the harness -- but the harness checks the DATA, and a bug here
# (say, resolving a follow-up back onto its parent) would still hang the server rather
# than the client. The guard is cheap; a hung battle thread is not.
MAX_FOLLOW_DEPTH = 4


@dataclasses.dataclass(eq=False)
class Unit:
    """The engine's view of a combatant. Whatever the caller stores is its own business.

    **This is the shared unit model.** `battle.Unit` subclasses it and adds the identity,
    progression and wire-serialisation the old engine still owns, so both engines read
    and write ONE object -- the bridge does not mirror state between two models, and a
    status applied here is a status the old code sees. Keep this class lean: anything
    that knows about JSON, the design pack or the roster belongs in the subclass.

    `eq=False` on purpose. A unit is an entity, not a value: two combatants with
    identical stats are different units, and the generated field-wise `__eq__` would make
    `unit in targets` match the wrong one.

    `attribute` is the char row's `_job` -- see `formula` for the mapping and how it was
    established. Use `attribute_of(char_id)` rather than passing a literal. 0/None is
    neutral both ways, which is what the 237 rows whose badge the client hides should be.
    """
    order: str
    team: int
    max_hp: int
    hp: int
    atk: int = 0
    defence: int = 0
    spd: int = 0
    attribute: Optional[int] = None
    # Client stat line (BattleAttributeData). Fractions, not percents.
    cri: Optional[float] = None
    cdi: float = 0.0
    cdr: float = 0.0
    prc: float = 0.0
    ehit: float = 0.0
    eanti: float = 0.0
    ddi: float = 0.0
    ddr: float = 0.0
    statuses: List[dict] = dataclasses.field(default_factory=list)

    @property
    def alive(self):
        return self.hp > 0


@dataclasses.dataclass
class Strike:
    swing: int
    target: str
    amount: int
    detail: dict = dataclasses.field(default_factory=dict)
    died: bool = False


@dataclasses.dataclass
class StatusEvent:
    target: str
    status_id: int
    name: Optional[str]
    applied: bool                 # False = removed
    duration: Optional[int] = None
    magnitude: Optional[float] = None
    stacks: Optional[int] = None
    unknown_duration: bool = False
    permanent: bool = False
    # Filled in once the event has LANDED, from the resulting Active: the client's
    # status row has slots for these (StatusST.lv / .value / .actOn) and draws the stack
    # count on the icon and the shield amount on the shield bar from them.
    stacks_now: int = 0
    shield_hp: int = 0
    kind: Optional[str] = None
    # Shield sizing, from the prose: a flat point amount, or what the percentage is a
    # percentage OF ("atk" / "caster_max_hp" / "max_hp"). See status.apply_event.
    flat: Optional[float] = None
    basis: Optional[str] = None
    # +1/-1 when the prose stated a DIRECTION for the magnitude, else None. The compiler
    # has always emitted `numbers.magnitude_sign` and nothing in the server read it --
    # a grep for the key across server/ returned only a test. It matters because
    # `Active.signed_magnitude` falls back to the status's CATEGORY to decide up or
    # down, and a category outside buff/debuff leaves the direction unknown and the
    # magnitude unusable. All nine CRI stat_mods are `category: passive_grant`, so
    # every one of them was inert even once its magnitude arrived. See status.py.
    sign: Optional[int] = None


@dataclasses.dataclass
class Outcome:
    caster: str
    skill_id: int
    swings: int
    targets: List[str] = dataclasses.field(default_factory=list)
    strikes: List[Strike] = dataclasses.field(default_factory=list)
    statuses: List[StatusEvent] = dataclasses.field(default_factory=list)
    heals: List[dict] = dataclasses.field(default_factory=list)
    gauge: List[dict] = dataclasses.field(default_factory=list)
    revives: List[dict] = dataclasses.field(default_factory=list)
    # Targets an immunity refused a status on. The client has a floating text for it
    # (DamageMode.Immunity = 10097 -> "IMMUNE"); silently dropping the application
    # left the player with no idea why a debuff did nothing.
    immune: List[str] = dataclasses.field(default_factory=list)
    # Signed cooldown changes: negative refreshes, positive delays. The gauge and the
    # cooldown are both server-authoritative and travel in `sync`/`skill_list`, not as
    # DamageInfo rows -- see wire.py on why mode 4 is never emitted.
    cooldowns: List[dict] = dataclasses.field(default_factory=list)
    # Effects the compiled spec flagged as not executable, carried through so a caller
    # can log or assert on them instead of silently running a partial skill.
    skipped: List[dict] = dataclasses.field(default_factory=list)
    children: List["Outcome"] = dataclasses.field(default_factory=list)

    def total_damage(self):
        return sum(s.amount for s in self.strikes) + sum(
            c.total_damage() for c in self.children)


# --- targeting --------------------------------------------------------------------

def _eligible(caster, unit, group):
    """`SkillTargetUtil.IsPossibleTarget`, transcribed. See contract doc 2."""
    same = unit.team == caster.team
    if group == "enemy":
        return not same and unit.alive
    if group == "ally":
        return same and unit.alive
    if group == "dead_enemy":
        return not same and not unit.alive
    if group == "dead_ally":
        return same and not unit.alive
    return False


def resolve_targets(caster, spec, units, rng=None, chosen=None):
    """-> the units this skill actually strikes.

    `units` is any iterable of Unit. `chosen` is the player's picked target order, used
    only where the breadth rule is "one of the eligible" -- the client lets the player
    choose, and ignoring that pick is how a single-target skill ends up hitting whoever
    the server felt like.

    Eligibility (who *may* be hit) and breadth (how many actually are) are separate in
    the client and stay separate here: `IsPossibleTarget` never limits count, the
    `23000 + _target` label table does.
    """
    r = rng or random.Random()
    t = spec.get("target") or {}
    group, select = t.get("group"), t.get("select")
    if select in (None, "none", "unknown"):
        return []

    pool = [u for u in units if _eligible(caster, u, group)]
    if not pool:
        return []
    count = t.get("count")

    if select == "all":
        return pool
    if select == "all_except_self":
        return [u for u in pool if u.order != caster.order]
    if select == "self_plus":
        others = [u for u in pool if u.order != caster.order]
        r.shuffle(others)
        return [caster] + others[:max(0, (count or 1) - 1)]
    if select == "random":
        r.shuffle(pool)
        return pool[:count or 1]
    if select in ("highest", "lowest"):
        stat = (t.get("stat") or "HP").upper()
        key = {"HP": lambda u: u.hp, "ATK": lambda u: u.atk,
               "DEF": lambda u: u.defence, "SPD": lambda u: u.spd}.get(
                   stat, lambda u: u.hp)
        return [max(pool, key=key) if select == "highest" else min(pool, key=key)]
    if select == "by_attribute":
        want = {"STR": formula.STR, "AGI": formula.AGI,
                "TEC": formula.TEC}.get(t.get("attribute"))
        match = [u for u in pool if u.attribute == want]
        return match[:1] or pool[:1]
    if select == "count":
        n = count or 1
        # The player's pick leads, then fill deterministically by slot so a 3-target
        # skill is reproducible for a given field.
        if chosen:
            lead = [u for u in pool if u.order == chosen]
            rest = [u for u in pool if u.order != chosen]
            return (lead + rest)[:n]
        return pool[:n]
    return pool[:count or 1]


# --- execution --------------------------------------------------------------------

# What to do with an apply_status whose prose gates it on a condition the opcodes do not
# encode ("if the caster is affected by The Divine, ... inflict freeze"). 4,934 of the
# 21,407 sites are like this, 875 of them control effects.
#
# Applying them unconditionally is not neutral: it permanently froze AND stunned a raid
# boss, because two conditional control effects landed on every cast and the boss never
# got a turn. Skipping them outright loses a quarter of all status gameplay.
#
# So the default is to roll: an unevaluatable condition is treated as "sometimes true",
# through the same effect-accuracy path a chance-based application uses. That is a
# MODELLING CHOICE, not a reading -- named here so it is visible and tunable rather than
# buried. Set "skip" to drop them entirely, "always" for the old behaviour.
CONDITIONAL_POLICY = "roll"
CONDITIONAL_CHANCE = 0.5

# The stand-in for op 113 when the row's own prose states no probability -- 295 of the
# 688 op-113 sites. A MODELLING CHOICE, like the two above: the opcode says "resistible"
# and nothing in the pack says how resistible, so this is a neutral base fed through the
# effect-accuracy path rather than a number read off anything. Where the prose DOES state
# odds, they are compiled to `chance_pct` and this is not consulted.
UNSTATED_CHANCE = 0.75


def _status_recipients(who, caster, targets, units):
    """-> who a status is applied to. Defaults to the skill's targets."""
    if who == "caster":
        return [caster]
    if who == "allies":
        return [u for u in units if u.team == caster.team and u.alive]
    return targets


def _heal_recipients(who, caster, targets, units, out, op, skill_id):
    """-> who a heal actually restores, or [] when the prose does not say."""
    if who == "caster":
        return [caster]
    if who == "allies":
        return [u for u in units if u.team == caster.team and u.alive]
    if who == "targets":
        return targets
    out.skipped.append({"op": op, "why": "heal recipient unknown", "skill": skill_id})
    return []


def _held_names(unit):
    return [str(getattr(st, "name", "") or "").lower()
            for st in getattr(unit, "statuses", [])]


def _snapshot(units):
    """Status names per unit, frozen BEFORE any of this action's effects land.

    Every `requires` gate in one action is judged against this, not against the running
    state. Lucifer's Lamenting Starlight is the case that forces it: its two clauses are
    a toggle -- hold The Divine, get The Fallen; hold The Fallen, get The Divine -- so
    resolving them in sequence let the first clause's grant satisfy the second clause's
    gate and the marker flipped straight back inside a single cast. Same shape as the
    passive rules, which evaluate every condition before applying any effect.
    """
    return {id(u): _held_names(u) for u in units}


def _holds_status(unit, name, snapshot=None, count=None):
    """Does this unit hold a status by (loose) name -- and, if asked, N stacks of it?

    Loose because the prose and the status row do not always spell it identically --
    "The Divine" in a clause is `Divine` once the article is stripped, and the row may
    carry a `(SP)` suffix. Substring either way, lowercased.

    `count` is a MINIMUM, which is what every stack phrasing in the corpus means
    (若自身「盛怒」達到5層, 至少有1層, 3層以上). It reads live `Active.stacks` rather than
    the name snapshot, because the snapshot holds names only; the gate that motivated
    it is about how much of a marker the caster has built up, not about a toggle two
    clauses of one cast could flip.

    -> True / False, or **None** when the status is held but cannot stack at all, so
    the count is unreachable rather than merely unmet.
    """
    if not name:
        return False
    want = str(name).strip().lower()
    if count is None:
        names = (snapshot or {}).get(id(unit)) if snapshot is not None else None
        if names is None:
            names = _held_names(unit)
        return any(got and (want in got or got in want) for got in names)
    for st in getattr(unit, "statuses", []):
        got = str(getattr(st, "name", "") or "").lower()
        if not got or not (want in got or got in want):
            continue
        if int(getattr(st, "stacks", 1) or 1) >= int(count):
            return True
        if int(count) > 1 and not getattr(st, "stack_cap", None):
            # Held, but the registry gives this status no stack cap, so `stacks` is
            # pinned at 1 and a "5 stacks of X" gate could never open no matter how the
            # fight went. That is a shut gate, not a false one -- 162 of the 193 count
            # gates name such a status, because `stack_cap` is read only off the `(N)`
            # suffix in the English name while the CHINESE states it in prose
            # (可疊加5次) for 86 statuses. Until the registry reads that, say so.
            return None
    return False


def _condition_met(requires, caster, target, snapshot=None, ctx=None):
    """Evaluate a compiled `requires` gate. -> True, False, or **None**.

    None means *this engine cannot answer the question here* -- not "false". The caller
    falls back to CONDITIONAL_POLICY for it, which is the same treatment an unparsed
    condition gets, because that is exactly what it is from the engine's side. Returning
    False instead would silently delete the effect; returning True would be the
    unconditional firing this whole mechanism exists to stop.

    The shapes, and where each one gets its answer:

    {"status": X, "on": caster|target, "negate": bool, "count": N} -- "if the caster is
      affected by The Divine" (Eclipse Slash), 若目標未擁有麻痺 with negate,
      若自身擁有5層Reload with count. Against the pre-action snapshot, not the running
      state: Lucifer's toggle otherwise satisfies its own second clause.
    {"hp": {"on": ..., "cmp": ..., "pct": N}} -- 若自身體力低於50%. Live HP.
    {"killed": bool} -- 若本次攻擊擊倒敵人. From `ctx["targets"]`: `execute` mutates HP
      through the whole swing loop before any non-damage effect runs, so whether this
      cast killed is already settled by the time a status asks.
    {"crit": bool} -- 若本次攻擊暴擊. From `ctx["strikes"]`, same ordering guarantee.
    {"round": {"parity": 0|1}} or {"round": {"cmp": ..., "n": N}} -- 奇數/偶數回合,
      總回合數不高於3. Needs `ctx["round"]`, which only a caller that HAS a battle can
      supply; without it the answer is None rather than a guess.
    """
    ctx = ctx or {}

    killed = requires.get("killed")
    if killed is not None:
        struck = ctx.get("targets")
        if struck is None:
            return None
        return bool(any(not u.alive for u in struck)) is bool(killed)

    crit = requires.get("crit")
    if crit is not None:
        strikes = ctx.get("strikes")
        if strikes is None:
            return None
        return bool(any((st.detail or {}).get("crit") for st in strikes)) is bool(crit)

    rnd = requires.get("round")
    if rnd:
        now = ctx.get("round")
        if now is None:
            return None
        now = int(now)
        if rnd.get("parity") is not None:
            return now % 2 == int(rnd["parity"])
        n, cmp_ = int(rnd.get("n") or 0), rnd.get("cmp")
        return {"==": now == n, "<=": now <= n, ">=": now >= n,
                "<": now < n, ">": now > n}.get(cmp_, None)

    hp = requires.get("hp")
    if hp:
        holder = caster if hp.get("on") == "caster" else target
        if holder is None or not getattr(holder, "max_hp", 0):
            return None
        now = 100.0 * float(holder.hp) / float(holder.max_hp)
        want = float(hp.get("pct") or 0)
        return {">": now > want, ">=": now >= want, "<": now < want,
                "<=": now <= want}.get(hp.get("cmp"), None)

    if not requires.get("status"):
        return None
    if requires.get("resolved") is False:
        # The compiler read a gate out of the prose and could NOT map the name it found
        # to a status row -- 若自身沒有疲勞, 若擁有5層時 with the marker elided, and the
        # category conditions (能力下降 is "a stat-down", not a status). Matching that
        # name against held statuses would fail every time, which is a gate that can
        # never open: the effect would be deleted from the game rather than gated. So
        # it is unevaluatable, and takes the same policy as an unparsed condition.
        # `resolved` is set only by the Chinese reader; the English path omits it, and
        # those names come from the registry already.
        return None
    holder = caster if requires.get("on") == "caster" else target
    held = _holds_status(holder, requires["status"], snapshot, requires.get("count"))
    if held is None:                     # a count we have no way to reach -- see above
        return None
    return (not held) if requires.get("negate") else held


def _status_event(caster, target, eff, rng, snapshot=None, ctx=None):
    """-> a StatusEvent, or None when the application does not land."""
    st = eff.get("status") or {}
    numbers = eff.get("numbers") or {}
    requires = eff.get("requires")
    met = _condition_met(requires, caster, target, snapshot, ctx) if requires else None
    if met is False:
        return None
    # `met is None` -- either no condition was compiled, or one was and this engine
    # cannot answer it here (a round gate with no battle behind the call). Both are
    # "unevaluatable", and both take the policy below rather than firing.
    if met is None and eff.get("conditional") and not eff.get("chance") \
            and eff.get("chance_pct") is None:
        if CONDITIONAL_POLICY == "skip":
            return None
        if CONDITIONAL_POLICY == "roll" and not formula.effect_lands(
                caster, target, CONDITIONAL_CHANCE, rng):
            return None
    stated = eff.get("chance_pct")
    if stated is not None:
        # The prose states the odds -- "30%固定機率附加暈眩" -- and they win outright over
        # the stand-in below. Emitted for op 112 as well as 113: see status_chance in
        # tools/compile_skills.py for why the opcode does not decide this.
        if not formula.effect_lands(caster, target, float(stated) / 100.0, rng):
            return None
    elif eff.get("chance"):
        # op 113 says the application is resistible and this row's prose states no
        # number, so the engine uses the effect-accuracy path with a neutral base
        # rather than inventing a per-skill one.
        if not formula.effect_lands(caster, target, UNSTATED_CHANCE, rng):
            return None
    dur = numbers.get("duration")
    return StatusEvent(
        target=target.order, status_id=st.get("id"), name=st.get("name"),
        applied=True, duration=dur, magnitude=numbers.get("magnitude"),
        stacks=numbers.get("stacks"), flat=numbers.get("flat"), basis=numbers.get("basis"),
        sign=numbers.get("magnitude_sign"),
        unknown_duration=dur is None and not numbers.get("permanent"),
        permanent=bool(numbers.get("permanent")))


def _removable(status_row, category):
    """Whether a cleanse of `category` may strip this status.

    509 statuses declare `(unremovable)` in prose. op 114 removes by CATEGORY BLOCK, so
    without this check a cleanse strips statuses the game says it must not.
    """
    if status_row and status_row.get("unremovable"):
        return False
    return category in (None, "any") or (status_row or {}).get("category") == category


def execute(caster, spec, units, rng=None, chosen=None, depth=0, apply_damage=True,
            coefficient_override=None, round_no=None):
    """Run one skill. -> Outcome.

    `apply_damage` mutates target HP as it goes, because later swings of a multi-hit
    skill must see the damage the earlier ones did -- a target that died on swing 2 is
    not struck again on swing 3, and the client's `die` flag depends on that ordering.
    Callers wanting a dry run pass False.

    `round_no` is the battle's round, needed by the 奇數/偶數回合 gates. It has no
    sensible default -- a caller with no battle behind it (the AI's dry runs, the fuzzer)
    passes nothing and those gates come back unevaluatable, which is honest. Guessing 1
    would make every "odd round" clause fire on every cast in the game.

    `coefficient_override` supplies the damage coefficient for a spec that has none of
    its own. Pursuit sub-skills need it: their design row carries no numbers at all
    (100000341 is `_note1_jp` "脊砕き 追加技能" and nothing else), and the figure lives in
    the PARENT's prose -- 追擊(造成120%攻擊力傷害). Only fills a gap; a child that states
    its own coefficient keeps it.
    """
    r = rng or random.Random()
    skill_id = spec.get("id")
    swings = int(spec.get("swings") or 1)
    out = Outcome(caster=caster.order, skill_id=skill_id, swings=swings)

    targets = resolve_targets(caster, spec, units, r, chosen)
    out.targets = [u.order for u in targets]

    effects = spec.get("effects") or []
    dmg = [e for e in effects if e["op"] == "damage"]

    # --- damage, one pass per swing ------------------------------------------------
    #
    # The swing count comes from the CINEMATIC (contract doc 3), and the client consumes
    # exactly one DamageInfo group per Damage tag. So the engine produces `swings`
    # passes here and phase 4 asserts the group count matches. Producing fewer is the
    # original silent bug: the animation plays with no number.
    for e in dmg:
        for sw in range(swings):
            for tgt in targets:
                # A target that was alive when the skill STARTED takes every swing, even
                # if an earlier one killed it. Skipping the dead mid-skill looks correct
                # and is not: a 4-hit skill that overkills on swing 1 then emits nothing
                # for swings 2-4, those groups get trimmed, and the client -- whose
                # cinematic fires four Damage tags regardless -- animates three swings
                # with no number. Found on device: Frozen Inferno Thorn III shipped ONE
                # group instead of four. Overkill is simply wasted; death resolves at the
                # end of the skill, which is also where the `die` flag belongs.
                coef = e.get("coefficient")
                if coef is None:
                    coef = coefficient_override
                amount, detail = formula.strike(
                    caster, tgt, coef, e.get("basis") or "ATK", r)
                if amount is None:
                    out.skipped.append({"op": "damage", "why": "coefficient unknown",
                                        "skill": skill_id})
                    continue
                if apply_damage:
                    # Shields eat damage before HP does, and report what they took so the
                    # number the client shows is the damage that actually landed.
                    amount, absorbed = _status.absorb(tgt, amount)
                    if absorbed:
                        detail["absorbed"] = absorbed
                    tgt.hp = max(0, tgt.hp - amount)
                out.strikes.append(Strike(swing=sw, target=tgt.order, amount=amount,
                                          detail=detail, died=False))

    # Frozen before the non-damage effects run; see _snapshot.
    held = _snapshot(list(units) + [caster])
    # What this cast has already done, for the gates that ask about it: 若本次攻擊擊倒敵人
    # reads `targets` (HP is final by now -- the swing loop above mutated it), and
    # 若本次攻擊暴擊 reads `strikes`.
    ctx = {"strikes": out.strikes, "targets": targets, "round": round_no}

    # --- everything else, once ------------------------------------------------------
    for e in effects:
        op = e["op"]
        if op == "damage":
            continue
        if op == "apply_status":
            # The recipient is not always the skill's target -- an attack routinely
            # buffs its own side. `None` means the prose did not say, which is the
            # ordinary "inflicts X on the target" case.
            for tgt in _status_recipients(e.get("recipient"), caster, targets, units):
                ev = _status_event(caster, tgt, e, r, held, ctx)
                if ev is None:
                    continue
                # Land it on the unit as STATE, not just on the wire. The unit is shared
                # with the old engine (battle.Unit subclasses Unit), so this is the same
                # list everything else reads.
                if apply_damage:
                    active = _status.apply_event(tgt, ev, caster)
                    if active is None:
                        # Blocked by an immunity. Not a status row -- but the client
                        # can SAY so: an "IMMUNE" floating text (DamageMode 10097).
                        if tgt.order not in out.immune:
                            out.immune.append(tgt.order)
                        continue
                    # What the client draws for this status comes from the landed
                    # Active, not the event: the stack count after this application
                    # and the shield amount (only a shield has one).
                    ev.stacks_now = int(getattr(active, "stacks", 1) or 1)
                    ev.shield_hp = int(getattr(active, "shield_hp", 0) or 0)
                    ev.kind = getattr(active, "kind", None)
                # A clause may name the status this one REPLACES ("grants the caster The
                # Fallen and removes its the Divine effect"). Only once it landed --
                # an application an immunity blocked must not strip anything.
                for dead in (_status.remove_named(tgt, e["removes"])
                             if apply_damage and e.get("removes") else []):
                    out.statuses.append(StatusEvent(
                        target=tgt.order, status_id=dead.status_id, name=dead.name,
                        applied=False))
                out.statuses.append(ev)
        elif op == "remove_status":
            cat = e.get("category") or (e.get("status") or {}).get("category")
            for tgt in targets:
                for dead in _status.remove_category(tgt, cat):
                    # The id, not just the name -- a removal reaches the client as a
                    # status row with that id and a round of 0.
                    out.statuses.append(StatusEvent(
                        target=tgt.order, status_id=dead.status_id, name=dead.name,
                        applied=False))
        elif op == "heal":
            pct = e.get("percent")
            if pct is None:
                out.skipped.append({"op": op, "why": "magnitude unknown",
                                    "skill": skill_id})
            else:
                # The RECIPIENT is not the skill's target. Michael's Gate of Judgement
                # "restores HP of all allies" is an enemy-targeting attack, so healing
                # its targets healed the raid boss -- five casts using it made the fight
                # unendable. Unknown recipient is skipped, not guessed.
                recip = _heal_recipients(e.get("target"), caster, targets, units, out,
                                         op, skill_id)
                # WHAT the percentage is a percentage OF. The pack writes heals both
                # ways -- "recovers the caster's Max HP by 15%" and "restores HP of all
                # allies by 250% ATK" -- and the two differ by an order of magnitude.
                # Reading every heal as max-HP made Michael's zero-cooldown skill heal
                # each ally for 250% of their own max HP, i.e. a guaranteed full-party
                # heal every turn. An ATK heal scales off the CASTER, a max-HP heal off
                # the unit being healed.
                basis = e.get("basis") or "max_hp"
                # Three bases, and only the last one scales per recipient.
                fixed = None
                if basis == "atk":
                    fixed = formula.effective_atk(caster)
                elif basis == "caster_max_hp":
                    fixed = float(caster.max_hp)
                for tgt in recip:
                    if _status.blocks_heal(tgt):
                        # `Block Heal` / `Heal Block`. Nothing enforced this on the new
                        # path -- the old check reads `st.definition`, which an engine
                        # status does not have -- so the boss's heal-block was decorative.
                        out.skipped.append({"op": op, "why": "heal blocked",
                                            "skill": skill_id, "target": tgt.order})
                        continue
                    pool = fixed if fixed is not None else float(tgt.max_hp)
                    amount = int(pool * pct / 100.0)
                    if apply_damage:
                        tgt.hp = min(tgt.max_hp, tgt.hp + amount)
                    out.heals.append({"target": tgt.order, "amount": amount,
                                      "basis": basis})
        elif op == "modify_gauge":
            pct = e.get("percent")
            if pct is None:
                out.skipped.append({"op": op, "why": "magnitude unknown",
                                    "skill": skill_id})
            elif e.get("chance_pct") is not None and not formula.effect_lands(
                    caster, caster, float(e["chance_pct"]) / 100.0, r):
                # "10%的機率使自己可以再度行動": the extra turn is a ROLL, and the
                # compiler put the stated odds here rather than in the magnitude.
                out.skipped.append({"op": op, "why": "chance failed", "skill": skill_id})
            else:
                # The RECIPIENT is not the skill's target. The prose says whose gauge
                # moves, and it is usually the caster or an ally: "the caster's Move
                # Gauge will increase 25%", "Grant the ally with the highest ATK an Move
                # Gauge increase of 40%". Handing it to `targets` would speed up the
                # enemies the skill just hit.
                who = e.get("target")
                if who == "caster":
                    recip = [caster]
                elif who == "allies":
                    # Approximation: the clause often names ONE ally ("with the highest
                    # ATK") and that selection is not modelled, so the strongest living
                    # ally stands in. Applying it to the whole team would be a bigger
                    # error than picking the wrong single ally.
                    mates = [u for u in units
                             if u.team == caster.team and u.alive]
                    recip = [max(mates, key=lambda u: u.atk)] if mates else []
                elif who == "targets":
                    recip = targets
                else:
                    out.skipped.append({"op": op, "why": "gauge recipient unknown",
                                        "skill": skill_id})
                    recip = []
                out.gauge.extend({"target": t.order, "percent": pct} for t in recip)
        elif op == "revive":
            pct = e.get("percent")
            # A revive raises the CASTER's fallen allies, not the units it is aimed at.
            who = e.get("target") or "allies"
            pool = ([u for u in units if u.team == caster.team and not u.alive]
                    if who in ("allies", "caster") else
                    [t for t in targets if not t.alive])
            for tgt in pool:
                hp = int(tgt.max_hp * (pct or 0) / 100.0) or 1
                if apply_damage:
                    tgt.hp = hp
                out.revives.append({"target": tgt.order, "hp": hp})
        elif op == "attack_rider":
            _rider(caster, e, targets, out, r, apply_damage, skill_id, units, held, ctx)
        elif op == "follow_up":
            child = specs.skill(e["skill"])
            # Tri-state, like _status_event: False skips, None means the gate could not
            # be answered here and takes the conditional policy. `not _condition_met(..)`
            # was correct only while the function returned a plain bool -- once None
            # became "unevaluatable", it would have skipped every round-gated pursuit
            # outright instead of rolling for it.
            gate = _condition_met(e["requires"], caster,
                                  targets[0] if targets else None, held, ctx) \
                if e.get("requires") else None
            if gate is False:
                out.skipped.append({"op": op, "why": "condition not met", "skill": e["skill"]})
            elif gate is None and e.get("requires") and CONDITIONAL_POLICY == "skip":
                out.skipped.append({"op": op, "why": "condition unevaluatable",
                                    "skill": e["skill"]})
            elif gate is None and e.get("requires") and CONDITIONAL_POLICY == "roll" \
                    and not formula.effect_lands(caster, caster, CONDITIONAL_CHANCE, r):
                out.skipped.append({"op": op, "why": "condition unevaluatable",
                                    "skill": e["skill"]})
            elif e.get("chance_pct") is not None and not formula.effect_lands(
                    caster, caster, float(e["chance_pct"]) / 100.0, r):
                # 以50%機率追擊 -- the pursuit is a ROLL. Every follow_up used to fire
                # unconditionally because the compiler dropped the stated odds and this
                # branch never looked for them, so 240 casts pursued on every cast.
                out.skipped.append({"op": op, "why": "chance failed", "skill": e["skill"]})
            elif child is None:
                out.skipped.append({"op": op, "why": "unknown skill", "skill": e["skill"]})
            elif depth >= MAX_FOLLOW_DEPTH:
                out.skipped.append({"op": op, "why": "follow-up depth limit",
                                    "skill": e["skill"]})
            else:
                # A sub-skill reached via follow_up inherits the invoker's target set --
                # its own `_target` is a placeholder (contract doc: `Refrain` from
                # `Aurora`). Passing the lead target keeps that inheritance.
                out.children.append(execute(
                    caster, child, units, r,
                    chosen=(targets[0].order if targets else None),
                    depth=depth + 1, apply_damage=apply_damage,
                    # The pursuit's damage figure is stated by the PARENT, not by the
                    # sub-skill's own row -- see execute's docstring.
                    coefficient_override=e.get("coefficient"),
                    round_no=round_no))
        elif op == "modify_cd":
            turns = e.get("turns")
            if turns is None:
                out.skipped.append({"op": op, "why": "delta unknown", "skill": skill_id})
            else:
                # Recipient read from the clause like everything else -- a CD change is
                # as often "the ally with the highest ATK" as it is the unit being hit.
                for tgt in _status_recipients(e.get("target"), caster, targets, units):
                    if turns < 0 and _status.blocks_cd_reduction(tgt):
                        # A refresh is refused; a DELAY still lands.
                        out.skipped.append({"op": op, "why": "cd reduction blocked",
                                            "skill": skill_id, "target": tgt.order})
                        continue
                    out.cooldowns.append({"target": tgt.order, "turns": int(turns)})
        else:
            out.skipped.append({"op": op, "why": "unhandled", "skill": skill_id})

    if apply_damage and depth == 0:
        # Damage-triggered passives and status behaviours, once per SKILL rather than
        # per swing: "triggers once while dealing multiple attacks" is how the prose
        # words it, and a 4-hit skill paying four reflects would be wrong.
        _damage_hooks(caster, targets, out, units, r)

    # AFTER the riders and follow-ups: those deal damage too, so flagging deaths any
    # earlier would miss a kill that a rider landed.
    _flag_deaths(out, targets, units)

    # Effects the compiler could not decode at all. Carried, not dropped: a caller that
    # wants to know "did this skill run in full?" must be able to ask.
    for u in spec.get("unknown") or []:
        out.skipped.append({"op": f"raw_{u.get('opcode')}", "why": "undecoded opcode",
                            "skill": skill_id})
    return out


def _report(out, applied):
    """Put a passive's damage and healing on the WIRE, not just on the unit.

    `fire()` mutates HP and returns what it did; every caller discarded that, so a
    counter-attack, a retaliation or an on-death heal changed the server's numbers and
    the client was never told -- no damage popup, and the HP bar only caught up at the
    next `sync`. This is the hook counters ride: a counter is
    `Rule(ON_DAMAGE_TAKEN, effect=DAMAGE, to=ATTACKER, ...)`, which the rule table
    already expresses.
    """
    for row in applied or []:
        # `fire()` returns a MIXED list: (target, effect, amount) for a damage or heal
        # rule, but (target, Active) for one that grants a status. Unpacking everything as
        # a triple crashed the fight outright -- ValueError, mid-turn -- for any passive
        # whose damage trigger grants a status. The stage suite never fielded one; random
        # five-cast teams in tools/ai_arena.py hit it immediately.
        if len(row) == 2:
            target, active = row
            # It already landed on the unit. The client is told for exactly the reason
            # this function exists: state the server applied and never reported leaves the
            # icon missing until the next sync.
            out.statuses.append(StatusEvent(
                target=target.order, status_id=active.status_id, name=active.name,
                applied=True, duration=active.remaining, magnitude=active.magnitude,
                stacks=active.stacks, permanent=active.permanent))
            continue
        target, effect, amount = row
        if effect == _passives.REMOVE:
            # A triggered cleanse. `amount` is the Active that was removed, not a
            # number: the client learns about a removal from a status row carrying that
            # id with a round of 0, so the id has to survive to here (status.py's
            # remove_category returns the objects for exactly this reason).
            out.statuses.append(StatusEvent(
                target=target.order, status_id=amount.status_id, name=amount.name,
                applied=False))
            continue
        if not amount:
            continue
        if effect == _passives.DAMAGE:
            # Past the last swing, same as the on-hit extra: wire.py clamps it into the
            # final group, so a counter shows as part of the last hit rather than
            # colliding with a row the unit already has in swing 0.
            swing = max((s.swing for s in out.strikes), default=0) + 1
            out.strikes.append(Strike(swing=swing, target=target.order,
                                      amount=int(amount), detail={"counter": True}))
        elif effect == _passives.HEAL:
            out.heals.append({"target": target.order, "amount": int(amount),
                              "basis": "passive"})


def _damage_hooks(caster, targets, out, units, rng):
    """Reflects and on-hit passives, after all of a skill's damage has landed."""
    struck = {s.target for s in out.strikes}
    if not struck:
        return
    ctx = {"attacker": caster, "rng": rng}

    for tgt in targets:
        if tgt.order not in struck:
            continue
        extra = _status.on_hit_extra_damage(tgt)
        if extra:
            # Lands on the TARGET, not the attacker: `Return` is a debuff Michael puts
            # on enemies, so it adds to what they take. Indexed past the last swing so
            # it does not collide with the target's existing row in swing 0 -- the wire
            # allows one row per unit per group. wire.py then CLAMPS it back into the
            # final swing and sums it there, which is the right outcome: the client
            # shows one number per swing, and the extra rides the last hit.
            tgt.hp = max(0, tgt.hp - extra)
            swing = max((s.swing for s in out.strikes), default=0) + 1
            out.strikes.append(Strike(swing=swing, target=tgt.order, amount=extra,
                                      detail={"on_hit_extra": True}))
        _report(out, _passives.fire_all(
            _passives.ON_DAMAGE_TAKEN, [tgt], units,
            ctx={"attacker": caster, "rng": rng},
            fired=getattr(caster, "_passives_fired", None)))

    _report(out, _passives.fire_all(
        _passives.ON_DAMAGE_DEALT, [caster], units, ctx=ctx,
        fired=getattr(caster, "_passives_fired", None)))

    # Deaths this skill caused. Fired for every unit's passive, not just the killer's:
    # Metatron revives on an ALLY's death, and she may not be the one who acted.
    for tgt in targets:
        if not tgt.alive:
            _report(out, _passives.fire_all(
                _passives.ON_DEATH, list(units), units,
                ctx={"victim": tgt, "attacker": caster, "rng": rng},
                fired=getattr(caster, "_passives_fired", None)))


def _flag_deaths(out, targets, units=()):
    """Mark `died` on the LAST strike naming each unit that ended the skill dead.

    Exactly one row per unit, which is what the wire requires -- rows carry final state,
    so flagging every strike after the fatal one would replay the death animation on
    every remaining swing.

    CANDIDATES ARE EVERY UNIT A STRIKE NAMES, not just the skill's targets. A counter
    kills the ATTACKER, who is never in `targets` -- so a counter that finished off the
    attacker sent `die: 0` on its row and the client was never told that unit had died.
    That is the same shape as the DoT soft-lock: the client keeps the unit in its own
    ActionOrderList and waits for a turn the server will never hand out. Reading the
    candidates off the strikes instead covers counters, the on-hit extra and the riders
    in one place, and cannot miss a future path for the same reason -- anything that
    deals damage has to produce a strike to be on the wire at all.
    """
    by_order = {u.order: u for u in (list(targets) + list(units or []))}
    dead = {o for o in {st.target for st in out.strikes}
            if by_order.get(o) is not None and not by_order[o].alive}
    if not dead:
        return
    for st in reversed(out.strikes):
        if st.target in dead:
            st.died = True
            dead.discard(st.target)
            if not dead:
                return


def _rider(caster, eff, targets, out, rng, apply_damage, skill_id, units=(),
           held=None, ctx=None):
    """op 1 -- the attack rider, and op 6 -- the same shape sized off the caster's HP.

    Kind comes from prose; see contract doc 6.3.3. `basis` is ATK unless the compiler
    says otherwise: op 6 sets `caster_current_hp` / `caster_max_hp` (zh_hp_rider), and
    those are read off the caster at the moment the rider fires -- AFTER this skill's
    own swings, so a self-damaging cast sizes its rider off what it has left.
    """
    kind, pct = eff.get("kind"), eff.get("percent")
    if pct is None:
        out.skipped.append({"op": "attack_rider", "why": "magnitude unknown",
                            "skill": skill_id})
        return
    if eff.get("requires"):
        # 攻擊時若自身擁有共享盛宴，額外對目標造成… -- most op-6 riders are gated. Tri-state
        # like everything else; an unanswerable gate takes the conditional policy.
        gate = _condition_met(eff["requires"], caster, targets[0] if targets else None,
                              held, ctx)
        if gate is False or (gate is None and CONDITIONAL_POLICY == "skip") or (
                gate is None and CONDITIONAL_POLICY == "roll"
                and not formula.effect_lands(caster, caster, CONDITIONAL_CHANCE, rng)):
            out.skipped.append({"op": "attack_rider", "why": "condition not met",
                                "skill": skill_id})
            return
    basis = eff.get("basis") or "atk"
    if basis == "caster_current_hp":
        base = float(caster.hp)
    elif basis == "caster_max_hp":
        base = float(caster.max_hp)
    else:
        base = float(formula.effective_atk(caster))
    if kind == "heal" and basis == "damage_dealt":
        # 攻擊後吸收30%傷害 -- life steal, sized on the damage this skill actually did.
        # The pack's own English for the sibling family 攻擊吸收 is "Life Steal", and
        # its long form spells the mechanic out: 擊傷時最多1次，以25%機率恢復6%體力.
        #
        # Off the STRIKES, not off ATK. A share of "the damage" is what the words say,
        # and the two differ by everything the strike formula does -- crit, attribute
        # advantage, the target's DEF, a shield eating part of it. Riders run after the
        # swing loop, so `out.strikes` already holds this skill's output.
        dealt = sum(st.amount for st in out.strikes)
        amount = int(dealt * pct / 100.0)
        if amount:
            if apply_damage:
                caster.hp = min(caster.max_hp, caster.hp + amount)
            out.heals.append({"target": caster.order, "amount": amount,
                              "from": "lifesteal"})
        return
    if kind == "heal" and eff.get("target") == "allies":
        # 以200%攻擊力恢復我方全體體力 -- the WHOLE party. The rider's generic heal below
        # reaches the caster alone, which was right while every rider heal was a
        # self-heal; it stopped being right once the compiler learned that 我方 without
        # 最低 means all of them. Michael's Gate of Judgement is the case: it healed him
        # for the party's share and left the party on nothing.
        amount = int(base * pct / 100.0)
        for who in [u for u in units if u.team == caster.team and u.alive]:
            if apply_damage:
                who.hp = min(who.max_hp, who.hp + amount)
            out.heals.append({"target": who.order, "amount": amount, "from": "rider"})
        return
    if kind == "heal" and eff.get("target") == "allies_lowest":
        # "以200%的攻擊力回復我方體力最低的2人": ATK-sized, onto the N lowest-HP living
        # allies (the caster included), not the caster alone.
        mates = sorted((u for u in units if u.team == caster.team and u.alive),
                       key=lambda u: u.hp)
        amount = int(base * pct / 100.0)
        for who in mates[:max(1, int(eff.get("count") or 1))]:
            if apply_damage:
                who.hp = min(who.max_hp, who.hp + amount)
            out.heals.append({"target": who.order, "amount": amount, "from": "rider"})
        return
    if kind == "heal":
        # ATK-based unless op 6 said otherwise -- the op-1 prose is "deals N% ATK as
        # damage and recovers the caster's HP" -- read through the same effective_atk as
        # the main heal path, so a buffed caster heals for more in both.
        amount = int(base * pct / 100.0)
        if apply_damage:
            caster.hp = min(caster.max_hp, caster.hp + amount)
        out.heals.append({"target": caster.order, "amount": amount, "from": "rider"})
    elif kind == "bonus_damage" and basis == "target_max_hp":
        # 額外造成敵方最大體力15%的傷害 -- an extra hit sized on the TARGET's pool, not
        # the caster's. 150 attack skills state one and it was compiled as nothing at
        # all, which is why the Guild Weekly boss (Special Sanction, 35% max HP) could
        # not hurt a level-100 party: its only real damage was the clause that vanished.
        #
        # Through `strike`, NOT flat, because the prose says so in as many words:
        # 此傷害會計算防禦與屬性 -- "this damage DOES calculate defence and attribute".
        # That is also what separates it from the caster_*_hp riders above, which the
        # prose never qualifies that way and which stay flat.
        #
        # A RIDER rather than a second `damage` effect, because `execute` runs every
        # damage effect once per SWING: Special Sanction has two, so a damage-op form
        # would pay 35% twice. The rider fires once, at swing 0.
        for tgt in targets:
            amount, detail = formula.strike(caster, tgt, pct / 100.0, "MAX_HP", rng)
            if amount is None:
                continue
            if apply_damage:
                amount, absorbed = _status.absorb(tgt, amount)
                if absorbed:
                    detail["absorbed"] = absorbed
                tgt.hp = max(0, tgt.hp - amount)
            detail["rider"] = True
            out.strikes.append(Strike(swing=0, target=tgt.order, amount=amount,
                                      detail=detail, died=False))
    elif kind == "bonus_damage" and basis != "atk":
        # 額外對目標造成自身8%當前體力的傷害. Not a coefficient on a stat the strike formula
        # knows, so it is dealt as a FLAT amount: no crit, no advantage roll, but it does
        # pass through the target's shield like any other hit. Whether retail mitigated
        # this by DEF is something only footage can settle -- flat is the reading of the
        # words, and it is named here so it can be revisited.
        amount = int(base * pct / 100.0)
        for tgt in targets:
            dealt = amount
            if apply_damage:
                dealt, absorbed = _status.absorb(tgt, dealt)
                tgt.hp = max(0, tgt.hp - dealt)
            out.strikes.append(Strike(swing=0, target=tgt.order, amount=dealt,
                                      detail={"rider": True, "basis": basis,
                                              "crit": False}, died=False))
    elif kind == "bonus_damage":
        for tgt in targets:
            # Same rule as the main damage loop: the rider belongs to this attack, so a
            # target that was alive when it started still takes it.
            amount, detail = formula.strike(caster, tgt, pct / 100.0, "ATK", rng)
            if amount is None:
                continue
            if apply_damage:
                tgt.hp = max(0, tgt.hp - amount)
            detail["rider"] = True
            out.strikes.append(Strike(swing=0, target=tgt.order, amount=amount,
                                      detail=detail, died=False))

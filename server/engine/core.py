"""Target resolution and skill execution. Pure: no JSON, no sockets, no globals.

`execute` returns an `Outcome` describing what happened. It does NOT mutate anything the
caller did not hand it, and it does not know how the client wants any of this shaped --
that is phase 4's job, and keeping the two apart is what makes the wire invariants
assertable in one place.
"""
import dataclasses
import random
from typing import Any, Dict, List, Optional

from . import formula, specs, status as _status

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

def _status_event(caster, target, eff, rng):
    """-> a StatusEvent, or None when a chance-based application misses."""
    st = eff.get("status") or {}
    numbers = eff.get("numbers") or {}
    if eff.get("chance"):
        # op 113 is the chance variant. The pack never states the probability, so the
        # engine uses the effect-accuracy path with a neutral base rather than inventing
        # a per-skill number.
        if not formula.effect_lands(caster, target, 0.75, rng):
            return None
    dur = numbers.get("duration")
    return StatusEvent(
        target=target.order, status_id=st.get("id"), name=st.get("name"),
        applied=True, duration=dur, magnitude=numbers.get("magnitude"),
        stacks=numbers.get("stacks"),
        unknown_duration=dur is None and not numbers.get("permanent"))


def _removable(status_row, category):
    """Whether a cleanse of `category` may strip this status.

    509 statuses declare `(unremovable)` in prose. op 114 removes by CATEGORY BLOCK, so
    without this check a cleanse strips statuses the game says it must not.
    """
    if status_row and status_row.get("unremovable"):
        return False
    return category in (None, "any") or (status_row or {}).get("category") == category


def execute(caster, spec, units, rng=None, chosen=None, depth=0, apply_damage=True):
    """Run one skill. -> Outcome.

    `apply_damage` mutates target HP as it goes, because later swings of a multi-hit
    skill must see the damage the earlier ones did -- a target that died on swing 2 is
    not struck again on swing 3, and the client's `die` flag depends on that ordering.
    Callers wanting a dry run pass False.
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
                amount, detail = formula.strike(
                    caster, tgt, e.get("coefficient"), e.get("basis") or "ATK", r)
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

    # --- everything else, once ------------------------------------------------------
    for e in effects:
        op = e["op"]
        if op == "damage":
            continue
        if op == "apply_status":
            for tgt in targets:
                ev = _status_event(caster, tgt, e, r)
                if ev is None:
                    continue
                # Land it on the unit as STATE, not just on the wire. The unit is shared
                # with the old engine (battle.Unit subclasses Unit), so this is the same
                # list everything else reads.
                if apply_damage and _status.apply_event(tgt, ev, caster) is None:
                    continue          # blocked by an immunity -- do not report it either
                out.statuses.append(ev)
        elif op == "remove_status":
            cat = e.get("category") or (e.get("status") or {}).get("category")
            for tgt in targets:
                for name in _status.remove_category(tgt, cat):
                    out.statuses.append(StatusEvent(
                        target=tgt.order, status_id=None, name=name, applied=False))
        elif op == "heal":
            pct = e.get("percent")
            if pct is None:
                out.skipped.append({"op": op, "why": "magnitude unknown",
                                    "skill": skill_id})
            else:
                for tgt in targets:
                    amount = int(tgt.max_hp * pct / 100.0)
                    if apply_damage:
                        tgt.hp = min(tgt.max_hp, tgt.hp + amount)
                    out.heals.append({"target": tgt.order, "amount": amount})
        elif op == "modify_gauge":
            pct = e.get("percent")
            if pct is None:
                out.skipped.append({"op": op, "why": "magnitude unknown",
                                    "skill": skill_id})
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
            for tgt in targets:
                if not tgt.alive:
                    hp = int(tgt.max_hp * (pct or 0) / 100.0) or 1
                    if apply_damage:
                        tgt.hp = hp
                    out.revives.append({"target": tgt.order, "hp": hp})
        elif op == "attack_rider":
            _rider(caster, e, targets, out, r, apply_damage, skill_id)
        elif op == "follow_up":
            child = specs.skill(e["skill"])
            if child is None:
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
                    depth=depth + 1, apply_damage=apply_damage))
        elif op == "modify_cd":
            pass                                  # cooldown bookkeeping is the caller's
        else:
            out.skipped.append({"op": op, "why": "unhandled", "skill": skill_id})

    # AFTER the riders and follow-ups: those deal damage too, so flagging deaths any
    # earlier would miss a kill that a rider landed.
    _flag_deaths(out, targets)

    # Effects the compiler could not decode at all. Carried, not dropped: a caller that
    # wants to know "did this skill run in full?" must be able to ask.
    for u in spec.get("unknown") or []:
        out.skipped.append({"op": f"raw_{u.get('opcode')}", "why": "undecoded opcode",
                            "skill": skill_id})
    return out


def _flag_deaths(out, targets):
    """Mark `died` on the LAST strike naming each unit that ended the skill dead.

    Exactly one row per unit, which is what the wire requires -- rows carry final state,
    so flagging every strike after the fatal one would replay the death animation on
    every remaining swing.
    """
    dead = {t.order for t in targets if not t.alive}
    if not dead:
        return
    for st in reversed(out.strikes):
        if st.target in dead:
            st.died = True
            dead.discard(st.target)
            if not dead:
                return


def _rider(caster, eff, targets, out, rng, apply_damage, skill_id):
    """op 1 -- the attack rider. Kind comes from prose; see contract doc 6.3.3."""
    kind, pct = eff.get("kind"), eff.get("percent")
    if pct is None:
        out.skipped.append({"op": "attack_rider", "why": "magnitude unknown",
                            "skill": skill_id})
        return
    if kind == "heal":
        amount = int(caster.atk * pct / 100.0)
        if apply_damage:
            caster.hp = min(caster.max_hp, caster.hp + amount)
        out.heals.append({"target": caster.order, "amount": amount, "from": "rider"})
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

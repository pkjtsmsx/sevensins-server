"""Move selection for the enemy turn and for player auto-battle. Tier 1.

See docs/BATTLE_AI_PLAN.md. The short version: instead of ranking skills by a percentage
grepped out of localized prose and hitting whoever comes first in the unit dict, every
legal move is SIMULATED with the real engine and scored by what it actually did.

    choose(battle, target_team) -> (attacker_order, defender_order, skill_id, slot)

One ply, deliberately. A 2-ply search measures at ~240 ms per enemy turn on a real phone
against ~4 ms for this, for gains that do not show: focus fire, lethal detection and not
wasting a heal all come from evaluating one ply HONESTLY, not from searching two.

## The dry run is a preview, not a replay

`core.execute(..., apply_damage=False)` does not mutate, which is what makes this cheap.
But four things then differ from what the move would really do, and every one of them is
silent -- so each is compensated for here rather than trusted:

  * `Strike.died` is ALWAYS False. `_flag_deaths` reads `t.alive`, and HP never dropped.
    Kills are recounted here from the per-target damage sum.
  * shields are not consumed (`status.absorb` is gated), so damage reads pre-shield.
    `_hp_pool` adds the shield back onto the target's effective HP.
  * immunities are not consulted (`status.apply_event` is gated), so a status an immunity
    would have blocked is still reported. `_status_value` checks `is_immune` itself.
  * counters/reflect do not fire (`_damage_hooks` is gated). NOT compensated: it is a
    cost of attacking at all and is near enough symmetric across candidates. Listed so
    nobody later "fixes" it by mutating during evaluation -- that would corrupt the
    battle the scorer is reading.

Two more, independent of the flag: `Strike.amount` includes OVERKILL and `heals[].amount`
includes OVERHEAL, because the engine clamps on application and not in the record. Both
are clamped here. Clamping the heal is the whole reason a healer stops healing a party
that is already at full HP.

## The score is in HP

One currency, so the weights below mean something. Damage and healing are HP already;
a unit's worth is `threat()`, its expected HP output per turn, so removing a turn (a kill,
a stun, a gauge cut) converts into HP at the same rate. Nothing is scored in "points".
"""
import dataclasses
import math
import random

from engine import core as _core
from engine import formula as _formula
from engine import specs as _specs
from engine import status as _status

# Fixed, and the SAME for every candidate: paired seeds mean the crit/variance noise
# cancels in the comparison instead of accumulating. Paired K=4 beats independent K=16,
# which is why this is a tuple of constants and not a sample count.
SAMPLE_SEEDS = (0, 1, 2, 3)

# `resolve_targets` reads `chosen` for exactly one select mode. For every other mode the
# lead target is ignored, so enumerating one candidate per enemy would dry-run the same
# move N times. This is most of the cost saving.
LEAD_MATTERS = {"count"}

# What an unstated duration is worth, mirroring status.DEFAULT_DURATION, and the cap put
# on a permanent one so "lasts all battle" cannot dominate a finite-turn comparison.
DEFAULT_TURNS = _status.DEFAULT_DURATION
PERMANENT_TURNS = 5

# Turn-proximity decay for threat(): a unit acting next is worth its full output, one
# acting four turns out is worth about half. Tier 2 -- free, because `turn_order` is
# already computed each turn and `_roll_turn_order` is exact arithmetic over SPD/scv.
PROXIMITY_DECAY = 0.85
PROXIMITY_ABSENT = 0.5          # dead (a revive target) or otherwise not in the queue

# Stat modifiers, converted into HP. ATK maps onto output directly; DEF and SPD do not,
# so they get a deliberately blunt stand-in rather than a false precision.
STAT_BASE = {
    "ATK": ("threat", 1.0),
    "SPD": ("threat", 0.6),
    "DEF": ("max_hp", 0.25),
    "HP": ("max_hp", 1.0),
}

# Kinds with no magnitude anywhere in the data get a flat fraction of the recipient's
# threat per turn. Small ON PURPOSE: 14,357 of 21,475 apply_status sites state no
# magnitude, and an unquantified status must never outrank a measured kill. Enough to
# prefer doing something over doing nothing, never enough to beat a real outcome.
UNKNOWN_STATUS_VALUE = 0.06
# A cleanse/dispel, valued the same blunt way: we know the direction from the removed
# row's category, not the size.
REMOVAL_VALUE = 0.10
# One turn of a skill's cooldown, as a fraction of the holder's per-turn output.
CD_TURN_VALUE = 0.25


@dataclasses.dataclass(frozen=True)
class Policy:
    """Difficulty is DATA. The algorithm is the same for both sides; these are the knobs.

    `temperature` above 0 makes the pick a softmax over the top `top_k` candidates rather
    than an argmax, so an enemy is competent without being surgical. That is one number
    to tune per stage, and far more legible than hand-writing dumb heuristics -- and it
    stays reproducible, because the rng is seeded from the battle state (see `_pick`).
    """
    name: str
    temperature: float = 0.0
    top_k: int = 3
    # How many future turns a kill is credited with. A kill is worth `threat x horizon`,
    # NOT one turn of threat: killing a unit removes every turn it had left, and at a
    # horizon of 1 chipping a healthy tank for 5,000 outscores finishing a 200-HP enemy
    # -- which is precisely the behaviour tier 1 exists to end. 3 is deliberately
    # conservative against a fight that runs 10-20 turns; `threat` is already discounted
    # by turn proximity on top of this.
    kill_horizon: float = 3.0
    revive_horizon: float = 3.0
    # Opportunity cost of putting a skill on cooldown. Its whole job is to stop the
    # ultimate being dumped on a trash mob at 5% HP; it must stay small enough that it
    # never outweighs a kill.
    cd_cost: float = 0.12


POLICIES = {
    # The player's own auto-battle: play it as well as one ply allows, and
    # deterministically, so a fight replays identically across battle-resume.
    "player_auto": Policy("player_auto", temperature=0.0),
    # The opposition. Mildly imperfect by default -- an enemy that always focus-fires the
    # healer is a worse game, which is the same judgement that keeps 2-ply out.
    "enemy": Policy("enemy", temperature=0.35, cd_cost=0.20),
}


def policy_for(unit):
    return POLICIES["player_auto" if unit.team == _core.TEAM_PLAYER else "enemy"]


# --- the entry point ----------------------------------------------------------------

def choose(battle, target_team, policy=None):
    """-> (attacker_order, defender_order, skill_id, slot), or None if nobody can act.

    Same tuple `Battle.auto_move` always returned, so every caller is unchanged.
    """
    attacker = battle.acting_unit()
    if attacker is None:
        return None
    foes = [u for u in battle.units.values() if u.team == target_team and u.alive]
    if not foes:
        return None
    policy = policy or policy_for(attacker)

    # Taunt/Charm/Confuse FIRST. `attack_cmd_json` applies this override after we have
    # chosen (battle.py, _forced_target), so scoring a move against a target it will
    # never be allowed to hit is silently wrong -- the pick has to be made inside the
    # restriction, not discovered outside it.
    forced = battle._forced_target(attacker)

    units = list(battle.units.values())
    threat = _threat_cache(battle)
    scored = []
    for slot, lead, spec in _candidates(battle, attacker, foes, forced):
        if spec is None:
            # No compiled spec: the new engine cannot run it and `bridge.attack_combo`
            # will fall back to the old path. Keep it as a candidate so a unit whose only
            # skill is uncompiled still acts, but at zero -- anything we can measure wins.
            scored.append((0.0, slot, lead or foes[0].order, attacker.skills[slot], slot))
            continue
        total = 0.0
        resolved = None
        for seed in SAMPLE_SEEDS:
            # A FRESH Random per sample, never a shared stream: a shared one would make
            # the result depend on the order candidates happen to be evaluated in.
            outcome = _core.execute(attacker, spec, units, random.Random(seed),
                                    chosen=lead, apply_damage=False)
            if resolved is None:
                resolved = list(outcome.targets)
            total += _score(battle, attacker, outcome, policy, threat)
        total /= len(SAMPLE_SEEDS)
        total -= policy.cd_cost * attacker.cd_turns(slot) * threat(attacker)
        # Aim the animation at a unit actually hit. For a `count` skill that is the lead;
        # for the rest `chosen` was ignored, and naming targets[0] out of the dict was
        # only ever right by coincidence.
        # `Outcome.targets` is already a list of order keys, not units.
        defender = (resolved[0] if resolved else
                    (forced.order if forced else foes[0].order))
        scored.append((total, slot, defender, attacker.skills[slot], slot))

    if not scored:
        return None
    best = _pick(battle, attacker, scored, policy)
    _score_, slot, defender, skill_id, _slot = best
    return attacker.order, defender, skill_id, slot


def _pick(battle, attacker, scored, policy):
    """-> the chosen candidate row. Stable tie-break, reproducible randomness."""
    # Sort by score, then by (slot, defender) so equal scores always resolve the same
    # way -- a replay and a restored battle must make the same choice.
    ranked = sorted(scored, key=lambda c: (-c[0], c[1], c[2]))
    if policy.temperature <= 0 or len(ranked) == 1:
        return ranked[0]
    top = ranked[:max(1, policy.top_k)]
    # Seeded from the battle state rather than the clock, so a fight that is saved and
    # resumed makes the same "imperfect" choice it made the first time.
    rng = random.Random(f"{battle.round}:{attacker.order}:{len(top)}")
    # Softmax over a scale set by the spread itself; a flat field stays near-uniform and
    # a decisive one stays decisive, without the weights needing a unit.
    spread = max(1.0, abs(ranked[0][0] - ranked[-1][0]))
    weights = [math.exp((c[0] - ranked[0][0]) / (policy.temperature * spread))
               for c in top]
    return rng.choices(top, weights=weights, k=1)[0]


# --- candidates ---------------------------------------------------------------------

def _candidates(battle, attacker, foes, forced):
    """-> [(slot, lead_order or None, spec)] -- every move worth simulating.

    `usable_slots` already owns legality (cooldowns, the ultimate's charge gate, per-slot
    seals) and never returns empty, so this only decides which LEAD targets are distinct.
    """
    out = []
    for slot in battle.usable_slots(attacker):
        if slot >= len(attacker.skills):
            continue
        spec = _specs.skill(attacker.skills[slot])
        if spec is None:
            out.append((slot, None, None))
            continue
        target = spec.get("target") or {}
        if target.get("select") not in LEAD_MATTERS:
            out.append((slot, None, spec))
            continue
        if forced is not None:
            out.append((slot, forced.order, spec))
            continue
        # `_eligible` is the engine's own rule for who MAY be hit; re-deriving it here
        # from the group name would be a second copy of it, and the two would drift.
        pool = [u for u in battle.units.values()
                if u.alive and _core._eligible(attacker, u, target.get("group"))]
        if not pool:
            out.append((slot, None, spec))
            continue
        # A skill that hits at least as many as there are targets resolves to the same
        # set whichever lead it is given, so it is ONE candidate, not len(pool) of them.
        if (target.get("count") or 1) >= len(pool):
            out.append((slot, pool[0].order, spec))
            continue
        for u in pool:
            out.append((slot, u.order, spec))
    return out


# --- scoring ------------------------------------------------------------------------

def _score(battle, attacker, outcome, policy, threat):
    """-> what this outcome is worth to `attacker`, in HP."""
    units = battle.units
    score = 0.0

    # Damage, summed per target across every swing, follow-up and rider before being
    # clamped -- clamping per strike would credit overkill on each swing separately.
    dealt = {}
    for st in _flat(outcome, "strikes"):
        dealt[st.target] = dealt.get(st.target, 0) + st.amount
    for order, amount in dealt.items():
        u = units.get(order)
        if u is None or not u.alive:
            continue
        pool = _hp_pool(u)
        side = _side(attacker, u)
        # Damage is only worth anything insofar as it leads to a KILL, so it is credited
        # twice: the HP itself, plus the share of the target's remaining pool it removed,
        # cashed at what killing that target is worth. At `amount >= pool` the share is 1
        # and the whole kill is paid -- so this subsumes a discrete kill bonus rather
        # than sitting beside it.
        #
        # Making it continuous is what produces FOCUS FIRE, and a discrete bonus does
        # not: the same 2,000 damage removes a tenth of a fresh target's pool and all of
        # a nearly-dead one's, so the finisher wins without needing a special case. The
        # first cut of this scored damage at face value, and a status the primary target
        # already held was then enough to push the next attack onto a fresh enemy --
        # spreading damage across the field, which is precisely the behaviour tier 1
        # exists to end.
        share = min(1.0, amount / pool) if pool > 0 else 1.0
        score += side * (min(amount, pool) + share * policy.kill_horizon * threat(u))

    for h in _flat(outcome, "heals"):
        u = units.get(h["target"])
        if u is None:
            continue
        score += _side(attacker, u, ally_is_good=True) * min(
            int(h.get("amount") or 0), max(0, u.max_hp - u.hp))

    for rv in _flat(outcome, "revives"):
        u = units.get(rv["target"])
        if u is None:
            continue
        # The engine's revive pool is already filtered to the dead, so an empty list is
        # what stops a revive being cast with nobody down -- it simply scores nothing.
        score += _side(attacker, u, ally_is_good=True) * (
            int(rv.get("hp") or 0) + policy.revive_horizon * threat(u))

    for ev in _flat(outcome, "statuses"):
        score += _status_value(battle, attacker, ev, threat)

    for g in _flat(outcome, "gauge"):
        u = units.get(g["target"])
        pct = g.get("percent")
        if u is None or pct is None:
            continue
        # A full gauge IS a turn, so a gauge move converts to HP through threat directly.
        score += _side(attacker, u, ally_is_good=True) * (float(pct) / 100.0) * threat(u)

    for c in _flat(outcome, "cooldowns"):
        u = units.get(c["target"])
        if u is None:
            continue
        # Signed: negative refreshes (good for the holder), positive delays.
        score += _side(attacker, u, ally_is_good=True) * (
            -float(c["turns"]) * CD_TURN_VALUE * threat(u))

    return score


def _status_value(battle, attacker, ev, threat):
    """-> the HP value to `attacker` of one StatusEvent, or 0 when it cannot be judged."""
    u = battle.units.get(ev.target)
    if u is None:
        return 0.0
    row = _specs.status(ev.status_id) or {}

    if not ev.applied:
        # A removal. The direction comes from what was stripped: taking a buff off an
        # enemy and a debuff off an ally are both good, and we know that much even
        # without a magnitude.
        category = (row.get("category") or "").lower()
        if category in ("buff", "stat_up", "heal_over_time", "shield"):
            good_for_holder = -1.0
        elif category in ("debuff", "damage_over_time"):
            good_for_holder = 1.0
        else:
            return 0.0
        return _side(attacker, u, ally_is_good=True) * good_for_holder * \
            REMOVAL_VALUE * threat(u)

    # The dry run never consulted the immunity, so a blocked status is still reported.
    if _status.is_immune(u, row):
        return 0.0
    # Redundancy: already held, and either not stackable or already at the cap. This is
    # what stops Freeze landing on the frozen while a second enemy stands untouched.
    existing = next((s for s in _status._actives(u) if s.status_id == ev.status_id), None)
    if existing is not None:
        cap = row.get("stack_cap")
        if not cap or existing.stacks >= int(cap):
            return 0.0

    turns = (PERMANENT_TURNS if ev.permanent
             else int(ev.duration) if ev.duration is not None else DEFAULT_TURNS)
    kind = (row.get("kind") or "").lower()
    mag = ev.magnitude
    stacks = max(1, int(ev.stacks or 1))
    # Value TO THE HOLDER: positive means the status is good for whoever now has it.
    value = None

    if kind == "control":
        # The most valuable debuff in the game and previously invisible. Only the ones
        # that actually stop a turn are worth a whole turn -- the registry files Taunt and
        # Charm as `control` too, and those redirect an action rather than deleting it.
        name = (ev.name or row.get("name") or "").lower()
        skips = any(w in name for w in _status.SKIPS_TURN_NAMES)
        value = -threat(u) * turns * (1.0 if skips else 0.4)
    elif kind == "dot":
        if mag is not None:
            # status.tick_damage: the inflicter's ATK, bypassing DEF.
            value = -float(attacker.atk) * float(mag) / 100.0 * stacks * turns
    elif kind == "heal":
        if mag is not None:
            value = float(attacker.atk) * float(mag) / 100.0 * stacks * turns
    elif kind == "shield":
        if mag is not None:
            value = float(mag) * stacks          # already HP
    elif kind == "block_heal":
        value = -UNKNOWN_STATUS_VALUE * threat(u) * turns
    elif kind == "stat_mod":
        signed = _signed(mag, row, stacks)
        if signed is not None:
            source, weight = STAT_BASE.get((row.get("stat") or "").upper(), (None, 0.0))
            base = threat(u) if source == "threat" else \
                float(u.max_hp) if source == "max_hp" else None
            if base is not None:
                value = base * weight * signed / 100.0 * turns
    elif kind == "damage_mod":
        signed = _signed(mag, row, stacks)
        if signed is not None:
            value = threat(u) * signed / 100.0 * turns
    elif kind in ("gauge", "immunity"):
        value = _category_sign(row) * UNKNOWN_STATUS_VALUE * threat(u) * turns
    # `other` and None -- 752 of the 1,685 rows -- fall through at zero, deliberately.
    # An unquantified status must not be able to outrank something we measured.

    if value is None:
        # Kind known but the magnitude is not, which is two thirds of all sites. Fall
        # back to the direction alone, small.
        value = _category_sign(row) * UNKNOWN_STATUS_VALUE * threat(u) * turns
    return _side(attacker, u, ally_is_good=True) * value


def _signed(magnitude, row, stacks):
    """The magnitude with its direction, or None. Mirrors `status.Active.signed_magnitude`
    -- an explicit sign would win, but a StatusEvent does not carry one, so the id-block
    category decides for a named stat and everything else stays unknown. Guessing here
    would silently invert "Damage taken +4%", which is a debuff with a positive number.
    """
    if magnitude is None:
        return None
    category = row.get("category")
    if not row.get("stat"):
        return None
    if category in _status.LOWERING:
        direction = -1
    elif category in _status.RAISING:
        direction = 1
    else:
        return None
    return direction * float(magnitude) * max(1, int(stacks))


def _category_sign(row):
    """-> +1 if the row is good for its holder, -1 if bad, 0 when the data does not say."""
    category = (row.get("category") or "").lower()
    if category in ("buff", "stat_up", "heal_over_time", "shield", "passive_grant"):
        return 1.0
    if category in ("debuff", "damage_over_time"):
        return -1.0
    return 0.0


def _side(attacker, unit, ally_is_good=False):
    """-> +1 when what is happening to `unit` is good for `attacker`, else -1.

    `ally_is_good` flips the sense: damage to an ally is bad, a heal on one is good, and
    a Charmed attacker really can do either.
    """
    same = unit.team == attacker.team
    return (1.0 if same else -1.0) if ally_is_good else (-1.0 if same else 1.0)


def _hp_pool(unit):
    """Effective HP: the bar plus any shield. The dry run does not spend shields, so
    without this a shielded target reads as killable when it is not."""
    shield = sum(max(0, s.shield_hp) for s in _status._actives(unit))
    return max(0, int(unit.hp) + shield)


# --- threat -------------------------------------------------------------------------

def _threat_cache(battle):
    """-> threat(unit), memoized for one decision."""
    cache = {}
    position = {order: i for i, order in enumerate(battle.turn_order)}

    def threat(unit):
        key = unit.order
        if key not in cache:
            cache[key] = _raw_threat(unit) * _proximity(position, unit)
        return cache[key]
    return threat


def _proximity(position, unit):
    """How soon this unit acts, as a discount. Tier 2, and free: `turn_order` is already
    computed every turn and `_roll_turn_order` is exact arithmetic over SPD and scv."""
    i = position.get(unit.order)
    if i is None:
        return PROXIMITY_ABSENT              # dead, or not in the queue (revive targets)
    return PROXIMITY_DECAY ** i


def _raw_threat(unit):
    """A unit's HP output per turn: its effective ATK times its best COMPILED coefficient.

    Compiled, never prose -- that is the whole point of the exercise. MAX_HP/DEF-basis
    coefficients are skipped rather than mixed in: they multiply a stat this scale is not
    in, and pretending otherwise would make a 20%-of-max-HP skill look feeble.
    """
    best = 0.0
    for skill_id in getattr(unit, "skills", []) or []:
        spec = _specs.skill(skill_id)
        if spec is None:
            continue
        swings = max(1, int(spec.get("swings") or 1))
        for e in spec.get("effects") or []:
            if e.get("op") != "damage" or e.get("coefficient") is None:
                continue
            if (e.get("basis") or "ATK") != "ATK":
                continue
            best = max(best, float(e["coefficient"]) * swings)
    if best <= 0:
        best = 1.0                            # a unit with no readable damage still acts
    return _formula.effective_atk(unit) * best


# --- plumbing -----------------------------------------------------------------------

def _flat(outcome, attr):
    """One list from an Outcome and every follow-up under it. A follow-up's damage is
    still this move's damage."""
    out = list(getattr(outcome, attr))
    for child in outcome.children:
        out.extend(_flat(child, attr))
    return out

"""Active status state: what is on a unit, how long, and what it does.

Phase 6. Until now the engine applied statuses to the wire and nothing tracked them --
the icon appeared, the duration counted down client-side, and a Freeze did not stop
anyone acting. This is the state and the rules that read it.

## Where each field comes from

Two sources, and the split matters because one is exact and one is not:

  * **the status registry** (`statuses.json`, built from the id-block taxonomy and the
    row's own prose): `kind`, `category`, `stack_cap`, `unremovable`. The category comes
    from a COLUMN -- the thousands block of the status id -- and is the reliable half.
  * **the applying skill** (`numbers` on each `apply_status` effect): `duration`,
    `magnitude`, `sign`, `stat`. Prose-derived, present on 53% and 32% of sites
    respectively, and explicitly `None` when unstated rather than defaulted.

## The sign rule, which is not what it looks like

A magnitude's sign is NOT "buff or debuff". Measured over the 3,849 sites that state one
explicitly, it agrees with the id-block category 3,508 times and disagrees 341 -- and
every disagreement is real: `Aging` is a debuff whose magnitude reads "Damage taken
**+4%**". A positive number that is bad for you.

So the sign describes the direction of the QUANTITY, not whether the status is good:

  1. an explicit prose sign always wins;
  2. otherwise, for a stat modifier, the category decides -- a debuff lowers the stat and
     a buff raises it, which is unambiguous for ATK/DEF/SPD/HP;
  3. otherwise the direction is UNKNOWN and the modifier is not applied. "Damage taken"
     and "damage dealt" are the same kind with opposite meanings, so guessing there would
     silently invert an effect.

Rule 3 is why `signed_magnitude` returns None rather than a number it cannot justify.
"""
import dataclasses
import re
from typing import List, Optional

from . import specs

# Categories that unambiguously lower the stat they name, and those that raise it.
# From the id block, so this is a column reading rather than an inference.
LOWERING = {"debuff"}
RAISING = {"buff", "stat_up", "heal_over_time"}

# Kinds whose holder cannot act. `control` covers Stun/Freeze/Daze/Charm/Taunt in the
# registry's classification; only the ones that actually stop a turn are listed by name,
# because Taunt and Charm redirect a turn rather than skipping it.
SKIPS_TURN_NAMES = {
    "daze", "stun", "freeze", "petrify", "immobilize", "sleep", "faint",
}


@dataclasses.dataclass
class Active:
    """One status instance on one unit.

    `source_atk` is snapshotted at apply time so a damage-over-time keeps ticking for the
    inflicter's power even after the inflicter's own buffs expire, or after it dies.
    """
    status_id: int
    name: Optional[str]
    kind: Optional[str]
    category: Optional[str]
    stat: Optional[str] = None
    remaining: Optional[int] = None          # None = lasts the whole battle
    stacks: int = 1
    magnitude: Optional[float] = None
    sign: Optional[int] = None
    stack_cap: Optional[int] = None
    unremovable: bool = False
    source_atk: Optional[int] = None
    shield_hp: int = 0

    @property
    def permanent(self):
        return self.remaining is None

    def signed_magnitude(self):
        """-> the magnitude with its direction, or None when that cannot be justified.

        See the module docstring: an explicit sign wins, else the category decides for a
        named stat, else unknown. Scaled by `stacks`, since a stackable status's whole
        point is that N applications hit N times as hard.
        """
        if self.magnitude is None:
            return None
        if self.sign is not None:
            direction = self.sign
        elif self.stat and self.category in LOWERING:
            direction = -1
        elif self.stat and self.category in RAISING:
            direction = 1
        else:
            return None
        return direction * float(self.magnitude) * max(1, int(self.stacks))


# What an UNSTATED duration becomes.
#
# It must not become permanent. Both branches of the old expression collapsed to None
# when the duration was unknown, and None means "lasts the whole battle" -- so the raid
# boss's Power Attack Seal, whose duration the pack never states, sealed the party's
# power attacks for the entire fight. Only an explicit "until the end of battle" in the
# prose should be permanent.
#
# 2 turns is the modal duration across the corpus and a deliberately conservative guess:
# a status that expires too early is a fidelity loss, one that never expires is a broken
# fight.
DEFAULT_DURATION = 2


def _remaining_for(event):
    """-> turns remaining for a new Active: stated, permanent, or the safe default."""
    if event.duration is not None:
        return int(event.duration)
    if getattr(event, "permanent", False):
        return None                      # the prose said "until the end of battle"
    return DEFAULT_DURATION


def _registry(status_id):
    try:
        return specs.status(int(status_id)) or {}
    except Exception:                                        # noqa: BLE001
        return {}


def apply_event(unit, event, caster=None):
    """Put a StatusEvent onto a unit. -> the Active, or None if it was rejected.

    Stacking: a status already present is REFRESHED (the longer duration wins, which is
    how a re-application should feel) and its stack count rises up to `stack_cap`. A
    non-stackable status just refreshes. This is the behaviour the `(N)` suffix in the
    names implies -- `Spirit(5)` caps at five.
    """
    row = _registry(event.status_id)
    if is_immune(unit, row):
        return None

    existing = next((s for s in unit.statuses
                     if isinstance(s, Active) and s.status_id == event.status_id), None)
    cap = row.get("stack_cap")
    if existing is not None:
        if cap:
            existing.stacks = min(int(cap), existing.stacks + 1)
        if event.duration is not None:
            existing.remaining = (event.duration if existing.remaining is None
                                  else max(existing.remaining, event.duration))
        return existing

    active = Active(
        status_id=int(event.status_id), name=event.name or row.get("name"),
        kind=row.get("kind"), category=row.get("category"), stat=row.get("stat"),
        remaining=_remaining_for(event),
        magnitude=event.magnitude, stack_cap=cap,
        unremovable=bool(row.get("unremovable")),
        source_atk=int(getattr(caster, "atk", 0) or 0) if caster is not None else None,
    )
    unit.statuses.append(active)
    return active


def is_immune(unit, row):
    """Does an existing immunity block this application?

    Deliberately narrow: only a status whose own registry `kind` is `immunity` and whose
    name names the incoming category blocks it. A broad "any immunity blocks anything"
    rule would make CC Immunity block buffs.
    """
    incoming = (row.get("category") or "").lower()
    incoming_kind = (row.get("kind") or "").lower()
    if incoming in ("buff", "stat_up", "heal_over_time", "shield"):
        return False
    for st in unit.statuses:
        if not isinstance(st, Active) or st.kind != "immunity":
            continue
        name = (st.name or "").lower()
        if "cc" in name or "crowd" in name:
            if incoming_kind == "control":
                return True
        if incoming and incoming in name:
            return True
    return False


# Damage and duration are deliberately SEPARATE calls, and the order matters.
#
# A turn goes: tick_damage -> decide whether the unit can act -> tick_duration. Doing
# both in one call at the start of a turn means a 1-turn Stun expires on the same tick
# that should have skipped the turn, so it never stops anybody -- which is exactly what
# happened, and what the new-path suite caught. The old engine had the same split for the
# same reason.


def tick_damage(unit):
    """DoT/HoT for the START of this unit's turn. -> (dot, hot).

    Every DoT/HoT in the pack is worded "when a turn starts, deals/restores...", so this
    is when the damage lands.

    The amount uses the INFLICTER's ATK, snapshotted at apply time, and bypasses DEF --
    the prose says "deal N% of the effect owner's ATK as damage" with no mitigation
    clause, and the old engine read it the same way.
    """
    dot = hot = 0
    for st in _actives(unit):
        if st.kind not in ("dot", "heal") or st.magnitude is None:
            continue
        base = st.source_atk if st.source_atk else getattr(unit, "atk", 0)
        amount = int(base * float(st.magnitude) / 100.0) * max(1, st.stacks)
        if st.kind == "dot":
            dot += amount
        else:
            hot += amount
    return dot, hot


def tick_duration(unit):
    """Spend one of this unit's turns off every status it holds. -> expired names.

    Called once the turn is resolved -- including a turn that was SKIPPED, since a
    skipped turn still counts against a duration or a stun would never wear off.
    """
    expired = []
    for st in list(unit.statuses):
        if not isinstance(st, Active) or st.permanent:
            continue
        st.remaining = int(st.remaining) - 1
        if st.remaining <= 0:
            unit.statuses.remove(st)
            expired.append(st.name)
    return expired


def tick(unit):
    """Both halves, for callers outside a turn loop. -> (dot, hot, expired)."""
    dot, hot = tick_damage(unit)
    return dot, hot, tick_duration(unit)


def remove_category(unit, category):
    """Cleanse. -> the names removed.

    `unremovable` is honoured: 509 statuses declare in prose that a cleanse cannot touch
    them, and op 114 removes by CATEGORY BLOCK, so without the check a cleanse would
    strip statuses the game says it must not.
    """
    gone = []
    for st in list(unit.statuses):
        if not isinstance(st, Active) or st.unremovable:
            continue
        if category in (None, "any") or st.category == category:
            unit.statuses.remove(st)
            gone.append(st.name)
    return gone


# --- statuses whose behaviour is not derivable ------------------------------------
#
# Same problem as passives, same answer: a small table rather than special cases. The
# registry says Return is `kind: other` -- nothing in the data says it reflects. Its
# prose does: "While taking damage, deals Target's 100% ATK as damage (triggers once
# while dealing multiple attacks)."
#
# Keyed by a lowercase name fragment, matched loosely as elsewhere. A reflect is a
# percentage of the ATTACKER's ATK, paid back to the attacker.
REFLECT = {
    "return": 100.0,
}

# "triggers once while dealing multiple attacks" -- a multi-hit skill reflects ONE time,
# not once per swing, or a 4-hit skill would pay four times.
REFLECT_ONCE_PER_SKILL = True


# Statuses that stop the move gauge moving, derived from the registry's own wording
# rather than a hand list: `Headwind` says "Move Gauge will not increase" and `Steady`
# "will not decrease". Both are kind `gauge`, so the KIND cannot tell them apart -- and
# there are 36 gauge statuses, most of which do neither.
_GAUGE_STOP_GAIN = re.compile(
    r"move gauge[^.]{0,40}?(will not|cannot|can\'t)\s*(increase|rise|fill)"
    r"|(cannot|can\'t|unable to)\s*increase[^.]{0,20}move gauge"
    r"|disables? (the )?move gauge", re.I)
_GAUGE_STOP_LOSS = re.compile(
    r"move gauge[^.]{0,40}?(will not|cannot|can\'t)\s*(decrease|drop|reduce)"
    r"|immun\w*[^.]{0,40}move gauge (reduction|decrease)", re.I)

_gauge_block_cache = None


def _gauge_block_ids():
    """-> (ids that stop the gauge RISING, ids that stop it FALLING)."""
    global _gauge_block_cache
    if _gauge_block_cache is not None:
        return _gauge_block_cache
    gain, loss = set(), set()
    try:
        for sid, row in (specs.statuses() or {}).items():
            text = " ".join(str(row.get(k) or "") for k in ("description", "name"))
            if _GAUGE_STOP_GAIN.search(text):
                gain.add(int(sid))
            if _GAUGE_STOP_LOSS.search(text):
                loss.add(int(sid))
    except Exception:                                        # noqa: BLE001
        pass
    _gauge_block_cache = (gain, loss)
    return _gauge_block_cache


def blocks_gauge_gain(unit):
    """Headwind and friends: this unit's move gauge does not fill."""
    ids, _ = _gauge_block_ids()
    return any(st.status_id in ids for st in _actives(unit))


def blocks_gauge_loss(unit):
    """Steady and friends: this unit's move gauge cannot be reduced."""
    _, ids = _gauge_block_ids()
    return any(st.status_id in ids for st in _actives(unit))


def reflect_amount(victim, attacker):
    """-> damage the victim's statuses pay back to `attacker`, or 0."""
    if attacker is None:
        return 0
    total = 0.0
    for st in _actives(victim):
        name = (st.name or "").lower()
        for frag, pct in REFLECT.items():
            if frag in name:
                total += float(getattr(attacker, "atk", 0) or 0) * pct / 100.0
                break
    return int(total)


# --- the reads -------------------------------------------------------------------

def _actives(unit):
    return [s for s in getattr(unit, "statuses", []) if isinstance(s, Active)]


def stat_multiplier(unit, stat):
    """-> a multiplier for ATK/DEF/SPD/HP from the unit's active statuses.

    Percentages stack ADDITIVELY, not multiplicatively: two ATK-35% give x0.30, which is
    what "stacks up to N times" reads as and what the old engine did. Floored at 0 so a
    stack of debuffs cannot invert the stat.
    """
    total = 0.0
    for st in _actives(unit):
        if st.kind != "stat_mod" or (st.stat or "").upper() != stat.upper():
            continue
        m = st.signed_magnitude()
        if m is not None:
            total += m
    return max(0.0, 1.0 + total / 100.0)


def damage_dealt_multiplier(unit):
    """Statuses that change how hard this unit HITS."""
    return _damage_mult(unit, taken=False)


def damage_taken_multiplier(unit):
    """Statuses that change how hard this unit is HIT."""
    return _damage_mult(unit, taken=True)


def _damage_mult(unit, taken):
    total = 0.0
    for st in _actives(unit):
        if st.kind != "damage_mod":
            continue
        m = st.signed_magnitude()
        if m is None:
            continue
        # "Damage taken" and "damage dealt" are the same kind with opposite subjects, and
        # only the name distinguishes them. A status that names neither is skipped rather
        # than guessed at -- applying a dealt-modifier as a taken-modifier would invert it.
        name = (st.name or "").lower()
        is_taken = "taken" in name or "reduction" in name or "受" in name
        if is_taken != taken:
            continue
        total += m
    return max(0.0, 1.0 + total / 100.0)


def is_immobilized(unit):
    """Does this unit skip its turn entirely?

    Name-based rather than kind-based on purpose: the registry files Taunt and Charm as
    `control` too, but those REDIRECT a turn rather than skipping it, and treating them
    as a skip would silently delete a unit's action.
    """
    for st in _actives(unit):
        if st.kind != "control":
            continue
        name = (st.name or "").lower()
        if any(word in name for word in SKIPS_TURN_NAMES):
            return True
    return False


def absorb(unit, amount):
    """Run incoming damage through any shields. -> (damage that lands, absorbed)."""
    left = int(amount)
    used = 0
    for st in _actives(unit):
        if st.kind != "shield" or st.shield_hp <= 0 or left <= 0:
            continue
        take = min(st.shield_hp, left)
        st.shield_hp -= take
        left -= take
        used += take
    return left, used

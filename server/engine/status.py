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
    # `damage_mod` only: "taken" / "dealt" / None. Which side of the exchange this
    # status's magnitude applies to -- see _damage_mult, which had to guess it from the
    # English NAME and was wrong for 84 of the 148 damage_mod statuses.
    subject: Optional[str] = None
    remaining: Optional[int] = None          # None = lasts the whole battle
    stacks: int = 1
    magnitude: Optional[float] = None
    sign: Optional[int] = None
    stack_cap: Optional[int] = None
    unremovable: bool = False
    source_atk: Optional[int] = None
    # WHO inflicted it. Needed by Taunt, which redirects to "the taunt caster" -- a
    # magnitude cannot express that. Defaults to None so an older save restores cleanly.
    source_order: Optional[str] = None
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


def wire_status_id(status_id):
    """-> the id to send the client, or None when it must not be sent.

    The client feeds every status id straight into `DesignSkillForm.GetRow(id)`
    (StatusST's constructors do it to read the row's `_statusID`), and that call THROWS
    on a miss rather than returning null. On the battle-open channel it throws inside
    BattleDataInitializer._InitUI, which kills the coroutine and leaves the loading
    screen up forever:

        DesignException: (0x0012) DesignSkillForm row ID -175492 not found
          at Game.Player.Battle.StatusST..ctor (Int32 _skillID, List`1 serverArgs)
          at Game.Player.Char.BattleDatas.RebuildAllStatus

    -175492 is one of OURS. A passive rule that names a status with no design row of its
    own gets a synthesised negative id minted from the name (see passives.py), which is
    exactly right for tracking it on the unit and fatal to hand to the client. Requiring
    a registry hit -- not merely a positive number -- also covers a status row id that
    simply is not in the pack.
    """
    try:
        sid = int(status_id)
    except (TypeError, ValueError):
        return None
    return sid if sid > 0 and _registry(sid) else None


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
        subject=row.get("subject"),
        remaining=_remaining_for(event),
        magnitude=event.magnitude, sign=_sign_for(event, row), stack_cap=cap,
        unremovable=bool(row.get("unremovable")),
        source_atk=int(getattr(caster, "atk", 0) or 0) if caster is not None else None,
        source_order=getattr(caster, "order", None) if caster is not None else None,
    )
    if active.kind == "shield":
        active.shield_hp = shield_size(unit, event, caster)
    unit.statuses.append(active)
    return active


def _sign_for(event, row):
    """-> the direction for this magnitude, or None to let the category decide.

    `tools/compile_skills.py` has always emitted `numbers.magnitude_sign` from the
    prose's own +/- and NOTHING in the server read it -- a grep for the key across
    `server/` returned a single test. It matters because `Active.signed_magnitude`
    otherwise falls back to the status's CATEGORY, and a category outside buff/debuff
    leaves the direction unknown and the magnitude unusable however well it parsed. All
    nine CRI stat_mods are `category: passive_grant`, so every Critical Surge / Execute
    Critical / Critical Injection in the game was inert even once its number arrived.

    **DELIBERATELY LIMITED TO `stat_mod`**, and the reason is the thing this nearly got
    wrong. Wiring the sign through for every kind changes 2,181 effects, because for
    697 of them the prose sign DISAGREES with what the category decided -- and the
    prose is right:

        Fragile      (category debuff) 受到的傷害承受量提高25%  -- the number goes UP
        Determination(category buff)   降低自己受到的傷害5%     -- the number goes DOWN

    The category answers "is this good for the holder", which is the OPPOSITE of "is
    the number up" for the whole damage-taken family (503 of the 697 are `damage_mod`).
    So those disagreements are real bugs and the sign fixes them -- but they move damage
    numbers across the entire game and want their own change, their own measurement and
    a device. `stat_mod` cannot have that conflation: the status IS the stat change, so
    the prose's sign and the magnitude's direction are the same question.
    """
    sign = getattr(event, "sign", None)
    if sign is None or (row or {}).get("kind") not in ("stat_mod", "damage_mod"):
        return None
    return int(sign)


def shield_size(unit, event, caster=None):
    """-> the HP a freshly applied shield absorbs before it breaks.

    NEVER COMPUTED BEFORE 2026-08-26. `absorb` was in place and `shield_hp` was a field
    on Active, but nothing set it on apply, so all 525 shield applications in the
    compiled cast skills started at 0 and absorbed nothing -- Life Shield, Field
    Shield, every 金剛 and 護盾 in the game were icons.

    Three sizes, all from the status's own Chinese line (status_prose.parse_zh):
    a flat amount ("7500點"), a percentage of the CASTER's ATK ("75%攻擊力"), or a
    percentage of a max HP -- the caster's ("施術者最大體力30%") or the holder's
    ("自身最大體力70%"). A shield whose line states none of these stays 0, visibly,
    rather than being given a made-up size.
    """
    flat = getattr(event, "flat", None)
    if flat:
        return int(flat)
    mag = getattr(event, "magnitude", None)
    if mag is None:
        return 0
    basis = getattr(event, "basis", None) or "max_hp"
    if basis == "atk":
        base = float(getattr(caster, "atk", 0) or 0) if caster is not None else 0.0
    elif basis == "caster_max_hp":
        base = float(getattr(caster, "max_hp", 0) or 0) if caster is not None else 0.0
    else:
        base = float(getattr(unit, "max_hp", 0) or 0)
    return int(base * float(mag) / 100.0)


# What each immunity status blocks, derived once from its registry row.
#
# THE BUG THIS REPLACES: is_immune compared the immunity's NAME against the incoming
# status's CATEGORY -- and every control status is category `misc`, so "misc" was looked
# for inside "daze immunity", never found, and the immunity was inert. Found on a phone
# 2026-08-26: the Guild Weekly boss opens with `Daze Immunity` and was dazed anyway.
# Only the `CC Immunity` family ever worked, through its special-cased "cc" rule. At
# that point the registry held 82 named immunities applied by 1,082 compiled effects
# (977 of them passives), all decoration.
#
# Subjects come from BOTH the name and the description, because the registry spells
# immunities two ways: 80 are named for their subject ("Freeze Immunity",
# "Charm/Confuse/Headwind Immunity") and 38 say it only in prose ("Undying Hellfire":
# "Gains immunity to Freeze effect", "Antibody": "immunity to all crowd control").
# "All control/CC/immobilization" collapses to the KIND rule the CC family already
# used; "all DoT" likewise. -> (frozenset of name stems, frozenset of blocked kinds).
_IMM_SUBJECTS = {}

# "Gains immunity to Enchant, Charm and Femme Fatale." / "immune to Absolute Zero (SP)
# and Freeze for four turns" -- the subject list follows "immun* to". The first cut of
# this parser instead captured whatever PRECEDED "immun", which turned Power Glove's
# "all allies are immune" into the stems {"all allies are", "in battle"}.
# The capture must run THROUGH a parenthetical, not stop at it -- "immune to Absolute
# Zero (SP) and Freeze for four turns" names two statuses, and stopping at `(` kept
# only the first. Qualifiers like (SP) are stripped per-part by _add instead.
_IMM_FROM_DESC = re.compile(
    r"immun\w*\s+to\s+(.+?)(?:\s+effects?\b|\s+for\s|[.。]|$)", re.I)
_IMM_ALL_KINDS = (
    (re.compile(r"all (?:crowd.control|control|cc|immobilization)", re.I), "control"),
    (re.compile(r"all DoT|all damage.over.time", re.I), "dot"),
)
_IMM_QUALIFIER = re.compile(r"\s*[(（][^)）]*[)）]\s*|\s+(?:UL|SP|[IVX]+)\s*$")
_IMM_NAME_PREFIX = re.compile(r"^(.*?)\s*immunity\b|[(（]\s*(.*?)\s*immunity\s*[)）]",
                              re.I)


def _immunity_subjects(status_id, name):
    key = int(status_id or 0)
    if key in _IMM_SUBJECTS:
        return _IMM_SUBJECTS[key]
    row = _registry(key)
    name = name or row.get("name") or ""
    desc = row.get("description") or ""
    stems, kinds = set(), set()
    for rx, kind in _IMM_ALL_KINDS:
        if rx.search(name) or rx.search(desc):
            kinds.add(kind)
    if re.search(r"\bcc\b|crowd", name.lower()):
        kinds.add("control")

    def _add(listing):
        for part in re.split(r"[/,]| and ", listing or ""):
            part = _IMM_QUALIFIER.sub("", part.strip().strip(".")).strip().lower()
            if part and part not in ("all", "the", "a", "an"):
                stems.add(part)

    # From the NAME: "Charm/Confuse/Headwind Immunity", "Kneel Down!(Charm Immunity)".
    m = _IMM_NAME_PREFIX.search(name)
    if m:
        _add(m.group(1) or m.group(2))
    # From the DESCRIPTION: "Gains immunity to Confuse and Deteriorate." -- 38 of the
    # registry's immunities state their subject only here.
    for m in _IMM_FROM_DESC.finditer(desc):
        _add(m.group(1))
    got = (frozenset(stems), frozenset(kinds))
    _IMM_SUBJECTS[key] = got
    return got


def is_immune(unit, row):
    """Does an existing immunity block this application?

    An immunity blocks the statuses it NAMES (loose match either way, qualifiers
    stripped -- `Freeze Immunity` blocks `Freeze UL`) and the KINDS it claims wholesale
    ("all control effects"). Still deliberately narrow the other way: a buff is never
    blocked, so a mis-parsed subject cannot turn an immunity into a buff-eater.
    """
    incoming_kind = (row.get("kind") or "").lower()
    incoming_cat = (row.get("category") or "").lower()
    incoming_name = _IMM_QUALIFIER.sub("", (row.get("name") or "").lower()).strip()
    helpful = incoming_cat in ("buff", "stat_up", "heal_over_time", "shield")
    for st in unit.statuses:
        if not isinstance(st, Active) or st.kind != "immunity":
            continue
        stems, kinds = _immunity_subjects(st.status_id, st.name)
        # An EXPLICIT name match wins even over a "helpful" category, because the
        # category is itself prose-derived and sometimes wrong -- Headwind, a
        # gauge-block debuff, is category `shield` in the registry, and the guard
        # below would have let it through a literal "Headwind Immunity". Naming the
        # status is the strongest evidence there is about what the immunity means.
        for stem in stems:
            if incoming_name and (stem in incoming_name or incoming_name in stem):
                return True
        # The WHOLESALE rules ("all control", CC) stay behind the guard: they are
        # derived, and a derived rule must never turn an immunity into a buff-eater.
        if not helpful and incoming_kind in kinds:
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
        base = st.source_atk if st.source_atk else getattr(unit, "atk", 0)
        # 凍結 is classified `control`, so it never reached the DoT branch even though
        # its Chinese says 受到持續性的傷害. Its size is a house number (CONTROL_DOT);
        # everything else about it -- the ATK basis, the source snapshot, the stacking --
        # is the same machinery every other DoT uses.
        house = CONTROL_DOT.get(st.status_id)
        if house:
            dot += int(base * house / 100.0) * max(1, st.stacks)
            continue
        if st.kind not in ("dot", "heal") or st.magnitude is None:
            continue
        amount = int(base * float(st.magnitude) / 100.0) * max(1, st.stacks)
        if st.kind == "dot":
            dot += amount
        else:
            hot += amount
    return dot, hot


def tick_duration(unit):
    """Spend one of this unit's turns off every status it holds. -> expired Actives.

    Called once the turn is resolved -- including a turn that was SKIPPED, since a
    skipped turn still counts against a duration or a stun would never wear off.

    Returns the expired Active objects, not names: the caller must be able to tell the
    CLIENT, and the wire row needs the status id. Discarding this return is how a
    Freeze that had expired server-side stayed drawn on the boss with "1 turn left" on
    a real phone (2026-08-26). The client counts rounds down ONLY for the unit that
    just acted (TurnEndState.OnEnter -> UpdateStatusRound on ActionOrderList[0]); a
    unit whose turn is skipped never gets a TurnEnd there, so its expiries need a
    round-0 row from us. See battle.Battle._queue_expired for which paths send one.
    """
    expired = []
    for st in list(unit.statuses):
        if not isinstance(st, Active) or st.permanent:
            continue
        st.remaining = int(st.remaining) - 1
        if st.remaining <= 0:
            unit.statuses.remove(st)
            expired.append(st)
    return expired


def tick(unit):
    """Both halves, for callers outside a turn loop. -> (dot, hot, expired)."""
    dot, hot = tick_damage(unit)
    return dot, hot, tick_duration(unit)


# --- nested opcodes ----------------------------------------------------------------
#
# 428 of the 1,685 status rows carry their own `_action`/`_actID` script (719 applies,
# 199 removes, 87 category cleanses). A status is not only a modifier -- it can be a
# TRIGGER MARKER that grants further statuses while it is held. Their prose says so in
# so many words: "When affected by this status, Angel of Faith, Michael will trigger
# Destiny UL effects."
#
# Three readings, each forced by the data rather than chosen:
#
#   WHEN.    Once per turn while the status is held, not once when it lands. Lucifer's
#            passive spells the timing out -- "if affected by The Fallen, grants Keen
#            (CRT+35%) for two turns at EACH START OF A TURN" -- and The Fallen's row is
#            exactly `apply 2007, apply 2009`.
#   STACKS.  A repeated identical apply is a stack count, not a duplicated line.
#            `Destiny UL` lists 3804 five times; `Haughty Malefics` lists 3012 twice.
#   ONE-SHOT. A row that removes ITSELF is consumed by firing. `Never Surrender` is
#            `apply All DMG Reduction, remove Never Surrender` -- so the self-removal is
#            what separates a one-shot marker from a recurring one, and no extra flag is
#            needed to tell them apart.
#
# Deliberately NOT recursive: a status applied here runs its own nested script at the
# next turn start, because by then the unit holds it. That is the same rule as everything
# else in this module rather than a special case, and it makes an unbounded chain
# impossible without a depth counter to tune.


# A nested script that fires ONLY when its holder's own action ends, not once per global
# turn like the rest. One status states this, and the difference is load-bearing:
#
#   邪眼詛咒 (407): 自身行動結束時，造成...傷害，並50%固定機率對自身附加逆風,
#                  50%固定機率對自身附加石化
#   ("WHEN THE HOLDER'S OWN ACTION ENDS ... 50% chance to inflict Headwind on itself")
#
# Run once per global turn instead, it becomes a permanent lockout that cannot unwind: the
# Headwind it inflicts blocks the holder's move gauge, so the holder never acts -- and the
# script keeps firing anyway, renewing the Headwind forever. A unit sat out a 44-turn
# fight at FULL HP that way, which is what the fuzzer's starvation invariant caught.
# Gated on the holder's own action, a blocked holder simply stops accruing more.
#
# Measured over all 428 nested-script statuses: 30 state 回合開始時/每回合 (each turn --
# the existing reading, kept), 397 state no timing at all (so the documented default
# stands), and this is the only one that states the holder's own action. It is a SET
# rather than a flag so the next one found is a one-line addition.
OWN_ACTION_NESTED = {407}


def nested_ops(status_id):
    return (_registry(status_id) or {}).get("nested") or []


def run_nested(unit, caster=None, own_action=False):
    """Fire the nested scripts of every status `unit` holds. -> [StatusEvent-ish dicts].

    Returns what changed so the caller can put it on the wire; the client is never told
    about a status it did not see applied, and a turn-start grant has no attack of its
    own to ride on.

    `own_action` says this call is the holder's own action ending rather than the global
    per-turn sweep. Scripts in OWN_ACTION_NESTED fire only on the former, everything else
    only on the latter -- so no script can fire twice in one turn.
    """
    changed = []
    for st in list(_actives(unit)):
        if (st.status_id in OWN_ACTION_NESTED) != bool(own_action):
            continue
        ops = nested_ops(st.status_id)
        if not ops:
            continue
        stacks = {}
        for op in ops:
            if op.get("op") == "apply_status" and op.get("status"):
                stacks[int(op["status"])] = stacks.get(int(op["status"]), 0) + 1
        for sid, count in stacks.items():
            row = _registry(sid)
            if not row:
                continue
            ev = None
            for _ in range(count):
                ev = apply_event(unit, _NestedEvent(sid, row), caster or unit)
            if ev is not None:
                changed.append({"target": unit.order, "status_id": sid,
                                "applied": True, "duration": ev.remaining})
        for op in ops:
            if op.get("op") == "remove_status" and op.get("status"):
                for dead in remove_id(unit, op["status"]):
                    changed.append({"target": unit.order, "status_id": dead.status_id,
                                    "applied": False, "duration": 0})
            elif op.get("op") == "remove_category" and op.get("category"):
                for dead in remove_category(unit, op["category"]):
                    changed.append({"target": unit.order, "status_id": dead.status_id,
                                    "applied": False, "duration": 0})
    return _collapse_noops(changed)


def _collapse_noops(changed):
    """Drop (apply, remove) pairs a single run produced for the same status.

    The pack's markers routinely read `apply 400, remove 400` -- 400 being a duplicate
    row of the marker itself -- which nets to nothing. Left in, the wire carries a
    removal for a status the client was never told was applied, and the engine reports a
    change that did not happen.
    """
    seen = {}
    for i, ch in enumerate(changed):
        key = (ch["target"], ch["status_id"])
        if ch["applied"]:
            seen[key] = i
        elif key in seen:
            changed[seen.pop(key)] = None
            changed[i] = None
    return [c for c in changed if c is not None]


class _NestedEvent:
    """The minimal shape apply_event reads. A nested grant states no duration of its own,
    so it takes the module default like any other unstated one."""

    def __init__(self, status_id, row):
        self.status_id = status_id
        self.name = row.get("name")
        self.duration = None
        self.magnitude = None
        self.permanent = bool(row.get("permanent"))


def remove_category(unit, category):
    """Cleanse. -> the Active objects removed.

    Returns the objects, not the names: the client is told about a removal by a status
    row carrying that status's ID and a round of 0, so a name alone cannot be sent.

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
            gone.append(st)
    return gone


def _loose(name):
    """Normalise a status name for matching: casefold, drop a trailing qualifier and a
    leading article. The prose names a status inconsistently -- the registry row is
    `The Divine` while the clause that strips it says "removes its the Divine effect",
    which the compiler reduces to `Divine`.
    """
    n = re.sub(r"\s*\([^)]*\)\s*$", "", name or "")
    n = " ".join(re.sub(r"[^a-z0-9]+", " ", n.lower()).split())
    return n[4:] if n.startswith("the ") else n


def remove_id(unit, status_id):
    """Strip exactly the status with this id. -> the Active objects removed.

    By ID, never by name, and that distinction is load-bearing: the pack ships DUPLICATE
    rows under one name -- 397 and 399 are both "The Divine", 398 and 400 both "The
    Fallen" -- and a status's nested script routinely applies the duplicate and then
    removes it again (`apply 400, remove 400`), which is a no-op as written. Matching
    that removal by name deleted the REAL marker along with the duplicate, so Lucifer
    lost her stance a few turns after entering it.
    """
    try:
        want = int(status_id)
    except (TypeError, ValueError):
        return []
    gone = []
    for st in list(unit.statuses):
        if isinstance(st, Active) and st.status_id == want:
            unit.statuses.remove(st)
            gone.append(st)
    return gone


def remove_named(unit, name):
    """Strip one status BY NAME. -> the Active objects removed.

    Deliberately not `remove_category`, and deliberately ignores `unremovable`. This is
    not a cleanse -- it is a skill naming the exact status it replaces, as Lucifer's
    Lamenting Starlight does: "grants the caster The Fallen and removes its the Divine
    effect". The Divine is `unremovable` (a cleanse must not touch it), so routing the
    swap through the cleanse path left both markers on the caster and the toggle stopped
    toggling. A named swap outranks the cleanse guard.
    """
    want = _loose(name)
    if not want:
        return []
    gone = []
    for st in list(unit.statuses):
        if not isinstance(st, Active):
            continue
        got = _loose(st.name)
        # Equality after normalising, or containment when the shorter side is long
        # enough to be unambiguous -- a 3-letter fragment would match half the registry.
        if got == want or (min(len(got), len(want)) >= 5
                           and (want in got or got in want)):
            unit.statuses.remove(st)
            gone.append(st)
    return gone


# --- statuses whose behaviour is not derivable ------------------------------------
#
# Same problem as passives, same answer: a small table rather than special cases. The
# registry says Return is `kind: other` -- nothing in the data says it reflects. Its
# prose does: "While taking damage, deals Target's 100% ATK as damage (triggers once
# while dealing multiple attacks)."
#
# Keyed by a lowercase name fragment, matched loosely as elsewhere. The value is a
# percentage of the HOLDER's own ATK, dealt as extra damage TO THE HOLDER when it is hit.
#
# This is NOT a reflect, and reading it as one had Michael's passive attacking his own
# party. `Return` is an offensive debuff: "Sword Draw: When a battle starts, inflicts
# Return on all ENEMIES", dispelled when Michael dies. Its own line -- "While taking
# damage, deals Target's 100% ATK as damage" -- names the Target, and the Target is the
# unit being attacked, i.e. the holder. A player passive that handed every enemy a free
# retaliation aura would be a downside, not a skill.
ON_HIT_EXTRA = {
    "return": 100.0,
}

# "triggers once while dealing multiple attacks" -- a multi-hit skill triggers this ONE
# time, not once per swing, or a 4-hit skill would pay four times.
ON_HIT_EXTRA_ONCE_PER_SKILL = True


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


# --- action seals -------------------------------------------------------------------
#
# Which SKILL SLOTS a status forbids, derived from the registry's own wording rather
# than a hand list -- the same approach as the gauge blocks above, and for the same
# reason: `kind` cannot tell them apart (605/606/607 are all `control`) while the
# description states each one exactly.
#
#   605 Power Attack Seal  "cannot cast Power Attack until the effect wears off"
#   606 Special Move Seal  "cannot use their Special Move skills"
#   607 Skill Seal         "cannot cast ANY skills"
#
# Slots are com_attack / skill / sp_skill, so the basic attack is slot 0 and is never
# sealed -- which matches the old engine's `ability_seal`, whose whole effect was to
# reduce the usable list to [0].
_SEAL_POWER = re.compile(r"cannot (?:cast|use)[^.]{0,30}power attack", re.I)
_SEAL_SPECIAL = re.compile(r"cannot (?:cast|use)[^.]{0,30}special move", re.I)
_SEAL_ALL = re.compile(r"cannot (?:cast|use)[^.]{0,20}any skill", re.I)
_SEAL_SLOTS = None


def _seal_slots():
    """-> {status_id: frozenset(blocked slot indices)}."""
    global _SEAL_SLOTS
    if _SEAL_SLOTS is None:
        _SEAL_SLOTS = {}
        for sid, row in (specs.statuses() or {}).items():
            text = row.get("description") or ""
            if not text:
                continue
            if _SEAL_ALL.search(text):
                blocked = (1, 2)
            elif _SEAL_POWER.search(text):
                blocked = (1,)
            elif _SEAL_SPECIAL.search(text):
                blocked = (2,)
            else:
                continue
            _SEAL_SLOTS[int(sid)] = frozenset(blocked)
    return _SEAL_SLOTS


def sealed_slots(unit):
    """-> the set of skill slots this unit may not use right now.

    Nothing enforced these on the new path: the boss opens every raid by sealing the
    party's Power Attack and Special Move, the icons appeared, the trace showed the
    statuses -- and every button stayed live, because the old engine's check reads
    `st.definition`, which an engine status does not have, so it silently returned False.
    """
    table = _seal_slots()
    out = set()
    for st in _actives(unit):
        out |= table.get(st.status_id, frozenset())
    return out


# `kind` is NOT usable for this one. The `block_heal` bucket holds 18 rows and only two
# of them block anything: it also collects healing INCREASES ("Increases healing
# received" -- Concentrate, Blessing), healing REDUCTIONS ("reduces the caster's healing
# received" -- Injured, Deterioration), an immunity (802), and Field Shield, which is a
# shield that grants immunity to Block Heal rather than inflicting it. Trusting the kind
# blocked every heal in the game, since the party carries Field Shield from turn one.
_HEAL_BLOCK = re.compile(r"cannot be (?:healed|cured)", re.I)
_HEAL_BLOCK_NOT = re.compile(r"immunit|immune|removes", re.I)


# 4230 "CD Reduction Block": "cannot receive Skill CD reduction effect". It blocks a
# REFRESH only -- a delay still lands, which is why the check is on the sign rather than
# on the op.
_CD_BLOCK = re.compile(r"cannot receive[^.]{0,30}cd reduction|cannot[^.]{0,20}reduce"
                       r"[^.]{0,20}(?:skill\s*)?cd", re.I)


def blocks_cd_reduction(unit):
    for st in _actives(unit):
        text = (_registry(st.status_id) or {}).get("description") or ""
        if _CD_BLOCK.search(text):
            return True
    return False


def blocks_heal(unit):
    """Does a status stop this unit being healed outright? (`Block Heal`.)

    Only a flat block. "Reduces healing received" is a magnitude, not a veto, and is
    left to the stat path rather than silently rounded up to zero.
    """
    for st in _actives(unit):
        text = (_registry(st.status_id) or {}).get("description") or ""
        if _HEAL_BLOCK.search(text) and not _HEAL_BLOCK_NOT.search(text):
            return True
    return False


# --- forced / scrambled targeting ----------------------------------------------------
#
# THREE different redirects, which the old engine's single `confused_targeting` flag
# could not tell apart. Each states itself exactly in the registry:
#
#   608/674 Taunt    "can only attack the taunt caster before the effect wears off"
#   612 Enchant,     "they will attack allies before the effect wears off"
#   619 Charm        (Charm adds "using normal attacks")
#   611/615 Confuse  "will attack both allies and enemies"
#
# Precedence: losing control of your target beats being drawn to a specific one, so
# Confuse/Charm outrank Taunt.
_REDIRECT_TAUNT = re.compile(r"only attack the taunt caster", re.I)
_REDIRECT_ALLIES = re.compile(r"will attack allies", re.I)
_REDIRECT_ANY = re.compile(r"attack both allies and enemies", re.I)
_REDIRECT_NOT = re.compile(r"immunit|immune", re.I)
_REDIRECT_IDS = None


def _redirect_ids():
    """-> {status_id: "taunt" | "allies" | "any"}."""
    global _REDIRECT_IDS
    if _REDIRECT_IDS is None:
        _REDIRECT_IDS = {}
        for sid, row in (specs.statuses() or {}).items():
            text = row.get("description") or ""
            if not text or _REDIRECT_NOT.search(text):
                continue
            if _REDIRECT_ANY.search(text):
                _REDIRECT_IDS[int(sid)] = "any"
            elif _REDIRECT_ALLIES.search(text):
                _REDIRECT_IDS[int(sid)] = "allies"
            elif _REDIRECT_TAUNT.search(text):
                _REDIRECT_IDS[int(sid)] = "taunt"
    return _REDIRECT_IDS


def redirect(unit):
    """-> ("any"|"allies"|"taunt", source_order) for this unit's control statuses.

    None when nothing redirects it. Returns the KIND rather than a target so the caller
    can apply it against the live field -- the taunt source may have died since.
    """
    table = _redirect_ids()
    found = {}
    for st in _actives(unit):
        kind = table.get(st.status_id)
        if kind:
            found.setdefault(kind, st.source_order)
    for kind in ("any", "allies", "taunt"):        # precedence, see above
        if kind in found:
            return kind, found[kind]
    return None


def blocks_gauge_gain(unit):
    """Headwind and friends: this unit's move gauge does not fill."""
    ids, _ = _gauge_block_ids()
    return any(st.status_id in ids for st in _actives(unit))


def blocks_gauge_gain_status(active):
    """Is THIS status the thing stopping the bar? -> bool.

    `blocks_gauge_gain` answers it for a unit; the caller ageing a block on the battle's
    clock has to know which of the unit's statuses to age.
    """
    ids, _ = _gauge_block_ids()
    return isinstance(active, Active) and active.status_id in ids


def blocks_gauge_loss(unit):
    """Steady and friends: this unit's move gauge cannot be reduced."""
    _, ids = _gauge_block_ids()
    return any(st.status_id in ids for st in _actives(unit))


def on_hit_extra_damage(victim):
    """-> extra damage the victim's own statuses add when it is struck, or 0.

    Paid by the VICTIM and scaled off the VICTIM's ATK -- see ON_HIT_EXTRA.
    """
    total = 0.0
    for st in _actives(victim):
        name = (st.name or "").lower()
        for frag, pct in ON_HIT_EXTRA.items():
            if frag in name:
                total += float(getattr(victim, "atk", 0) or 0) * pct / 100.0
                break
    return int(total)


# --- the reads -------------------------------------------------------------------

def _actives(unit):
    return [s for s in getattr(unit, "statuses", []) if isinstance(s, Active)]


# ---- the control debuffs' numeric halves ------------------------------------
# Four statuses state a second effect in Chinese and state no NUMBER for it, in either
# language. Following the project's tier order (CLAUDE.md section 1): the Chinese is
# silent on the figure, the English is silent too, so the figure is OURS -- a design
# choice, named here, in one knob, and nowhere else.
#
# What is NOT ours is the CHANNEL, and this is where reading the English first goes
# wrong. Each entry below takes its direction and its stat from `_note1`:
#
#   610 麻痺  無法行動並且受到的傷害增加   -> damage TAKEN up.
#   613 石化  自身防禦力上升但無法行動     -> the holder's own DEF UP. The English says
#             "take reduced damage", which is a different channel with a different
#             interaction with defence ignores -- the Chinese wins.
#   619 幻惑  攻擊力降低，且...使用普攻    -> the holder's ATK down.
#   602 凍結  無法行動並且受到持續性的傷害 -> a damage-over-time.
#
# DELIBERATELY ABSENT, because for these the Chinese is COMPLETE rather than silent, so
# there is nothing to fall back FROM:
#   612 魅惑  無法操控並且必定攻擊友方 -- EN 612 claims an ATK penalty the Chinese does
#             not state anywhere. That is a translation error, not a gap.
#   601 暈眩  無法行動 -- full stop. (EN calls 610 "Stun" and 601 "Daze"; the damage
#             clause belongs to 麻痺/610, not here.)
#   611/615 混亂 無法分別敵我 -- scrambles targeting, no DoT in either language.
#
# OWNER-SANCTIONED HOUSE NUMBER. One knob for all four so they stay comparable and so
# there is exactly one place to change when footage or prose ever states a real figure.
# Each map is independent, so a single recovered number can be split out without
# disturbing the others.
CONTROL_RIDER_PCT = 25.0

CONTROL_DAMAGE_TAKEN = {610: +CONTROL_RIDER_PCT}     # 麻痺 受到的傷害增加
CONTROL_SELF_DEF_UP = {613: +CONTROL_RIDER_PCT}      # 石化 自身防禦力上升
CONTROL_ATK_DOWN = {619: -CONTROL_RIDER_PCT}         # 幻惑 攻擊力降低
# 凍結's tick. Runs through the ORDINARY DoT machinery -- a percentage of the inflicter's
# ATK, snapshotted in `source_atk` like every other prose DoT -- rather than the "share of
# the hit that applied it" the contribution proposed. That would have been a second,
# parallel damage-over-time mechanism for one status, and nothing in either language asks
# for one.
CONTROL_DOT = {602: CONTROL_RIDER_PCT}               # 凍結 受到持續性的傷害


def _control_rider(unit, table):
    """-> the summed house rider from `table` for this unit's active statuses."""
    total = 0.0
    for st in _actives(unit):
        pct = table.get(st.status_id)
        if pct:
            total += pct * max(1, int(st.stacks))
    return total


# ---- Bankai's other half ----------------------------------------------------
# 萬解 (status 158) states a SPD gain that reaches nothing, for the same two reasons
# Taunt's ATK penalty did not: the registry classifies it `kind=other` so
# `stat_multiplier` skips it, and its `category` is `misc` so `signed_magnitude` cannot
# pick a direction. The row carries `stat: SPD` and no number at all.
#
# THE NUMBER IS STATED, just not on Satan's own rung. Her passive writes only
# 自身速度提升 ("raises own SPD"), but the same status spelled out on the three alt-band
# casts gives the figure outright:
#
#   ※ 萬解：攻擊行動後追加使用1次普攻IV。自身速度+35%，不可疊加，持續整場戰鬥。
#           (151000121 / 152000121 / 153000121)
#
# So this is a RECONSTRUCTION -- the pack's own figure for the pack's own status -- not a
# house number. Contributed, and checked against all three rows before being taken.
BANKAI_SPD_UP = {158: +35.0}


# ---- Taunt's other half -----------------------------------------------------
# 挑釁 and 超．挑釁 state an ATK penalty that the English description drops entirely:
#
#   ※ 挑釁    ：使目標攻擊力-35%並只攻擊自己，持續1回合。
#   ※ 超．挑釁：使目標攻擊力-35%並只攻擊自己，持續1回合，不受全免疫影響，不可解除。
#   EN 608/674: "can only attack the taunt caster before the effect wears off."
#
# Only the "只攻擊自己" half was implemented (battle._forced_target). The penalty died
# TWICE over: the registry classifies these `kind=control`, so `stat_multiplier` skipped
# them, and their `category` is `misc`, so `signed_magnitude` could not justify a
# direction either -- even though the compiler had already parsed the figure onto 584
# clauses, per level (20/30/35).
#
# The real cause is that `kind` is single-valued and 挑釁 is genuinely both a control and
# a stat_mod. Fixing that is a compile_statuses change touching every consumer, so this
# is the narrow fix and this comment is the record of why.
#
# NOT widened to the other 15 `kind=control` rows carrying a `stat`: most of those
# magnitudes are not stat changes at all -- 603 Poison's `stat=ATK mag=75` is the SIZE OF
# ITS DoT, 611 Confuse's `stat=HP` likewise, 159 Charge's 180 is a damage coefficient.
# These two are here because the Chinese states a stat, a direction and a number.
# 幻惑/石化/麻痺 all state a stat change with NO number in either language, so they get
# nothing rather than an invented figure.
TAUNT_ATK_DOWN = (608, 674)

# 幻惑 (EN "Charm"): the only redirect whose prose also restricts the SKILL --
# "將使用普攻攻擊我方". Consumed by battle._forced_skill; see there for why 612/611/608
# are not in it and why the English descriptions point the wrong way.
CHARM_BASIC_ONLY = 619


def stat_multiplier(unit, stat):
    """-> a multiplier for ATK/DEF/SPD/HP from the unit's active statuses.

    Percentages stack ADDITIVELY, not multiplicatively: two ATK-35% give x0.30, which is
    what "stacks up to N times" reads as and what the old engine did. Floored at 0 so a
    stack of debuffs cannot invert the stat.
    """
    total = 0.0
    for st in _actives(unit):
        if st.status_id in TAUNT_ATK_DOWN and stat.upper() == "ATK":
            # Sign from the prose ("攻擊力-35%"), magnitude from the clause that landed
            # it, so a 20%/30%/35% rung pays its own figure. See TAUNT_ATK_DOWN.
            if st.magnitude is not None:
                total -= abs(float(st.magnitude)) * max(1, int(st.stacks))
            continue
        if st.kind != "stat_mod" or (st.stat or "").upper() != stat.upper():
            continue
        m = st.signed_magnitude()
        if m is not None:
            total += m
    # 幻惑's ATK drop and 石化's DEF rise: stated in the Chinese, numbered nowhere.
    if stat.upper() == "ATK":
        total += _control_rider(unit, CONTROL_ATK_DOWN)
    elif stat.upper() == "DEF":
        total += _control_rider(unit, CONTROL_SELF_DEF_UP)
    elif stat.upper() == "SPD":
        # 萬解's 自身速度+35% -- see BANKAI_SPD_UP. Unlike the riders above, this figure
        # is the pack's own, read off the same status on another cast's rung.
        total += _control_rider(unit, BANKAI_SPD_UP)
    return max(0.0, 1.0 + total / 100.0)


def crit_rate_bonus(unit):
    """-> the CRIT RATE change this unit's statuses add, as a 0..1 DELTA.

    `stat_multiplier` covers ATK/DEF/SPD/HP and nothing else, so the nine `kind=stat_mod`
    statuses whose stat is CRI -- Execute Critical I-III, Critical Surge I-III, Critical
    Injection I-III -- applied, drew their icon, counted down and changed nothing:
    `formula.strike` read crit off the unit's own `cri` attribute and never from a
    status, in either direction.

    ADDITIVE, not a multiplier, because crit is a RATE: +20% on a 34% base is 54%, not
    40.8%. That is also why this returns a delta rather than reusing `stat_multiplier`.
    Not floored -- a reduction is a real effect and `strike` clamps the result to 0..1
    itself. Stacking is additive and `signed_magnitude` already scales by `stacks`, the
    same contract `stat_multiplier` has.

    ONLY `kind=stat_mod`. The registry's `other` bucket also holds 146 statuses that
    name a stat, and it is NOT safe to read as a stat change: `Fear Nothing` (HP) is
    pursuit damage, `Grand Feast` (HP) is a heal, `Admonition (Reduce CRT)` is its own
    thing. `other` means unclassified, not "a stat mod we forgot to label".
    """
    total = 0.0
    for st in _actives(unit):
        if st.kind != "stat_mod" or (st.stat or "").upper() != "CRI":
            continue
        m = st.signed_magnitude()
        if m is not None:
            total += m
    return total / 100.0


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
        # "Damage taken" and "damage dealt" are the same kind with opposite subjects.
        # This used to read the English NAME for it -- "taken"/"reduction"/受 -- and
        # that is wrong for 84 of the 148 damage_mod statuses, because the names simply
        # do not say: `Fortitude`, `Legion Aegis`, `My Guardian` and `Wide Defense` are
        # all damage-TAKEN modifiers, and every one of them was being applied to the
        # holder's damage DEALT instead. The registry now carries the answer, read from
        # the Chinese glossary where it speaks (`subject`, compile_statuses.py).
        #
        # A status whose subject is UNRESOLVED is skipped rather than guessed at -- 22
        # of them. The old name heuristic is not a safe fallback here: it is the thing
        # being replaced, and on this population it is wrong more often than right.
        if st.subject is None or (st.subject == "taken") != taken:
            continue
        total += m
    if taken:
        # 麻痺: "無法行動並且受到的傷害增加". No design row carries the figure.
        total += _control_rider(unit, CONTROL_DAMAGE_TAKEN)
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

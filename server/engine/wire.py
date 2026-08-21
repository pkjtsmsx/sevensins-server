"""Turn an `Outcome` into the client's `AttackJsonData`. The ONLY place `data` is built.

Every wire invariant is asserted here rather than hoped for, because each one has already
cost a debugging session and none of them fail loudly on their own:

  * **too few groups** -- `AttackBehavior.BscTag` case 5 reads `DmgInfo[0]` behind a
    `Count >= 1` guard, so a missing group does not throw. The swing animates with **no
    damage number** and the fight carries on. Silent and cosmetic.
  * **a unit twice in one group** -- the client reads each group into a
    `Dictionary<string,bool>` keyed by target order (`OnDamageAndNumber` builds
    `statusDic`, `OnDamage` does the `Add`). A duplicate throws "An item with the same
    key has already been added", which the generic event handler swallows: no stack, no
    animation, and the attacker never yields its turn. Silent and **fatal**.
  * **`die` on more than one row** -- rows carry the unit's final state, so a unit killed
    on swing 1 would replay its death animation on every later swing.

Asserting turns each of those into a server-side test failure instead.

## The shapes, from the client

`AttackJsonData` has only two `[JsonProperty]` fields -- `passiveID` -> `pskill_id` and
`DmgInfo` -> `data`. `caster` and `skill` reach it through the constructor
`.ctor(string caster, int skill)`, which Newtonsoft matches to JSON keys by parameter
name, so they are real wire keys with no attribute to find.

`data` is `List<List<DamageInfo>>`: **one inner list per cinematic swing.**

`DamageInfo` keys, from the JsonProperty thunks:
`Order c, Mode md, ChargeType cg, Damage dmg, nCri cri, nDie die, status status,
Extra extra, passiveIconList picons, passiveID pskill_id`.

## Mode, and the sign of `dmg`

From `AttackBehavior.OnDamage` (0x1BE2A18):

    case 1  HP change.  `Damage < 0` plays the hurt voice, records the unit in
            `CurInjures` and calls `PlayInjured`; `Damage > 0` does not.
            So **damage rides NEGATIVE and healing rides positive.**
    case 2  revive -- calls `doRebornUnit`, then returns EARLY, so a mode-2 row's
            `status` and `extra` are never read.
    case 4  move gauge -- `ShowScvBar`.

`Mode == 5` is special-cased one level up in `OnDamageAndNumber`: the unit lookup is
skipped entirely, so such a row names no unit.

`status` entries are `[order, skill_id, rounds]` -- each names its own unit, so they can
all hang off the lead row.
"""

MODE_HP = 1
MODE_REVIVE = 2
MODE_GAUGE = 4

# `Extra` is List<List<DamageInfo>> and `OnDamage` recurses through it, so follow-up
# outcomes could nest there. We flatten into the parent's groups instead: nesting makes
# the one-row-per-unit-per-group rule much harder to verify, and the flat form renders
# identically.
FLATTEN_FOLLOW_UPS = True


class WireError(AssertionError):
    """A payload that would have failed silently on the client."""


def _row(order, mode, amount, crit=False, died=False):
    return {"c": order, "md": mode, "cg": 0, "dmg": amount,
            "cri": 1 if crit else 0, "die": 1 if died else 0,
            "status": [], "extra": [], "picons": [], "pskill_id": 0}


def _all_strikes(outcome):
    """-> every strike in the tree, parents before children.

    A follow-up's strikes belong to the same animation: the sub-skill has no cinematic of
    its own being played here, so its damage has to ride the invoker's swings.
    """
    out = list(outcome.strikes)
    if FLATTEN_FOLLOW_UPS:
        for child in outcome.children:
            out.extend(_all_strikes(child))
    return out


def _status_rows(outcome):
    rows = []
    for ev in outcome.statuses:
        if not ev.applied or ev.status_id is None:
            continue
        # An unknown duration must not silently become 0 -- the client counts `rounds`
        # down itself, and 0 would expire the status instantly. 1 is the minimum that
        # still shows the icon; the uncertainty is recorded in the spec, not here.
        rounds = ev.duration if ev.duration is not None else 1
        rows.append([ev.target, int(ev.status_id), int(rounds)])
    for child in outcome.children:
        rows.extend(_status_rows(child))
    return rows


def attack_json(outcome, *, caster_order=None, skill_id=None):
    """-> the AttackJsonData dict for one skill use.

    Raises `WireError` rather than emitting a payload that the client would mishandle.
    """
    swings = max(1, int(outcome.swings or 1))
    groups = [[] for _ in range(swings)]

    def fold(group, order, mode, amount, crit, died):
        """One row per (unit, mode) per group -- duplicates are SUMMED, not appended.

        Summing is not a concession to the client: one number per target per swing is
        all the wire shape can express, and it is what the damage popup shows either
        way. The total is unchanged. Mode is part of the key because a unit can take
        damage and have its gauge moved in the same swing, and those are different rows.
        """
        for r in group:
            if r["c"] == order and r["md"] == mode:
                r["dmg"] += amount
                r["cri"] = r["cri"] or (1 if crit else 0)
                r["die"] = r["die"] or (1 if died else 0)
                return
        group.append(_row(order, mode, amount, crit, died))

    for st in _all_strikes(outcome):
        idx = min(max(int(st.swing), 0), swings - 1)
        # NEGATIVE: `IsDamage` is `Mode == 1 && Damage < 0`.
        fold(groups[idx], st.target, MODE_HP, -int(st.amount),
             bool(st.detail.get("crit")), bool(st.died))

    # Non-damage effects have no swing of their own; they ride the first group so they
    # resolve at the start of the animation.
    lead = groups[0]
    for h in outcome.heals:
        fold(lead, h["target"], MODE_HP, int(h["amount"]), False, False)
    # NO mode-4 rows. Emitting the move-gauge change as a DamageInfo row stalls the
    # client outright -- found on device: every skill carrying `modify_gauge` (Lucifer's
    # Eclipse Slash, Metatron's Poison Injection) hung the fight after the animation,
    # while the same casts' other skills were fine.
    #
    # It is also the wrong channel. The gauge is server-authoritative and reaches the
    # client through `sync[order].Scv`, which is the very field `GetNextAction` re-runs
    # the ATB from to draw the "Next" badge (contract doc 3.5). The caller applies
    # `outcome.gauge` to the unit and lets `sync` carry it; see engine/bridge.py.
    for rv in outcome.revives:
        # Mode 2 returns early in OnDamage, so it must be its OWN row -- merging a
        # revive into an HP row would drop whichever effect lost the merge.
        lead.append(_row(rv["target"], MODE_REVIVE, int(rv.get("hp") or 0)))

    # A skill whose only effect was the move gauge now produces no rows at all (the
    # gauge left the payload -- see above). The client still needs a combo entry to drive
    # the animation and yield the turn, so it gets a single zero-damage row, which is
    # what the old engine did for the same reason. An EMPTY combo is not an option.
    # The caster is the fallback when the skill resolved no targets at all -- a
    # caster-only gauge effect is the case that reaches here, and a combo entry still
    # has to name somebody.
    if not any(groups):
        who = outcome.targets[0] if outcome.targets else outcome.caster
        if who:
            groups[0].append(_row(who, MODE_HP, 0))

    # Trailing empties are legitimate: every target died before the later swings landed,
    # and the cinematic simply plays those swings with no number. An INTERIOR empty is
    # not -- it means a swing produced nothing while a later one produced something,
    # which would shift every subsequent group onto the wrong tag.
    while len(groups) > 1 and not groups[-1]:
        groups.pop()
    for i, g in enumerate(groups):
        if not g:
            raise WireError(
                f"group {i} of {len(groups)} is empty but a later group is not; "
                f"the cinematic would pair damage with the wrong swing")

    _assert_invariants(groups, swings)

    if groups and groups[0]:
        groups[0][0]["status"] = _status_rows(outcome)

    return {"caster": caster_order or outcome.caster,
            "skill": int(skill_id if skill_id is not None else (outcome.skill_id or 0)),
            "pskill_id": 0, "data": groups}


def _assert_invariants(groups, swings):
    if len(groups) > swings:
        raise WireError(f"{len(groups)} groups for {swings} cinematic swings -- "
                        f"the extra groups will never be consumed")

    for i, g in enumerate(groups):
        seen = set()
        for r in g:
            key = (r["c"], r["md"])
            if key in seen:
                raise WireError(
                    f"unit {r['c']} appears twice in group {i} with mode {r['md']}; "
                    f"the client's dictionary insert throws and the fight stalls")
            seen.add(key)

    # `die` belongs on the LAST row naming a unit, and on exactly one.
    died = {}
    for i, g in enumerate(groups):
        for j, r in enumerate(g):
            if r["die"]:
                died.setdefault(r["c"], []).append((i, j))
    for order, at in died.items():
        if len(at) > 1:
            raise WireError(f"unit {order} is flagged die on {len(at)} rows {at}; "
                            f"it would replay its death animation")


def clear_die_except_last(groups):
    """Fold repeated `die` flags down to the last row naming each unit.

    Rows carry the unit's FINAL state, so a unit killed on swing 1 reads as dead on every
    later row. Callers that build rows from final state should run this before asserting.
    """
    seen = set()
    for g in reversed(groups):
        for r in reversed(g):
            if not r["die"]:
                continue
            if r["c"] in seen:
                r["die"] = 0
            else:
                seen.add(r["c"])
    return groups

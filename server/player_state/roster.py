"""Roster: the character collection — seeding, formations, lock/decompose/capacity/sort, and the two lobby icons.

Split out of the former monolithic core.py; depends only on .core.
"""


import json
import battle as bt
import design_data as dd

from .core import (
    item_count,
    spend_item,
    CHAR_BUYCOUNT_MAX,
    CHAR_SORT_SLOTS,
    char_sort_list,
    EMPTY_SLOT,
    FORMATION_SLOTS,
    TUTORIAL_CASTS,
    TUTORIAL_CAST_LIMIT,
    TUTORIAL_CAST_LV,
    char_star,
    grant_reward,
    quest_completed,
    save,
    uid,
)



def stage_ap_cost(stage_row):
    """-> (item id, count) a stage charges to enter, or None for ordinary stamina."""
    if int(stage_row.get("_ap_type") or 0) != 2:
        return None
    iid = int(stage_row.get("_ap_v1") or 0)
    return (iid, int(stage_row.get("_ap") or 1)) if iid else None
TUTORIAL_DONE_QUEST = 31002


def maybe_reset_tutorial_casts(state):
    """Keep the starter casts right for the tutorial, then hand them to the player -- a
    ONE-TIME transition. Idempotent; returns True if it changed anything (caller
    persists + re-syncs).

    The forced newbie battles are BALANCED around a boosted Lucifer/Leviathan. While the
    tutorial is unfinished (quest 31002 not done) the two casts are held at level 10 /
    rank 4 (this also repairs accounts seeded before the boost existed). The moment it
    finishes, casts STILL at the exact tutorial boost revert to base, and the account is
    stamped `tutorial_casts_done` -- after which this NEVER touches them again, so any
    real leveling the player does post-tutorial is preserved.

    The `tutorial_casts_done` stamp is also why an already-developed account (e.g. 1000001,
    Lucifer at level 100) is safe: it is `done` but its casts are NOT at limit_char 12, so
    nothing is reverted -- it is just stamped and left alone.
    """
    if state.get("tutorial_casts_done"):
        return False
    done = quest_completed(state, TUTORIAL_DONE_QUEST)
    changed = False
    for entry in state.get("roster", {}).values():
        if entry.get("id") not in TUTORIAL_CASTS:
            continue
        boosted = int(entry.get("limit_char", 0) or 0) >= TUTORIAL_CAST_LIMIT
        if done:
            if boosted:                        # still the untouched tutorial loadout
                entry.update({"lv": 1, "xp": 0, "plus": 0,
                              "limit_char": 0, "limit_book": 0, "super_star": 0})
                changed = True
        elif not boosted:
            entry.update({"lv": TUTORIAL_CAST_LV, "limit_char": TUTORIAL_CAST_LIMIT,
                          "limit_book": 0})
            changed = True
    if done:
        # One-time: stamp so post-tutorial leveling is never clobbered.
        state["tutorial_casts_done"] = True
        changed = True
    return changed


# Unsummon / "Mana Extract" (PanelSell action type 0, CharRpcServerCmd.char_sell 291).
# Selling a cast pays the mana crystals named on its own design row -- `_sellId` x
# `_sellNumber`, e.g. Lucifer = 6x item 502 "Prime Mana Crystal" -- plus coins. Low
# rarity casts (the gremlins) carry `_sellId` 0 and pay coins only.
#
# The coin figure comes from CharDefine.SellMoneyStar, a 6-entry int[] whose values
# live in global-metadata.dat (an il2cpp RuntimeFieldHandle blob) rather than in
# libil2cpp.so, and which has NO C# reader at all (only its cctor and a Puerts
# get/set), so it could not be read out the way every other constant here was. It is
# calibrated instead against the panel's own pre-confirm preview -- the same number the
# live server paid.
#
# Calibrated 2026-08-04 from four previews. Keyed by the cast's RANK (the upgradeable
# star), NOT by design `_rarity`: rarities 3/4/5 give three different payouts while
# ranks 4/5/6 line up one-to-one, and a 6-entry array indexed [rank - 1] covers ranks
# 1..6 exactly.
#
#   rank 3 -> 500     (rarity-2 gremlin, lv 1)
#   rank 4 -> 1500    (Jacqueline / Evelina, rarity 3, lv 1)
#   rank 5 -> 3000    (Dixie, rarity 4, lv 1)
#   rank 6 -> 15000   (Lucifer / Leviathan, rarity 5, lv 100)
#
# LEVEL DOES NOT MATTER, tested directly: a rank-3 gremlin levelled to 30 server-side
# still previewed 500, identical to its lv-1 siblings. That was worth checking because
# every sample below rank 6 was level 1 while the only rank-6 sample was level 100, so
# the 15000 could have been a level effect. It is not -- the payout is purely
# SellMoneyStar[rank - 1]. Ranks 1-2 are 0 because no cast is ever ranked that low: the
# lowest grade in the game is the rank-3 fodder, so those slots are unreachable rather
# than uncalibrated. This table is COMPLETE for every rank that exists.
SELL_COIN_BY_RANK = (0, 0, 500, 1500, 3000, 15000)


def sell_coins(rank):
    """Coins paid for unsummoning a cast of this rank. CharDefine.SellMoneyStar is a
    6-entry array indexed [rank - 1], so rank 1 is the first slot."""
    try:
        return SELL_COIN_BY_RANK[int(rank) - 1]
    except (IndexError, TypeError, ValueError):
        return 0

# Coin, as an ITEM id (grant_reward routes it to currency via the item row's _action).
# Same id the login bonus pays: mail row 1001 is _itemID 2 x 50000 = "coin 50000".
COIN_ITEM_ID = 2


def char_sell_gain(char_id, rank):
    """-> [(item id, amount), ...] paid for unsummoning one cast.

    Mana crystals are exactly the cast's own `_sellId` x `_sellNumber` with no level or
    rank scaling -- verified against the panel preview (Lucifer +6 x 502, Dixie +2 x
    502, Jacqueline +3 x 501, all matching their design rows). Coins scale with rank."""
    row = dd.row("char", char_id) or {}
    gain = []
    sell_id, sell_n = row.get("_sellId") or 0, row.get("_sellNumber") or 0
    if sell_id and sell_n:
        gain.append((sell_id, sell_n))
    coins = sell_coins(rank)
    if coins:
        gain.append((COIN_ITEM_ID, coins))
    return gain


def sell_chars(state, uids):
    """Unsummon casts: drop them from the roster and pay out.

    Returns [(item id, amount), ...] merged across every cast sold. The charIDDic
    record is deliberately NOT pruned -- Soul Link help rule 3 says the pedia keeps a
    cast's record even after it is unsummoned or lost."""
    gain = {}
    sold = []
    for uid in uids:
        entry = state["roster"].get(uid)
        if not entry:
            continue
        row = dd.row("char", entry["id"]) or {}
        star = entry.get("star") or char_star(row.get("_rarity"))
        for item_id, amount in char_sell_gain(entry["id"], star):
            gain[item_id] = gain.get(item_id, 0) + amount
        del state["roster"][uid]
        sold.append(uid)
    # A sold cast must not linger in a formation, or the client keeps showing it in the
    # party and CompiledCharDic would mark a uid that no longer exists.
    for form in state.get("formations", []):
        form["array"] = ["" if u in sold else u for u in form.get("array", [])]
    for item_id, amount in gain.items():
        grant_reward(state, item_id, amount)
    return sold, sorted(gain.items())


def set_helper(state, char_uid):
    """Choose the cast lent to friends. -> the stored uid."""
    if char_uid not in state.get("roster", {}):
        raise ValueError(f"helper uid {char_uid!r} is not in the roster")
    state["helper"] = char_uid
    return char_uid


def set_showgirl(state, char_id, char_state, offset):
    """Choose the lobby showgirl. -> (id, state, offset)."""
    if not (bt.dd.row("char", int(char_id)) or {}):
        raise ValueError(f"showgirl id {char_id} is not a DesignCharForm row")
    state["showgirl"] = int(char_id)
    state["showgirl_state"] = int(char_state)
    state["showgirl_offset"] = str(offset or "")
    return state["showgirl"], state["showgirl_state"], state["showgirl_offset"]


def battle_team(state, index=0):
    """The party to field, taken from a saved formation so that editing the team in
    the lobby actually changes who fights. Falls back to the raw `team` list for
    accounts whose roster could not be seeded.

    Yields the roster entries themselves (id + lv + star + super_star), which
    battle.Battle accepts in place of bare ids, so a fight uses the same _growStar
    rungs the lobby just displayed."""
    try:
        slots = state["formations"][index]["array"]
    except (KeyError, IndexError, TypeError):
        return list(state.get("team", []))
    party = [dict(state["roster"][u], uid=u) for u in slots
             if u and u in state["roster"]]
    return party or list(state.get("team", []))


def set_formation(state, index, uids, support):
    """Apply a client formation edit (PlayerChar cmd 274) and persist it.

    Returns the stored FormationData so the caller can echo it back on cmd 530,
    which replaces formations[index] wholesale on the client side.
    """
    slots = [u if u in state["roster"] else EMPTY_SLOT
             for u in list(uids)[:FORMATION_SLOTS]]
    slots += [EMPTY_SLOT] * (FORMATION_SLOTS - len(slots))
    data = {"array": slots, "sup": support}
    if 0 <= index < len(state["formations"]):
        state["formations"][index] = data
        save(state)
    return data


# ---- lock / decompose / capacity / sort / remove-from-all-formations --------
# Read out of PlayerChar.OnClientCmdReceived (0x1698584) and the four handlers it
# dispatches to. Reply cmds: 276->532, 277->533, 292->548 (fail 597), 312->none,
# 321->577. Every one of these is reachable from the cast list, and the sell/unsummon
# precedent says an unanswered one soft-locks its panel rather than merely doing nothing.


def toggle_char_lock(state, uids):
    """CharRpcServerCmd.lock_char (276). -> [(uid, new state), ...]

    It is a TOGGLE and the server owns the decision: `RequestServerLock(List<string>)`
    (0x16A00A0) sends the uids as strargs and **no intargs at all**, and
    `PanelCharacterInformation.OnLockClick` passes exactly one uid with no desired
    state. Echoing a fixed 1 back would make the padlock un-clearable.

    `receivedLockChar` (0x169A36C) does `charDic[strargs[0]].dbChar.ilock = intargs[0]`
    -- ONE uid per reply, and it uses Dictionary.get_Item, which THROWS on a uid it does
    not hold. So reply once per uid and never name a uid we do not have.
    `DBCharData.ilock` is wire key `lock`, which char_json already emits.
    """
    changed = []
    for uid in uids:
        entry = state["roster"].get(uid)
        if entry is None:
            continue
        entry["lock"] = 0 if entry.get("lock") else 1
        changed.append((uid, entry["lock"]))
    return changed


def decompose_gain(char_id):
    """-> [(item id, amount), ...] paid for decomposing one cast.

    The `char_decompose` design form is keyed by the cast's `char._group` (Michael,
    group 10101 -> Gust of Agility x4/x3/x2). Casts whose group has no rows -- most of
    the 3017 char rows are boss/placeholder entries -- yield nothing.
    """
    row = dd.row("char", char_id) or {}
    group = row.get("_group")
    if group is None:
        return []
    return [(r["_item_id"], r["_item_count"])
            for r in (dd.rows("char_decompose") or {}).values()
            if r.get("_group") == group and r.get("_item_id")]


def decompose_chars(state, uids):
    """CharRpcServerCmd.char_decompose (292). -> (decomposed uids, [(item, amt), ...])

    Mirrors sell_chars: `receivedCharDecompose` (0x169AB8C) drops every uid in strargs
    from charDic and reads intargs as FLAT [item id, amount, ...] pairs for the reward
    popup, exactly like char_sell. Refuses casts that are locked or fielded, the same
    rule the Unsummon path enforces -- otherwise the client keeps showing a uid that no
    longer exists.
    """
    in_party = {u for f in state.get("formations", []) for u in f.get("array", []) if u}
    gain, done = {}, []
    for uid in uids:
        entry = state["roster"].get(uid)
        if not entry or uid in in_party or entry.get("lock"):
            continue
        for item_id, amount in decompose_gain(entry["id"]):
            gain[item_id] = gain.get(item_id, 0) + amount
        del state["roster"][uid]
        done.append(uid)
    for item_id, amount in gain.items():
        grant_reward(state, item_id, amount)
    return done, sorted(gain.items())


def remove_from_all_formations(state, uid):
    """CharRpcServerCmd.remove_from_allformation (321). -> the formations dict to echo.

    `receivedRemoveFromAllFormation` (0x169986C) takes TWO strargs -- [0] normal
    formations, [1] arena-team formations -- and indexes [1] before checking the count,
    so both must be present. Each is `Dictionary<int, FormationData>` and the client does
    `formations.set_Item(key - 1, value)`, i.e. the keys are **1-based**.
    Both are also deref'd right after deserializing, so neither may be a string that
    yields null: send real JSON (`{}` at worst), never `""`.
    """
    for form in state.get("formations", []):
        form["array"] = [EMPTY_SLOT if u == uid else u for u in form.get("array", [])]
    return {str(i + 1): f for i, f in enumerate(state.get("formations", []))}


def set_char_sort(state, index, sort_type, down):
    """CharRpcServerCmd.set_sort (312). Fire-and-forget -- there is no set_sort in
    CharRpcClientCmd, so the client never waits for a reply; we only need to persist it
    so the next login sync returns the same ordering.

    `sort_list` is CHAR_SORT_SLOTS entries of "<type>_<down>"; `index` selects the slot
    (one per cast-list context).
    """
    slots = char_sort_list(state)
    state["sort_list"] = slots
    if 0 <= index < CHAR_SORT_SLOTS:
        slots[index] = f"{int(sort_type)}_{int(down)}"
    return slots


def char_capacity(state):
    """CharRpcServerCmd.char_max (277). Despite the name this is NOT "max out a cast":
    case 533 sets `PlayerCharData.addCharCount = intargs[0]` and raises
    CharEventType.CHAR_CHAR_MAX -- it is the roster CAPACITY purchase/query.
    Capacity itself is CharCapacityDefault(100) + CharCapacityPerBuycount(5) * add_char.
    """
    return int(state.get("add_char", CHAR_BUYCOUNT_MAX))


# ---- AUTO PLAY (the offline sweep) -----------------------------------------
# PanelAutoPlay's Start button sends **PlayerStage cmd 3**
# `RequestServerStageAutoStart(stageID, count, useQuickBattleCoupon)` (0x180C318). The
# three answers it wants back:
#   cmd 33 HandleAutoSuccess -- intargs EXACTLY [stageID, count]; shows confirm 401,
#          "sweep started". Any other length and it returns without a word.
#   cmd 24 HandleAutoSync    -- strargs[0] is a StageSyncData whose `auto` dict is
#          Dictionary<int, AutoRunData>; it dispatches StageEvent 1 so the panel redraws.
#   (stop) HandleAutoStop    -- intargs EXACTLY 3, confirm 402.
# AutoRunData's wire keys are {stage_id, count, duetime, starttime} -- a TIMED job, which
# is what the panel's "Est. Time" is counting down and what Express mode pays to skip.
AUTORUN_COUPON_ITEM = 30064          # Quick Battle Coupon, the Express currency
AUTORUN_SECONDS_PER_TURN = 10        # the panel's own "+10s per turn"
AUTORUN_DEFAULT_TURNS = 10           # used until the stage has a clear record
STAMINA_ENERGY_TYPE = 1
AUTORUN_CHARGES_STAMINA = False       # see autorun_start


def stage_best_turns(state, stage_id):
    """Best recorded clear length, or None. Drives both the sweep's duration and the
    panel's "Stage Clear Record" -- which reads **-1 Turn(s)** while `bestrec` is empty."""
    rec = (state.get("bestrec") or {}).get(str(int(stage_id)))
    return int(rec) if rec else None


def record_stage_turns(state, stage_id, turns):
    """Keep the best (lowest) clear length for a stage."""
    if not turns or turns < 1:
        return
    rec = state.setdefault("bestrec", {})
    key = str(int(stage_id))
    if key not in rec or int(turns) < int(rec[key]):
        rec[key] = int(turns)


def autorun_duration(state, stage_id, count):
    """Seconds a sweep of `count` runs will take, by the panel's own arithmetic."""
    turns = stage_best_turns(state, stage_id) or AUTORUN_DEFAULT_TURNS
    return max(1, int(turns)) * AUTORUN_SECONDS_PER_TURN * max(1, int(count))


def autorun_start(state, stage_id, count, use_coupon, now):
    """Begin an offline sweep. -> (ok, reason). Charges up front, like the panel says."""
    row = dd.row("stage", int(stage_id)) or {}
    if not row:
        return False, f"unknown stage {stage_id}"
    if state.get("autorun"):
        return False, "a sweep is already running"
    count = max(1, int(count))
    # **A Quick Battle Coupon buys the SPEED, not the run.** Express still pays the
    # stage's own entry cost on top -- otherwise the express tab would be a way to run
    # a pass-gated dungeon without spending passes.
    if use_coupon and not spend_item(state, AUTORUN_COUPON_ITEM, count):
        return False, f"not enough Quick Battle Coupons ({count} needed)"

    # **Passes first, then STAMINA for the rest.** A daily dungeon's passes are free
    # runs, not a hard cap: with only 3 a day, refusing a 99-run sweep outright would
    # make the whole feature unusable on exactly the stages people want to sweep. Spend
    # what passes there are, and let stamina carry the remainder.
    on_passes = 0
    item_cost = stage_ap_cost(row)
    if item_cost:
        iid, per = item_cost
        on_passes = min(count, item_count(state, iid) // per) if per else count
        if on_passes:
            spend_item(state, iid, per * on_passes)
    remainder = count - on_passes
    # **Stamina is deliberately NOT charged**, so the remainder is currently free.
    # Ordinary runs do not charge it either (the stage-entry path only spends
    # `_ap_type 2` pass items), so taking it here would make a sweep cost more than
    # doing the same runs by hand. Flip AUTORUN_CHARGES_STAMINA to switch it on -- the
    # panel already quotes the cost and the arithmetic below is ready for it.
    if remainder and AUTORUN_CHARGES_STAMINA:
        per_ap = int(row.get("_ap") or 0)
        slot = state["energy"].setdefault(str(STAMINA_ENERGY_TYPE),
                                          {"energy": 0, "cap": 0})
        if int(slot.get("energy", 0)) < per_ap * remainder:
            return False, (f"not enough stamina ({per_ap * remainder} needed, "
                           f"{slot.get('energy', 0)} held)")
        slot["energy"] = int(slot["energy"]) - per_ap * remainder
    # Express skips the wait entirely -- that is what the coupon buys.
    secs = 0 if use_coupon else autorun_duration(state, stage_id, count)
    state["autorun"] = {"stage_id": int(stage_id), "count": count,
                        "starttime": int(now), "duetime": int(now) + secs}
    return True, ""


def autorun_json(state, now=None):
    """The `auto` dict of StageSyncData: Dictionary<int, AutoRunData>."""
    job = state.get("autorun")
    if not job:
        return {}
    return {str(job["stage_id"]): {"stage_id": job["stage_id"],
                                   "count": job["count"],
                                   "starttime": job["starttime"],
                                   "duetime": job["duetime"]}}


def autorun_due(state, now):
    """The job if its timer has elapsed, else None."""
    job = state.get("autorun")
    return job if job and int(now) >= int(job["duetime"]) else None


def autorun_runs_elapsed(job, now):
    """How many of a sweep's runs its timer has covered by `now` (capped at its count)."""
    count = int(job["count"])
    span = int(job["duetime"]) - int(job["starttime"])
    if span <= 0:
        return count
    done = int(count * max(0, int(now) - int(job["starttime"])) / span)
    return max(0, min(done, count))


def autorun_cancel(state):
    """Abandon a running sweep. -> the job that was dropped, or None. Nothing is
    refunded: the runs it already represents are paid for."""
    return state.pop("autorun", None)


# Kinds the rating evaluator could judge BEFORE 2026-08-16: everything else reported 0
# and so was never paid, however many times the stage was cleared.
LEGACY_RATING_KINDS = (1, 2, 3)


def migrate_rating_masks(state):
    """Clear rating bits that were stored as earned but never actually paid. -> count.

    Every clear used to write a flat 15 into `state["stages"][id]` -- all four stars --
    while only kinds 1/2/3 were ever evaluated or paid. Now that the mask decides what
    a clear owes, that legacy 15 would lock a player out of the grimoire fragments and
    posters they never received. So drop the bits for rows whose kind the old code
    could not judge, leaving them to be earned once, properly.

    Runs ONCE per account (guarded by a flag): re-running would clear bits that have
    since been legitimately earned, which would re-open the farm this closed.
    """
    if state.get("_rating_masks_migrated"):
        return 0
    fixed = 0
    for sid, mask in list((state.get("stages") or {}).items()):
        mask = int(mask or 0)
        row = bt.dd.row("stage", int(sid)) or {}
        keep = 0
        for i in range(4):
            cells = bt.dd.csv_ints(row.get(f"_rating_datas{i + 1}"))
            if cells and cells[0] in LEGACY_RATING_KINDS and (mask >> i) & 1:
                keep |= 1 << i
        if keep != mask:
            state["stages"][sid] = keep
            fixed += 1
    state["_rating_masks_migrated"] = 1
    return fixed


def stage_json(state, now=None):
    """PlayerStage.StageSyncData. `stages` maps stage id -> rating bitmask and is
    what GetStageRating reads; the other three dicts use LuaTableConverter and must
    be present or they deserialize to null.

    `bestrec` is the per-stage best clear length. Leaving it empty is why the auto-play
    panel reads "Stage Clear Record -1 Turn(s)" and cannot estimate a sweep's duration.
    `auto` carries the running sweep, if any."""
    return json.dumps({
        "entrance": {}, "stages": state["stages"],
        "bestrec": state.get("bestrec") or {},
        "auto": autorun_json(state, now),
        "weekday": 0,
    }, separators=(",", ":"))

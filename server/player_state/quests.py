"""Quests: counter stores, completion tracking, and reward claim plumbing.

Split out of the former monolithic core.py; depends only on .core.
"""


import json
import re
import battle as bt

from .core import (
    BP_STORAGE_EQUIPMENT,
    RUNE_ATTR_LEVEL,
    ITEM_ACTION_CURRENCY,
    ITEM_ACTION_ENERGY,
    SP_QUEST_COMPLETE,
    UNSUPPORTED_QUEST_TYPES,
    _quest_is_sp,
    add_char,
    bump_quest_counter,
    char_equips,
    grant_reward,
)


# ---- character rewards ("boxes" that are really casts) ---------------------
#
# **A quest whose `_item_id` is `_action 2` pays a CHARACTER, not a bag item.** Quest
# 31015 pays item 930 "★4勇進之隸魔 卡蓮" -- `_class 2`, `_action 2`, `_param1 2930` --
# and dropping that in the backpack is wrong twice over:
#
#   * `PlayerBackpack.GetItemSpace` (0x18EE554) has no case for `_action 2`, so
#     AddStorage files it in `_storageList` and NO category list. UIPrepareInventory
#     renders one category list per tab, so the item is INVISIBLE in game -- it cannot
#     be seen, tapped or opened. An item the client cannot file was never meant to be
#     held.
#   * `_param1` resolves to nothing in the pack (all 55 forms checked), because box
#     contents were live-ops server data -- which is exactly why the client has to ASK
#     for them (Backpack cmd 129).
#
# Live footage settles what should happen: claiming plays the single-pull character
# reveal and the cast is granted. So the item is a DISPLAY row for the reward popup and
# the character is the real payload.
#
# **`_action 1` is the real character item and its `_param1` IS the char id** -- 709 of
# the 710 such rows point at a genuine `char` row. `_action 2` is an indirection whose
# `_param1` (2930) resolves to nothing we hold, but the two rows carry the IDENTICAL
# Chinese `_itemName`, so the box maps onto the character item by name alone -- no fuzzy
# matching, no guessing which of the duplicate cast rows is playable:
#
#     930  action 2  param1 2930   CN '★4勇進之隸魔 卡蓮'  EN '★4 Caillen of Braveheart'
#  110984  action 1  param1 10981  CN '★4勇進之隸魔 卡蓮'  EN '★4 Braveheart Caillen'
#
# Match on the CHINESE name: the EN strings are assembled differently ("Caillen of
# Braveheart" vs "Braveheart Caillen") and would not join up. Live footage shows the
# reveal card reading "★4 Braveheart Caillen" -- item 110984's name, NOT 930's -- so the
# original server paid the character item, and the quest row's own `_item_id` is just
# the design-side stand-in. We return the resolved item id so the reward popup shows the
# same card the real game did.
#
# Name-matching by title+name against the `char` form was the first approach and is
# WRONG: Caillen has the playable 10981 plus rarity-5 AVG rows (166046) and rarity-1
# duplicates (3000891, 4000002, ...) sharing the title, and any tie-break over those is
# a guess. `_param1` is the answer the data already carries.
_CHAR_ITEM_ACTION = 1
_CHAR_BOX_ACTION = 2
_STAR_PREFIX = re.compile(r"^★(\d+)")


def _char_item_index():
    """{CN item name: item id} over every `_action 1` item that names a real cast."""
    idx = {}
    for iid, row in (bt.dd.rows("item") or {}).items():
        if row.get("_action") != _CHAR_ITEM_ACTION:
            continue
        if not bt.dd.row("char", row.get("_param1")):
            continue
        name = (row.get("_itemName") or "").strip()
        if name:
            idx.setdefault(name, int(iid))
    return idx


_char_item_index_cache = None


def char_reward_of(item_id):
    """-> (char id, star, display item id) if this reward is a cast, else None.

    Accepts either the character item itself or the box that shares its name. `star` is
    the ★N the item is sold as (110984/5/6 are the same cast at ★4/★5/★6), which is what
    the cast should be granted at -- NOT the char row's own `_rarity`.
    """
    global _char_item_index_cache
    row = bt.dd.row("item", int(item_id)) or {}
    action = row.get("_action")
    if action not in (_CHAR_ITEM_ACTION, _CHAR_BOX_ACTION):
        return None
    name = (row.get("_itemName") or "").strip()

    if action == _CHAR_BOX_ACTION:
        if _char_item_index_cache is None:
            _char_item_index_cache = _char_item_index()
        resolved = _char_item_index_cache.get(name)
        if resolved is None:
            return None
        return char_reward_of(resolved)

    char_id = row.get("_param1")
    if not bt.dd.row("char", char_id):
        return None
    m = _STAR_PREFIX.match(name)
    if not m:
        # 10 of the 710 carry no ★N; fall back to the cast's own rarity rather than
        # dropping the reward on the floor.
        star = int((bt.dd.row("char", char_id) or {}).get("_rarity") or 1)
    else:
        star = int(m.group(1))
    return int(char_id), star, int(item_id)



# ---- quests ---------------------------------------------------------------
# The newbie tutorial is driven by PlayerStage.CheckNewbieForceStep, which gates on
# NewbieForceDefine's STAGE_STEP1 = stage 1101, QUEST_STEP1 = quest 31001 ("clear
# story battle 1-1") and QUEST_STEP2 = quest 31002 ("pull gacha once"):
#
#   QuestHasCompleted(31002)          -> [-1,-1]  flow finished
#   GetStageRating(1101) == 0         -> [-1,-1]  no forced step  <- needs stage sync
#   CheckQuestWillbeCompleted(31001)  -> [1, 1]   announce the 1-1 clear
#   QuestHasCompleted(31001) and not CheckQuestWillbeCompleted(31002)
#                                     -> [1, 2] / [2, 0]   the GACHA step
#   otherwise                         -> [-1,-1]
#
# So the gacha explanation needs 31001 *completed* and 31002 not yet completable --
# no gacha implementation required for the step itself to appear.

# DesignQuestRow._case_id 1002 means "clear main story stage `_case_v1`". Inferred
# from quest 31001 (_case_id 1002, _case_v1 1101, named "完成 劇情戰鬥「1-1 普通」")
# and corroborated by all 516 rows carrying it being named "攻略主線劇情 (N)".
# Other condition types are not modelled, so only stage-clear quests complete.
QUEST_CASE_CLEAR_STAGE = 1002
# Which dictionary a completion has to go in is decided by **`_case_type` (row +0xA8)**,
# NOT `_type` (+0x94). PlayerQuest.QuestHasCompleted does `LDR W8,[X0,#0xA8]` and then
#   1 -> Quest_Datas.quests.ContainsKey(id)
#   2 -> SP_Quest_Datas.sp_quests[id].status == 1
#   anything else -> always false
# The two columns disagree often (459 rows are _type 2 / _case_type 1), and quest 21001
# -- the second quest clearing stage 1-1 satisfies -- is exactly such a row: routing it
# on _type filed it under sp_quests, where the client never looked, so it could never
# read as complete.
QUEST_CASE_TYPE_MAIN = 1


def complete_quests(state, quest_ids):
    """Mark quests completed because the player CLAIMED them.

    Clearing a stage does not complete its quest -- it only makes it *claimable*
    (`CheckQuestWillbeCompleted`, driven by GetQuestValue >= CaseCnt). The player
    then taps "Collect Reward" and the client sends PlayerQuest cmd 257 with the
    ids; only that turns into a real completion. Doing it server-side at stage-clear
    time skips the claim the newbie tutorial is waiting on.

    Returns ([(quest_id, item_id, item_cnt), ...], [new char uid, ...]) -- the first for
    the reward popup (cmd 513), the second so the caller can push Char `create` (529),
    which is what actually puts the cast in charDic and plays the reveal.
    """
    rewards = []
    new_chars = []
    rows = bt.dd.rows("quest")
    for qid in quest_ids:
        row = rows.get(int(qid))
        if not row:
            continue
        key = str(int(qid))
        if _quest_is_sp(row):
            # **setdefault is not enough.** Once bump_quest_counter has created the
            # entry to track progress, the key already exists, so setdefault silently
            # left status at 0 -- the client saw the quest as still claimable and kept
            # re-offering it (and re-paying the reward on every press).
            e = state["sp_quests"].setdefault(
                key, {"id": int(qid), "a_time": 0, "cnt": 0, "status": 0})
            e["cnt"] = max(int(e.get("cnt", 0)), row.get("_case_cnt") or 1)
            e["status"] = SP_QUEST_COMPLETE
        else:
            state["quests"].setdefault(key, 1)
        item_id, cnt = row.get("_item_id") or 0, row.get("_item_cnt") or 0
        char = char_reward_of(item_id) if item_id else None
        if char:
            # **A cast reward is granted, never bagged.** The design row's item is a
            # display stand-in the client cannot even file into an inventory tab; the
            # payload is the character. Report the RESOLVED item id so the popup shows
            # the card the live game showed.
            char_id, star, display_item = char
            for _ in range(max(1, int(cnt))):
                new_chars.append(add_char(state, char_id, star=star))
            rewards.append((int(qid), display_item, cnt))
        elif item_id and cnt:
            # Routed by `_action` -- 21001 pays 10000x item 2, which is Mira, not a
            # 10000-deep backpack stack.
            #
            # **A quest can also pay a BUNDLE.** Step 19 of Lucifer's Note pays item
            # 1200006 "Evolution TUT Bundle", `_action 2`: no inventory tab will hold
            # it (GetItemSpace has no case for `_action 2`) and EnqueItemPopupInfo
            # strips it from the reward popup, so claiming the goal handed over
            # something invisible and said nothing. grant_goods is the one place that
            # knows how to turn an item id into what it is actually worth -- bundles,
            # casts, random boxes and plain items alike -- so defer to it and report
            # what it says was paid.
            from .shop import grant_goods       # local: shop imports us, not the reverse
            uids, paid_id, paid_cnt = grant_goods(state, item_id, cnt)
            new_chars.extend(uids)
            rewards.append((int(qid), paid_id, paid_cnt))
        else:
            rewards.append((int(qid), item_id, cnt))
    return rewards, new_chars


# ---- case 5: "clear any stage of category N" ------------------------------
# `_case_id` 5 counts stage clears by DUNGEON FAMILY, with `_case_v1` naming the family.
# Read straight off the rows' own English text:
#     0  main story     1  Kizuna Tower     2  Starshard Temple    7  ANY stage
#     8  Guild Boss    21  Trainers Gym    22  Rank Up Abyss
#    23  Transcend Corridor               24  Treasure Raiders
# Nothing bumped this at all, which is why "Complete any Kizuna Quest 1 time" stayed at
# 0/1 however many Kizuna stages were cleared.
#
# A stage's family is its dmap's ROOT: `dmap._link`, or the dmap itself when `_link` is
# 0 (the daily dungeons are their own roots; story chapters and tower floors point up).
QUEST_CASE_CLEAR_CATEGORY = 5
CATEGORY_ANY = 7
STAGE_CATEGORY_ROOTS = {
    0: (1001, 1002, 1003, 1004, 1005, 1006),   # the six story chapters
    2: (40011,),                               # Starshard Railway, incl. the Temple
    21: (30004, 31004),                        # Trainers Gym (+ its "Double" variant)
    22: (30002, 31002),                        # Rank Up / Evolution Abyss
    23: (30014,),                              # Transcend Corridor
    24: (30003,),                              # Treasure Raiders
    # 8 (Guild Boss) has no dmap -- the guild subsystem does not exist yet.
}
# **A "Kizuna Quest" is NOT the Kizuna Tower.** The in-game panel titled "Kizuna
# Quests" lists one entry per cast ("Cupid's Envoy: Ravinia", "The Undaunted: Marilu",
# 0/4 CLEAR each), and those are dmaps 22001..22076 -- 57 of them, every one `_type 1`
# with `_link 0`, so each is its own root -- holding 564 stages numbered dmap*100 + n.
# The 24 dmaps actually NAMED "Kizuna Tower" (41001, 41101, … 43501) hold a separate
# 1440-stage stat grind ("Bond of TEC") that does not appear in the EN build at all.
# Both are credited here: the towers cost nothing to include and cannot fire while they
# are unreachable, and quest 51007 ("Complete any Kizuna Stage") reads as covering both.
KIZUNA_CATEGORY = 1
KIZUNA_QUEST_DMAP_LO, KIZUNA_QUEST_DMAP_HI = 22000, 23000
KIZUNA_TOWER_ROOT_NAME = "Kizuna Tower"

_root_category_cache = None


def _root_category_index():
    global _root_category_cache
    if _root_category_cache is None:
        idx = {}
        for cat, roots in STAGE_CATEGORY_ROOTS.items():
            for r in roots:
                idx[int(r)] = cat
        for did, row in (bt.dd.rows("dmap") or {}).items():
            did = int(did)
            if KIZUNA_QUEST_DMAP_LO <= did < KIZUNA_QUEST_DMAP_HI:
                idx[did] = KIZUNA_CATEGORY
            # The towers need a NAME test, not a range: 41401/41404/41407… sit in the
            # same span but are event maps ("The Deathblow", "Beauty Pageant").
            elif (row.get("_type") == 2
                    and (row.get("_name_en") or "").strip() == KIZUNA_TOWER_ROOT_NAME):
                idx[did] = KIZUNA_CATEGORY
        _root_category_cache = idx
    return _root_category_cache


def stage_category(stage_id):
    """Which case-5 family a stage belongs to, or None if we do not model it."""
    row = bt.dd.row("stage", int(stage_id)) or {}
    dmap_id = int(row.get("_dmap_id") or 0)
    dmap = bt.dd.row("dmap", dmap_id) or {}
    return _root_category_index().get(int(dmap.get("_link") or dmap_id))


def bump_stage_category_quests(state, stage_id):
    """Credit the case-5 counters a clear of `stage_id` satisfies. -> keys touched.

    Every clear advances the ANY family (`_case_v1` 7) as well as its own, which is
    what the "[Daily] Clear any stages N times" rows count. Bumping is all the server
    owes: the client rebuilds its claimable list from the counters and the player still
    claims through Quest cmd 257.
    """
    touched = list(bump_quest_counter(state, QUEST_CASE_CLEAR_CATEGORY,
                                      case_v1=CATEGORY_ANY))
    cat = stage_category(stage_id)
    if cat is not None and cat != CATEGORY_ANY:
        touched += bump_quest_counter(state, QUEST_CASE_CLEAR_CATEGORY, case_v1=cat)
    return touched


# ---- starshard upgrade goals (`_case_id` 21 / 26 / 27) ---------------------
#
# Three different questions about the same action, none of them a plain event count:
#
#   21  "Upgrade Starshards for N levels in total" (20701..20752) -- CUMULATIVE, so it
#       is the only one of the three that is a real increment.
#   26  "Level up any Starshard to N" (50143 -> 12, 50163 -> 15) -- the HIGH-WATER
#       level of the single best shard. Netherworld Note step 31 (quest 31030) is this
#       case with `_case_cnt` **3**, even though its English text says "+5"; the data
#       is what the client compares against.
#   27  "Upgrade any <cnt> Starshards to level +<v1>" (31037/31044/31049/31055/31062/
#       31069, 103646..103650) -- HOW MANY shards sit at level >= `_case_v1`, so `v1`
#       is a discriminator and every distinct threshold needs its own recompute.
#
# 26 and 27 are recomputed from the bag and stored with `to=` (a max, not an assign),
# because they are state rather than events -- see bump_quest_counter.
QUEST_CASE_RUNE_LEVELS_TOTAL = 21
QUEST_CASE_RUNE_BEST_LEVEL = 26
QUEST_CASE_RUNE_AT_LEVEL = 27


def _rune_levels(state):
    return [int((e.get("attr") or {}).get(RUNE_ATTR_LEVEL, 0))
            for e in state["backpack"].get(str(BP_STORAGE_EQUIPMENT), {}).values()]


def _at_level_thresholds():
    return {int(r.get("_case_v1") or 0) for r in bt.dd.rows("quest").values()
            if r.get("_case_id") == QUEST_CASE_RUNE_AT_LEVEL}


def bump_rune_upgrade_quests(state, gained_levels):
    """Credit cases 21/26/27 after a starshard gained `gained_levels`. -> keys touched."""
    touched = list(bump_quest_counter(state, QUEST_CASE_RUNE_LEVELS_TOTAL,
                                      amount=int(gained_levels)))
    levels = _rune_levels(state)
    touched += bump_quest_counter(state, QUEST_CASE_RUNE_BEST_LEVEL,
                                  to=max(levels, default=0))
    for threshold in sorted(_at_level_thresholds()):
        at = sum(1 for lv in levels if lv >= threshold)
        touched += bump_quest_counter(state, QUEST_CASE_RUNE_AT_LEVEL,
                                      to=at, case_v1=threshold)
    return touched


# ---- "clear a stage wearing a full set" (`_case_id` 2006) -------------------
#
# Quest 31029 is "Complete 1 battle with full-set Endearment Starshards", and it sat at
# 0/1 forever because nothing here knew the case. Case 2006 is really "clear a stage
# while <condition>", and its `_case_v1` is overloaded: the goal-chain rows name a
# **suit id** (1 = Endearment on 31029, 5 = Nightshade on 50175) while the ~40 "Activate
# <cast>'s Ex Break value +1, and complete any stage" rows put a soulmirror item id
# there instead. Passing an explicit `case_v1` keeps the two apart -- we only ever bump
# the suit we actually saw worn, so the Ex Break rows stay untouched (they need the
# soulmirror system's own hook, which does not exist yet).
#
# A suit comes off the ITEM row, not the instance: item `_param1` names the
# DesignEquipment row and that row's `_suitID` is the set. "Full set" is all SIX
# starshard slots (equips 0..5) carrying the same suit -- the six actions 111..116.
QUEST_CASE_CLEAR_WITH_SUIT = 2006
RUNE_SLOT_COUNT = 6


def _rune_suit(item_id):
    equip = bt.dd.row("equipment", (bt.dd.row("item", int(item_id)) or {}).get("_param1"))
    return (equip or {}).get("_suitID")


def full_suits_worn(state, char_uids):
    """-> {suit id} for which some cast in `char_uids` wears a COMPLETE 6-slot set."""
    by_uid = {e.get("uid"): e for e in
              state["backpack"].get(str(BP_STORAGE_EQUIPMENT), {}).values()}
    suits = set()
    for char_uid in char_uids:
        entry = state["roster"].get(str(char_uid))
        if not entry:
            continue
        worn = char_equips(entry)[:RUNE_SLOT_COUNT]
        if not all(worn):
            continue
        seen = {_rune_suit(by_uid[u]["iid"]) for u in worn if u in by_uid}
        if len(seen) == 1 and None not in seen:
            suits |= seen
    return suits


def bump_full_suit_quests(state, char_uids):
    """Credit case-2006 counters for every full set the party wore. -> keys touched."""
    touched = []
    for suit in sorted(full_suits_worn(state, char_uids)):
        touched += bump_quest_counter(state, QUEST_CASE_CLEAR_WITH_SUIT,
                                      case_v1=suit)
    return touched


def complete_stage_quests(state, stage_id):
    """Mark every stage-clear quest satisfied by clearing `stage_id`. Returns the
    ids newly completed, so the caller can decide whether a push is worthwhile.

    Completion is stored in TWO different places depending on the quest's
    **`_case_type`** (see QUEST_CASE_TYPE_MAIN above -- NOT `_type`, which is a
    different column that frequently disagrees):
      _case_type 1 -> QuestSyncQuestData.quests.ContainsKey(id)
      _case_type 2 -> SPQuestSyncQuestData.sp_quests[id].status == 1
    Filing a completion in the wrong one is silently ignored, which is what happened
    to 21001 (the other quest that clearing stage 1-1 satisfies).
    """
    newly = []
    for qid, row in bt.dd.rows("quest").items():
        if (row.get("_case_id") != QUEST_CASE_CLEAR_STAGE
                or row.get("_case_v1") != int(stage_id)):
            continue
        # Same scope limit as bump_quest_counter: never complete event/OFA/BP/special
        # stage-clear quests (a later stage does appear in OFA quest chains).
        if row.get("_type") in UNSUPPORTED_QUEST_TYPES:
            continue
        key = str(qid)
        if _quest_is_sp(row):
            if key in state["sp_quests"]:
                continue
            state["sp_quests"][key] = {"id": qid, "a_time": 0,
                                       "cnt": row.get("_case_cnt") or 1,
                                       "status": SP_QUEST_COMPLETE}
        else:
            if key in state["quests"]:
                continue
            state["quests"][key] = 1
        newly.append(qid)
    return newly
QUEST_CASE_SKILL_UP = 16        # "Complete Skill-up"     -> char_limit_up  (281)
QUEST_CASE_TRANSCEND = 18       # "Perform Transcend"     -> char_plus_up   (280)
QUEST_CASE_RANK_UP = 19         # "Perform Rank Up"       -> char_rank_up   (279)
QUEST_CASE_POWER_UP = 20        # "Perform Cast Power-up" -> char_level_up  (278)


def quest_json(state):
    """PlayerQuestData, the cmd-529 payload. It is an INCREMENTAL update: the
    handler merges `update_quests` into QuestSyncQuestData.quests and applies the
    remove_* lists, rather than replacing the collection, so re-sending the full set
    is harmless.

    Send EVERY field, even when empty. The handler applies each one only `if
    (field != null)`, and several PlayerQuest members it feeds have no lazy-init
    getter, so omitting a key leaves the live collection null and AnalysisQuest --
    which runs on every one of these replies -- throws a NullReference partway
    through. That abort leaves willbeCompletedQuestList half-rebuilt, which is what
    made the newbie flow read quest 31001 as "completable" rather than "completed"
    and send the player to PanelGoalQuest instead of the gacha step.
    """
    return json.dumps({
        # WIRE NAMES, from the [JsonProperty] thunks -- they are NOT the field names:
        #   update_db -> db_u    remove_db        -> db_r
        #   update_quests -> q_u remove_quests    -> q_r
        #   sp_quests -> sp_u    remove_sp_quests -> sp_r
        #   gq_db -> gq_db       gq_ow            -> gq_ow
        # Sending "update_quests" was silently ignored, so completions never reached
        # QuestSyncQuestData.quests and QuestHasCompleted stayed false forever.
        "db_u": {k: {"total": v} for k, v in state["quest_db"].items()},
        "db_r": [],
        "q_u": {k: v for k, v in state["quests"].items()},
        "q_r": [],
        "sp_u": dict(state["sp_quests"]), "sp_r": [],
        "gq_db": {}, "gq_ow": {},
    }, separators=(",", ":"))


def item_bucket(item_id):
    """Where grant_reward would put this item, without granting it. Callers use it to
    decide which syncs to push -- a grant the client is never told about may as well not
    have happened."""
    row = bt.dd.row("item", item_id) or {}
    action, param = row.get("_action"), row.get("_param1") or 0
    if action == ITEM_ACTION_CURRENCY and param:
        return "currency"
    if action == ITEM_ACTION_ENERGY and param:
        return "energy"
    return "backpack"

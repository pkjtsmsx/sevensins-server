"""Quests: counter stores, completion tracking, and reward claim plumbing.

Split out of the former monolithic core.py; depends only on .core.
"""


import json
import battle as bt

from .core import (
    ITEM_ACTION_CURRENCY,
    ITEM_ACTION_ENERGY,
    SP_QUEST_COMPLETE,
    UNSUPPORTED_QUEST_TYPES,
    _quest_is_sp,
    grant_reward,
)



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

    Returns [(quest_id, item_id, item_cnt), ...] for the reward popup (cmd 513).
    """
    rewards = []
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
        if item_id and cnt:
            # routed by _action -- 21001 pays 10000x item 2, which is Mira, not a
            # 10000-deep backpack stack
            grant_reward(state, item_id, cnt)
        rewards.append((int(qid), item_id, cnt))
    return rewards


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

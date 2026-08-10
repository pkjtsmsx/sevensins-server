#!/usr/bin/env python3
"""Rewind an account to a point in the newbie tutorial, for testing.

The flow is gated by PlayerStage.CheckNewbieForceStep (docs/GAME_SERVER.md §8):

    QuestHasCompleted(31002)            -> [-1,-1]  flow finished
    GetStageRating(1101) == 0           -> [-1,-1]  no forced step
    CheckQuestWillbeCompleted(31001)    -> [1, 1]   claim the 1-1 goal reward
    QuestHasCompleted(31001) and not CheckQuestWillbeCompleted(31002)
                                        -> [1, 2]   the GACHA step
So each stage below just removes the state that gates it.

  gacha   drop quest 31002 + the gacha counter -> back at the gacha step
  claim   also drop 31001                      -> back at the goal-reward claim
  battle  also clear stage 1101                -> tutorial battle runs again
  all     wipe roster/backpack/quests entirely -> brand new account

Usage: python reset_tutorial.py [gacha|claim|battle|all] [player_id]
"""
import sys

import player_state as ps

GACHA_QUEST, CLEAR_QUEST, TUTORIAL_STAGE = "31002", "31001", "1101"


def reset(stage="gacha", player_id="1000001"):
    st = ps.load(player_id)
    seeded = [ps._char_uid(player_id, i + 1) for i in range(len(st.get("team", [])))]

    if stage == "all":
        st["quests"], st["sp_quests"], st["quest_db"] = {}, {}, {}
        st["stages"] = {}
    else:
        st["quests"].pop(GACHA_QUEST, None)
        st["quest_db"] = {}
        if stage in ("claim", "battle"):
            st["quests"].pop(CLEAR_QUEST, None)
        if stage == "battle":
            st["stages"].pop(TUTORIAL_STAGE, None)

    st["gacha_count"] = 0
    st["gacha_pending"] = []
    # Story decisions lock permanently once made (see player_state.set_avg_choice), so a
    # rewind has to drop them as well -- otherwise the re-run replays each scene with the
    # old option already chosen and never offers the choice again.
    st["avg_choices"] = {}
    st["karma"] = {}
    # the gacha step needs the 10 Awaker Scrolls the 1-1 goal reward grants; at the
    # 'claim'/'battle'/'all' stages the player re-earns them, so start empty there
    st["roster"] = {u: e for u, e in st["roster"].items() if u in seeded}
    st["backpack"] = {}
    if stage == "gacha":
        ps.grant_item(st, ps.GACHA_COST_ITEM, ps.GACHA_COST_AMOUNT)

    ps.save(st)
    print(f"reset to '{stage}': quests={st['quests']} stages={st['stages']} "
          f"roster={len(st['roster'])} backpack={st['backpack'] or '{}'}")


if __name__ == "__main__":
    reset(*(sys.argv[1:] or ["gacha"]))

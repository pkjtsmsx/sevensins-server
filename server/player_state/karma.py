"""Karma: AVG decision rewards.

Split out of the former monolithic core.py; depends only on .core.
"""


from .core import (
    CUR_CASH,
)

# The karma curve was assumed server-side and stubbed at a flat 10 xp per rank. It is
# NOT server-side: Formula.GetCharFLVupNeedXP computes it client-side and the panel
# renders the result, so it was recoverable after all -- see char_flv_need_xp below.


# (avg id, option index) -> (currencyType, currencyValue, charID, fexp).
# Reconstructed from footage, not from data -- the option->reward mapping was
# server-side and the `avg` design form is not even in our pack. Observed in the
# tutorial, both paying the same character (the banner portrait does not change):
#
#   decision 1, top option    -> 5 ダイヤ, +10 karma   ("Up!")
#   decision 2, second option -> 10 ダイヤ, +20 karma  ("Big Up!")
#
# Those banner labels are an independent check on the decompile: OptionButton.SetReward
# picks "Up!" under 20 and "Big Up!" from 20, which is exactly what the two clips show.
#
#   decision 3, second option -> 15 ダイヤ, +100 karma ("Super Up!" in that build; our
#                                pack's text 10086 says "Ultimate Up!" -- same >=100
#                                branch, the EN wording just differs between builds)
#
# Those banner labels are an independent check on the decompile: OptionButton.SetReward
# picks "Up!" under 20, "Big Up!" from 20 and the third string from 100, which is
# exactly what the three clips show.
#
# The payout is **hardcoded per (scene, option)** -- it is not derivable from the option
# index or the scene ordinal, so the table below has to be rebuilt from video and each
# row needs its own clip. The three observations line up with nothing in particular:
# decisions 1/2/3 paid 5/10/15 gems while the options picked were 1/2/2, so the currency
# is not a function of either on its own. Footage only ever shows the branch that was
# taken, so an option's siblings stay unknown until someone picks them.
#
# **A choice is permanent per difficulty.** Replaying a stage on the same difficulty
# re-shows the scene with the previous option already locked in; only a different
# difficulty lets you pick again. That is what `avg_choices` records, and it is fed back
# to the client as intargs[0] of the AVG sync reply (see titan_server). It also settles
# the payout question: a scene pays the FIRST time it is decided and never again.
KARMA_TUTORIAL_CHAR = 11001            # Jacqueline; the portrait matches her, unconfirmed

# Observed in the tutorial, as (decision, option picked, gems, fexp). These become
# KARMA_REWARDS entries as soon as a run tells us the avg ids -- the handler logs them.
KARMA_OBSERVED = [
    (1, 1, 5, 10),
    (2, 2, 10, 20),
    (3, 2, 15, 100),
]
KARMA_REWARDS = {
    # (avg_id, option): (currency type, amount, char id, fexp)
    #
    # The tutorial's three decisions, in story order. The avg ids come from the AVG
    # *sync* line in our own server log (10102 -> 10103 -> 10107) and the option indices
    # from a playthrough that picked 1st / 2nd / 2nd -- exactly the branches the clips
    # show, which is what lets the amounts be attached to specific ids. Option indices
    # are 0-based.
    (10102, 0): (CUR_CASH, 5, KARMA_TUTORIAL_CHAR, 10),
    (10103, 1): (CUR_CASH, 10, KARMA_TUTORIAL_CHAR, 20),
    (10107, 1): (CUR_CASH, 15, KARMA_TUTORIAL_CHAR, 100),
}
# Until an entry exists, pay the smallest observed tier rather than nothing, so an
# undocumented decision still moves the story and still reads as a reward.
KARMA_DEFAULT = (CUR_CASH, 5, KARMA_TUTORIAL_CHAR, 10)


def karma_reward(avg_id, option):
    """(currencyType, amount, charID, fexp) for a decision, or the default."""
    return KARMA_REWARDS.get((int(avg_id), int(option)), KARMA_DEFAULT)


def avg_choice(state, avg_id):
    """The 0-BASED option already locked in for this scene, or None if never decided.

    None rather than 0, because option 0 is a real answer -- the first button -- and
    conflating the two is what let a decided scene re-open. See avg_choice_wire.
    """
    return state.setdefault("avg_choices", {}).get(str(avg_id))


def avg_choice_wire(state, avg_id):
    """intargs[0] of the AVG sync reply: the locked option, **1-BASED**, 0 = undecided.

    `AvgUIOptions.UpdateAVGOptionLockState` reads it as

        v13 = _replayMode ? 0 : mOptionTag[0]
        if (v13 <= 0):  lock everything, then unlock by the digits of mOptionTag[1]
        else:           v15 = 10^(v13 - 1);  unlock ONLY btnOptions[digit - 1]

    so it is a 1-based position and 0 means "nothing chosen" -- the same 1-based
    convention as the unlock digits, which index `_btnOptions[digit - 1]`.

    The request side is 0-BASED (`RequestServerAvgSelectOption` sends
    [avgID, optionIndex]), so the two directions disagree and the stored value has to be
    shifted on the way out. Echoing it raw broke both cases: picking the FIRST option
    sent 0 and re-opened the whole scene, and picking any other locked in the option
    before the one actually chosen.
    """
    chosen = avg_choice(state, avg_id)
    return 0 if chosen is None else int(chosen) + 1


def set_avg_choice(state, avg_id, option):
    """Record a decision. Returns False if this scene was already decided, in which case
    the reward must NOT be paid again -- the client re-shows the locked-in option."""
    seen = state.setdefault("avg_choices", {})
    if str(avg_id) in seen:
        return False
    seen[str(avg_id)] = int(option)
    return True


def karma_reward(avg_id, option):
    return KARMA_REWARDS.get((int(avg_id), int(option)), KARMA_DEFAULT)


def grant_currency(state, cur_type, amount):
    """Credit a CurrencyType directly. The AVG reply names a currency type rather than
    an item id, so this is the one grant path that does not go through a design row."""
    key = str(cur_type)
    state["currency"][key] = int(state["currency"].get(key, 0)) + amount
    return state["currency"][key]

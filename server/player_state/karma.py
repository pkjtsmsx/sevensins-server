"""Karma: AVG decision rewards.

Split out of the former monolithic core.py; depends only on .core.
"""

import re

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
KARMA_CHAPTER2_CHAR = 10981            # Caillen

# Which cast a chapter's decisions pay karma to, keyed by the stage BOOK the scene
# belongs to. The chapter is resolved from the design data rather than from the avg id's
# digits: `_avg_stage_index` maps every avg id back to the stage that plays it, so this
# keeps working if the numbering is not as regular as it looks.
#
# Chapter 1 is Jacqueline (the tutorial portrait). Chapter 2 is Caillen -- she is the
# cast the chapter is actually about, and paying its karma to Jacqueline was just the
# fallback showing through.
KARMA_CHAPTER_CHAR = {
    1: KARMA_TUTORIAL_CHAR,
    2: KARMA_CHAPTER2_CHAR,
}

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


_avg_stage_cache = None


def _avg_stage_index():
    """{avg id: stage id} built from every stage row's four AVG columns.

    Derived rather than pattern-matched on the id: 10103 belongs to stage 1101 and 20901
    to 2109, which LOOKS like a simple prefix rule, but the columns are the authority and
    a scene shared between stages would break the guess.
    """
    global _avg_stage_cache
    if _avg_stage_cache is None:
        import battle as _bt
        out = {}
        for sid, row in (_bt.dd.rows("stage") or {}).items():
            for col in ("_preAVG_datas", "_beforeBattleAVG_datas",
                        "_afterBattleAVG_datas", "_interludeAVG_datas"):
                for tok in str(row.get(col) or "").split(","):
                    tok = tok.strip()
                    if tok.isdigit() and int(tok):
                        out.setdefault(int(tok), int(sid))
        _avg_stage_cache = out
    return _avg_stage_cache


def avg_chapter(avg_id):
    """The story chapter a decision belongs to.

    The stage index is the authority (stage ids are <chapter><nn>, so 2109 -> 2), but it
    only lists the scenes a stage ENTERS at -- scenes chain onward from there, so a mid
    -chain id like 10102 appears nowhere in the columns even though the client plays it.
    For those, fall back to the id's own layout: avg ids are
    <chapter><stage-in-chapter><seq>, i.e. 10103 -> chapter 1 and 20901 -> chapter 2.
    """
    stage = _avg_stage_index().get(int(avg_id))
    if stage is not None:
        return int(stage) // 1000
    return int(avg_id) // 10000 or None


# ---- portrait resolution by speaker ---------------------------------------------
#
# A finer source than the per-chapter mapping below, contributed by a server user and
# re-verified here against our own pack (125 + 3 of 171, reproduced exactly).
#
# **It changes nothing on its own.** The chain needs scene -> role -> charID, and only
# the SECOND hop is derivable: nothing in the pack links an avg id to the role whose
# portrait its banner shows. `quest.json` has `_avg_id` and `_char_id` but only 2 rows
# carry an `_avg_id`; `tutorial.json`'s ids are the 199xxx range; `stage.json` lists
# scenes but names only mob groups; `avg_role.json` has no charID column; the `avg` form
# is not shipped at all. So KARMA_SCENE_ROLE starts empty and is filled by observation
# -- once a run shows "scene 10102 displays role N", one line here resolves the
# portrait with no further data work.
#
# Until then every scene falls through to the per-chapter mapping, which is why that is
# kept rather than replaced: it already pays chapter 2 to Caillen, and the contributed
# version had no chapter step at all.
KARMA_SCENE_ROLE = {
    # avg_id: avg_role id whose portrait the banner shows. Empty until confirmed.
}

# avg_role id -> charID for speakers the name join cannot reach. Add a row ONLY on a
# confirmed identification, never because an English translation looks like a unit's
# name: role 3401 is ライエラ and role 4101 is レイラ, two different speakers in two
# different sprite folders that both render as "Layla" in EN. ライエラ has no char row
# at all, so pointing 3401 at Layla's 10701 would put the wrong face on the banner.
#
# Most story speakers legitimately have no unit -- of the roles the join misses, most
# are `npc*` extras. A missing entry is the normal case, not a gap to fill by guessing.
ROLE_CHAR_OVERRIDES = {
    # Halsey & Ashley in unison (folder co009_01). The unit is filed under the FAMILY
    # name -- char 10541 is ガルシア姉妹 / "Garcias" -- so no given-name join reaches it.
    2501: 10541,
}

_role_char_cache = None
_QUALIFIER = re.compile(r"[（(].*?[)）]")


def _strip_qualifier(name):
    return _QUALIFIER.sub("", str(name or "")).strip()


def _role_char_map():
    """{avg_role id: charID}, by name join. Lazy, cached, and never raises.

    Two passes: `_roleName` == `char._name_jp`, then the same with a （qualifier）
    stripped -- セシリア（学者）/（無頼）/（少女） are all roles for セシリア.

    Keyed on `_group`, NOT `_id`: `_id` alone resolves Lucifer to 10000, her "Bunrei"
    variant, instead of 10001. And one name can belong to several units (シャルル is both
    10551 and 20551; each demon lord has alt-costume rows in the 20xxx range), so the
    lowest id wins -- deterministically the base version rather than whichever row dict
    iteration reached first. State an alt costume in ROLE_CHAR_OVERRIDES if a scene ever
    needs one.

    Best-effort by design: a karma payout must not fail because a design form is
    missing, so any error degrades to whatever resolved before it.
    """
    global _role_char_cache
    if _role_char_cache is not None:
        return _role_char_cache
    mapping = {}
    try:
        import battle as _bt          # lazy, as _avg_stage_index does
        by_jp = {}
        for cid, row in (_bt.dd.rows("char") or {}).items():
            jp = row.get("_name_jp")
            if not jp:
                continue
            g = int(row.get("_group") or cid)
            if jp not in by_jp or g < by_jp[jp]:
                by_jp[jp] = g
        for rid, row in (_bt.dd.rows("avg_role") or {}).items():
            nm = row.get("_roleName")
            cid = by_jp.get(nm) or by_jp.get(_strip_qualifier(nm))
            if cid:
                mapping[int(rid)] = int(cid)
    except Exception:                       # noqa: BLE001 -- never break a payout
        pass
    mapping.update(ROLE_CHAR_OVERRIDES)     # hand-confirmed rows always win
    _role_char_cache = mapping
    return mapping


def role_char(role_id, default=None):
    """charID for an avg_role id, or `default` when the name join finds nothing."""
    if role_id is None:
        return default
    return _role_char_map().get(int(role_id), default)


def karma_char_for(avg_id):
    """Which cast a decision pays karma to.

    The scene's confirmed speaker wins when there is one; otherwise the chapter
    mapping, which is what actually resolves every scene today.
    """
    by_role = role_char(KARMA_SCENE_ROLE.get(int(avg_id)))
    if by_role:
        return by_role
    return KARMA_CHAPTER_CHAR.get(avg_chapter(avg_id), KARMA_TUTORIAL_CHAR)


def karma_reward(avg_id, option):
    """(currencyType, amount, charID, fexp) for a decision, or the default.

    The character is resolved per CHAPTER even when the amounts fall back to the
    default, so an undocumented chapter-2 decision still pays the right cast.
    """
    cur, amount, char_id, fexp = KARMA_REWARDS.get((int(avg_id), int(option)),
                                                   KARMA_DEFAULT)
    return cur, amount, karma_char_for(avg_id), fexp


# The identity unlock mask: digit at position i has the value i+1, so option N sits at
# position N-1 and `tag[1] / 10^(N-1) % 10 == N`. Zeroing a digit removes that option
# from the unlock set (and from the "already chosen" test), which is how an option taken
# on another difficulty is locked out.
AVG_UNLOCK_IDENTITY = 4321
AVG_OPTION_SLOTS = 4


def _avg_record(state, avg_id):
    """{difficulty: option} for one scene, migrating the old flat format.

    Choices used to be stored as a bare `{avg_id: option}`, which could not express the
    per-difficulty rule at all. An old value is read as difficulty 1.
    """
    seen = state.setdefault("avg_choices", {})
    rec = seen.get(str(avg_id))
    if rec is None:
        return {}
    if not isinstance(rec, dict):
        rec = {"1": int(rec)}
        seen[str(avg_id)] = rec
    return rec


def avg_choice(state, avg_id, difficulty=1):
    """The 0-BASED option locked in for this scene ON THIS DIFFICULTY, or None.

    None rather than 0, because option 0 is a real answer -- the first button -- and
    conflating the two is what let a decided scene re-open.
    """
    rec = _avg_record(state, avg_id)
    v = rec.get(str(int(difficulty)))
    return None if v is None else int(v)


def avg_options_taken(state, avg_id, exclude_difficulty=None):
    """The 0-based options already used on OTHER difficulties."""
    rec = _avg_record(state, avg_id)
    skip = None if exclude_difficulty is None else str(int(exclude_difficulty))
    return {int(v) for k, v in rec.items() if k != skip}


def avg_sync_tags(state, avg_id, difficulty=1):
    """-> (tag0, tag1), the two values HandleAVGSyncReplyCmd stores as _avgRecord[0..1].

    `UpdateAVGOptionLockState` has THREE branches, and which one runs decides both what
    is selectable AND whether the decision pays:

        v13 = _replayMode ? 0 : tag[0]
        if (v13 <= 0):                      lock all, UNLOCK the digits of tag[1]
            _skipPerform = 1
        else:
            v17 = tag[1] / 10^(v13-1) % 10
            if v17 >= 1:                    lock all, UNLOCK only btnOptions[v17-1]
                _skipPerform = 1
            else:                           LOCK the digits of tag[1]
                _skipPerform = 0

    and `_skipPerform` is the whole ballgame, because `OnBtnClick` dispatches
    AvgUIOptionsEvent **3** (`OnAvgOptionsSkipover` -- no request, no reward) when it is
    set, and **1** (`OnAvgOptionsSelected` -> RequestServerAvgSelectOption -> the reward
    banner) when it is not. Event types confirmed from PanelAvg.Init's AddListener calls.

    So there are exactly two shapes worth sending:

      * **decided on this difficulty** -> (chosen+1, identity mask). The digit at
        position chosen is chosen+1, so v17 >= 1: only that option shows and the click
        skips over without paying again. This is the replay behaviour.
      * **not yet decided here** -> tag[1] carries ONLY the digits of options used on
        other difficulties (a LOCK-OUT list), and tag[0] points at a position whose
        digit is 0 so that v17 == 0 and _skipPerform stays 0. The untried options are
        selectable and the pick pays.

    tag[1]'s meaning therefore inverts between the branches -- unlock-list in the first
    two, lock-list in the third -- which is why a single "unlock mask" cannot serve both.
    An identity 4321 makes v17 >= 1 for every tag[0], so it always takes the skip path:
    that is what silenced the reward popups.
    """
    chosen = avg_choice(state, avg_id, difficulty)
    if chosen is not None:
        return chosen + 1, AVG_UNLOCK_IDENTITY
    taken = avg_options_taken(state, avg_id, exclude_difficulty=difficulty)
    # Lock-out list: option o sits as digit (o+1) at position o, matching the identity
    # layout so the client's `digit - 1` lands back on the right button.
    tag1 = sum((o + 1) * (10 ** o) for o in taken if 0 <= o < AVG_OPTION_SLOTS)
    # Any position whose digit is 0 forces v17 == 0, keeping _skipPerform clear. With at
    # most three options used, the fourth slot is always free.
    tag0 = next(p for p in range(AVG_OPTION_SLOTS)
                if tag1 // 10 ** p % 10 == 0) + 1
    return tag0, tag1


def avg_choice_wire(state, avg_id, difficulty=1):
    """tag[0] alone -- kept for callers that only need the locked option."""
    return avg_sync_tags(state, avg_id, difficulty)[0]


def set_avg_choice(state, avg_id, option, difficulty=1):
    """Record a decision for one scene ON ONE DIFFICULTY.

    -> False if this scene was already decided on this difficulty, in which case the
    reward must NOT be paid again (the client re-shows the locked-in option).

    A scene pays once PER DIFFICULTY, not once ever: the stage has three difficulties and
    the scene three options, so a full clear is three different decisions, and
    KARMA_REWARDS is keyed by (scene, option) precisely because each pays its own amount.
    """
    seen = state.setdefault("avg_choices", {})
    rec = _avg_record(state, avg_id)
    key = str(int(difficulty))
    if key in rec:
        return False
    rec[key] = int(option)
    seen[str(avg_id)] = rec
    return True


def grant_currency(state, cur_type, amount):
    """Credit a CurrencyType directly. The AVG reply names a currency type rather than
    an item id, so this is the one grant path that does not go through a design row."""
    key = str(cur_type)
    state["currency"][key] = int(state["currency"].get(key, 0)) + amount
    return state["currency"][key]

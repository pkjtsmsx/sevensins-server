"""Karma: AVG decision rewards.

Split out of the former monolithic core.py; depends only on .core.
"""

import re

import settings

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
KARMA_TUTORIAL_CHAR = 11001            # Jacqueline -- also the fallback for any chapter
                                       # not yet listed below

# Which cast a chapter's decisions pay karma to, keyed by the stage BOOK the scene
# belongs to. The chapter is resolved from the design data rather than from the avg id's
# digits: `_avg_stage_index` maps every avg id back to the stage that plays it, and the
# `avg_id // 10000` shortcut is WRONG from chapter 3 on (see avg_chapter).
#
# **Every chapter pays ONE cast -- its antagonist -- for all of its decisions**, which is
# why this is keyed by chapter and not by scene. Confirmed in play: all three chapter-1
# decisions pay Jacqueline even though she speaks in only one of them (10103 has no
# Jacqueline lines at all).
#
# **This is not derivable and must be observed.** Two derivations were tried and both are
# disproven; do not retry them:
#   * "the most-spoken non-protagonist cast in the scene" -- fails chapter 1 outright,
#     per the 10103 case above.
#   * "the chapter's boss" -- chapter 1's last stage does field Jacqueline, but chapter
#     3's fields Noach while its decisions pay Matina, and chapter 3's decision stages
#     carry only generic mobs.
# `tools/avg_decisions.py` lists all 96 decision scenes so a run can confirm a chapter
# quickly; ~3 per chapter, and chapters 7, 23 and 33 have none.
KARMA_CHAPTER_CHAR = {
    1: 11001,      # Jacqueline, "Eccentric Inventor"  -- confirmed
    2: 10981,      # Caillen,    "Knight of Sincerity" -- confirmed
    3: 10821,      # Matina,     "Conflagration"       -- confirmed
    4: 11021,      # Noach,      "Scrutiny of Flesh"   -- confirmed
    5: 10971,      # Aura,       "Calamity"            -- confirmed
    6: 10991,      # Draco,      "Blazewind"           -- confirmed
    # Chapters 8..34 still fall back to Jacqueline and are therefore WRONG. Chapters 7,
    # 23 and 33 need no entry -- they carry no decisions at all.
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


# ---- reward tiers ---------------------------------------------------------------
#
# The banner has three grades and `OptionButton.SetReward` picks between them purely by
# the karma amount: "Up!" under 20, "Big Up!" from 20, "Ultimate Up!" from 100. The three
# payouts are chosen to land one in each band, so the banner grades itself.
KARMA_TIERS = {
    1: (CUR_CASH, 5, 10),      # "Up!"
    2: (CUR_CASH, 10, 20),     # "Big Up!"
    3: (CUR_CASH, 15, 100),    # "Ultimate Up!"
}

# **`KARMA_TIERS` IS the balance knob.** Change a gem amount here and every decision in
# the game follows, the observed ones included -- `karma_reward` uses an observation only
# to learn which GRADE an option is, then pays the amount above. The retail record in
# `KARMA_OBSERVED` / `KARMA_REWARDS` is preserved as evidence rather than as the live
# number. Setting 25/50/100 here is the rescale a server user proposed.
#
# **Only the gems are free.** `OptionButton.SetReward` picks the banner from the karma
# amount alone, with the bands hardcoded client-side at 20 and 100, so 10/20/100 are
# chosen to sit one per band. Changing those collapses the grades into a single banner.

# Which grade each OPTION pays. Every one of the 96 decision scenes has exactly three
# options, so they map one-to-one onto the three grades.
#
# **The real rule is how CRUEL the option is** -- Lucifer is a demon king, so the meanest
# choice pays the most and the kindest the least. That is the semantic to fill in, and it
# is not derivable from the option text with any confidence yet: of the three retail
# payouts on record it is legible in only one (10102's "Take my tickle attack!" is the
# gentlest of its three and did pay the lowest), while 10103's options are a guessing
# game with no cruelty axis at all.
#
# So until a scene is observed, its three options are dealt one grade each in an
# ARBITRARY order. Arbitrary but NOT random at runtime: the shuffle is seeded by avg id
# when this table is generated, so an option always pays the same thing. A payout is
# recorded once and must survive a reload, a save restore and a replay on another
# difficulty -- rolling per call would let one decision pay differently depending on when
# it was asked.
#
# **To correct a scene, add its real values to KARMA_REWARDS**; those win outright and
# are the record of what the retail server actually paid. An observed option is also
# PINNED when this table is generated, and the scene's other two options are dealt the
# grades that remain -- so a partly-observed scene still pays one of each grade rather
# than doubling up on the one that happens to be known.
#
# Generated by tools/avg_decisions.py from the scene scripts in data_avg_<hash>.ab. The
# set is static, so it is baked rather than re-read at runtime.
KARMA_DECISION_TIER = {
    (10102, 0): 1, (10102, 1): 3, (10102, 2): 2,   # observed
    (10103, 0): 1, (10103, 1): 2, (10103, 2): 3,   # observed
    (10107, 0): 2, (10107, 1): 3, (10107, 2): 1,   # observed
    (20901, 0): 3, (20901, 1): 2, (20901, 2): 1,
    (20902, 0): 3, (20902, 1): 2, (20902, 2): 1,
    (21001, 0): 3, (21001, 1): 1, (21001, 2): 2,
    (21401, 0): 2, (21401, 1): 3, (21401, 2): 1,
    (21402, 0): 2, (21402, 1): 3, (21402, 2): 1,
    (21601, 0): 3, (21601, 1): 1, (21601, 2): 2,
    (22201, 0): 1, (22201, 1): 3, (22201, 2): 2,
    (22301, 0): 1, (22301, 1): 3, (22301, 2): 2,
    (22302, 0): 2, (22302, 1): 3, (22302, 2): 1,
    (22303, 0): 3, (22303, 1): 1, (22303, 2): 2,
    (30703, 0): 3, (30703, 1): 2, (30703, 2): 1,
    (31001, 0): 2, (31001, 1): 3, (31001, 2): 1,
    (31003, 0): 3, (31003, 1): 1, (31003, 2): 2,
    (31501, 0): 3, (31501, 1): 1, (31501, 2): 2,
    (31502, 0): 3, (31502, 1): 1, (31502, 2): 2,
    (31601, 0): 2, (31601, 1): 3, (31601, 2): 1,
    (40801, 0): 1, (40801, 1): 2, (40801, 2): 3,
    (40901, 0): 2, (40901, 1): 1, (40901, 2): 3,
    (41001, 0): 2, (41001, 1): 3, (41001, 2): 1,
    (41101, 0): 2, (41101, 1): 1, (41101, 2): 3,
    (41501, 0): 2, (41501, 1): 1, (41501, 2): 3,
    (41901, 0): 1, (41901, 1): 3, (41901, 2): 2,
    (42302, 0): 2, (42302, 1): 1, (42302, 2): 3,
    (42401, 0): 1, (42401, 1): 2, (42401, 2): 3,
    (42601, 0): 2, (42601, 1): 3, (42601, 2): 1,
    (50502, 0): 2, (50502, 1): 1, (50502, 2): 3,
    (50803, 0): 1, (50803, 1): 2, (50803, 2): 3,
    (51001, 0): 1, (51001, 1): 3, (51001, 2): 2,
    (51602, 0): 1, (51602, 1): 3, (51602, 2): 2,
    (51701, 0): 2, (51701, 1): 1, (51701, 2): 3,
    (51801, 0): 1, (51801, 1): 2, (51801, 2): 3,
    (52402, 0): 2, (52402, 1): 1, (52402, 2): 3,
    (52501, 0): 2, (52501, 1): 3, (52501, 2): 1,
    (60702, 0): 1, (60702, 1): 2, (60702, 2): 3,
    (60801, 0): 3, (60801, 1): 1, (60801, 2): 2,
    (60901, 0): 2, (60901, 1): 3, (60901, 2): 1,
    (61501, 0): 2, (61501, 1): 1, (61501, 2): 3,
    (61801, 0): 3, (61801, 1): 1, (61801, 2): 2,
    (61901, 0): 3, (61901, 1): 1, (61901, 2): 2,
    (62101, 0): 1, (62101, 1): 2, (62101, 2): 3,
    (62301, 0): 1, (62301, 1): 2, (62301, 2): 3,
    (62901, 0): 3, (62901, 1): 2, (62901, 2): 1,
    (70502, 0): 2, (70502, 1): 1, (70502, 2): 3,
    (70801, 0): 2, (70801, 1): 1, (70801, 2): 3,
    (70803, 0): 2, (70803, 1): 1, (70803, 2): 3,
    (71201, 0): 2, (71201, 1): 1, (71201, 2): 3,
    (71603, 0): 3, (71603, 1): 1, (71603, 2): 2,
    (71801, 0): 1, (71801, 1): 3, (71801, 2): 2,
    (72101, 0): 2, (72101, 1): 1, (72101, 2): 3,
    (72501, 0): 1, (72501, 1): 2, (72501, 2): 3,
    (72502, 0): 3, (72502, 1): 1, (72502, 2): 2,
    (80601, 0): 2, (80601, 1): 3, (80601, 2): 1,
    (80701, 0): 2, (80701, 1): 3, (80701, 2): 1,
    (81003, 0): 3, (81003, 1): 2, (81003, 2): 1,
    (81301, 0): 3, (81301, 1): 1, (81301, 2): 2,
    (81701, 0): 1, (81701, 1): 3, (81701, 2): 2,
    (82001, 0): 2, (82001, 1): 3, (82001, 2): 1,
    (82101, 0): 1, (82101, 1): 3, (82101, 2): 2,
    (82201, 0): 1, (82201, 1): 3, (82201, 2): 2,
    (82702, 0): 3, (82702, 1): 2, (82702, 2): 1,
    (91901, 0): 3, (91901, 1): 1, (91901, 2): 2,
    (92001, 0): 1, (92001, 1): 2, (92001, 2): 3,
    (92002, 0): 3, (92002, 1): 1, (92002, 2): 2,
    (92901, 0): 2, (92901, 1): 3, (92901, 2): 1,
    (93001, 0): 2, (93001, 1): 1, (93001, 2): 3,
    (93002, 0): 2, (93002, 1): 3, (93002, 2): 1,
    (100501, 0): 2, (100501, 1): 3, (100501, 2): 1,
    (100601, 0): 2, (100601, 1): 1, (100601, 2): 3,
    (100801, 0): 3, (100801, 1): 2, (100801, 2): 1,
    (102701, 0): 1, (102701, 1): 3, (102701, 2): 2,
    (103101, 0): 3, (103101, 1): 2, (103101, 2): 1,
    (103202, 0): 3, (103202, 1): 2, (103202, 2): 1,
    (104501, 0): 3, (104501, 1): 1, (104501, 2): 2,
    (104601, 0): 2, (104601, 1): 1, (104601, 2): 3,
    (104701, 0): 2, (104701, 1): 1, (104701, 2): 3,
    (110101, 0): 1, (110101, 1): 2, (110101, 2): 3,
    (110902, 0): 2, (110902, 1): 1, (110902, 2): 3,
    (111101, 0): 2, (111101, 1): 3, (111101, 2): 1,
    (111801, 0): 3, (111801, 1): 1, (111801, 2): 2,
    (112201, 0): 3, (112201, 1): 1, (112201, 2): 2,
    (112801, 0): 2, (112801, 1): 3, (112801, 2): 1,
    (113001, 0): 3, (113001, 1): 2, (113001, 2): 1,
    (113301, 0): 3, (113301, 1): 2, (113301, 2): 1,
    (113501, 0): 2, (113501, 1): 1, (113501, 2): 3,
    (113701, 0): 3, (113701, 1): 2, (113701, 2): 1,
    (114901, 0): 3, (114901, 1): 1, (114901, 2): 2,
    (115101, 0): 1, (115101, 1): 3, (115101, 2): 2,
    (121402, 0): 1, (121402, 1): 3, (121402, 2): 2,
    (121501, 0): 3, (121501, 1): 2, (121501, 2): 1,
    (121601, 0): 1, (121601, 1): 2, (121601, 2): 3,
    (123901, 0): 1, (123901, 1): 2, (123901, 2): 3,
    (124001, 0): 2, (124001, 1): 3, (124001, 2): 1,
    (124201, 0): 3, (124201, 1): 2, (124201, 2): 1,
}


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
    idx = _avg_stage_index()
    stage = idx.get(int(avg_id))
    if stage is not None:
        return int(stage) // 1000

    # The old fallback was `avg_id // 10000`, and it is WRONG from chapter 3 onward:
    # avg ids are allocated CONTINUOUSLY, not restarted per chapter. Chapter 2 runs
    # 20101..21001 and chapter 3 carries straight on at 21101..22003, so the leading
    # digits say "2" for both. Measured across the campaign it disagreed with the stage
    # on 1,297 of 1,680 scenes -- it happened to be right only for chapters 1 and 2,
    # which is exactly the range anyone would have spot-checked.
    #
    # Interpolate instead: ids ascend with the story, so the nearest indexed scene at or
    # below this one is in the same chapter. That needs no numbering assumption beyond
    # "ids increase", which the data does support.
    below = [a for a in idx if a <= int(avg_id)]
    if below:
        return int(idx[max(below)]) // 1000
    return None


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


_dmap_char_cache = None
# Most chapter portraits live under a `coNNN_01` folder, but some use `chNNN_01`
# (chapter 34 is `ch018_01`), so both prefixes have to match.
_ATLAS_FOLDER = re.compile(r"((?:co|ch)\d+_\d+)")


def _dmap_chapter_char():
    """{chapter: charID} derived from the chapter's own key art.

    **This is the rule, and it is fully derivable** -- which two earlier attempts were
    not. The chapter select screen shows one character per chapter, and that is the cast
    its decisions pay. The chain, all of it from shipped data:

        chapter == dmap id       (campaign stages carry `_dmap_id`)
          dmap._portrait_info    -> a sprite id
          sprite._atlas          -> "character/co055_01/co055_01_avg"
          coNNN_01               == avg_role._folderName
          avg_role id            -> charID via _role_char_map()

    Verified against all six chapters confirmed in play -- 1 Jacqueline, 2 Caillen,
    3 Matina, 4 Noach, 5 Aura, 6 Draco -- six of six, and it resolves the remaining
    chapters with no further observation.

    Best-effort like the rest of this file: any failure degrades to whatever resolved
    before it, because a payout must never fail on a missing form.
    """
    global _dmap_char_cache
    if _dmap_char_cache is not None:
        return _dmap_char_cache
    out = {}
    try:
        import battle as _bt
        sprites = _bt.dd.rows("sprite") or {}
        by_folder = {}
        for rid, row in (_bt.dd.rows("avg_role") or {}).items():
            folder = str(row.get("_folderName") or "").strip()
            if folder:
                by_folder.setdefault(folder, []).append(int(rid))
        roles = _role_char_map()
        for dmap_id, row in (_bt.dd.rows("dmap") or {}).items():
            portrait = str(row.get("_portrait_info") or "").strip()
            if not portrait.isdigit():
                continue
            atlas = str((sprites.get(int(portrait)) or {}).get("_atlas") or "")
            m = _ATLAS_FOLDER.search(atlas)
            if not m:
                continue
            for rid in by_folder.get(m.group(1), []):
                if rid in roles:
                    out[int(dmap_id)] = roles[rid]
                    break
    except Exception:                       # noqa: BLE001 -- never break a payout
        pass
    _dmap_char_cache = out
    return out


def karma_char_for(avg_id):
    """Which cast a decision pays karma to.

    Order: a scene's confirmed speaker, then the hand-confirmed chapter table, then the
    chapter's key art. The table comes before the derivation only so a confirmed
    observation can always override it; the two agree on all six chapters checked.
    """
    by_role = role_char(KARMA_SCENE_ROLE.get(int(avg_id)))
    if by_role:
        return by_role
    chapter = avg_chapter(avg_id)
    if chapter in KARMA_CHAPTER_CHAR:
        return KARMA_CHAPTER_CHAR[chapter]
    return _dmap_chapter_char().get(chapter) or KARMA_TUTORIAL_CHAR


def karma_reward(avg_id, option):
    """(currencyType, amount, charID, fexp) for a decision.

    The amount is per (scene, OPTION): the reward tracks how cruel the choice is, so the
    three options of a scene pay three different grades rather than the scene having one
    payout. `KARMA_REWARDS` wins where a real observation exists -- that is the record of
    what the retail server paid -- and everything else falls to the dealt grade.

    The character is resolved per chapter regardless, so even an unlisted scene pays the
    right cast.
    """
    key = (int(avg_id), int(option))
    grade = KARMA_DECISION_TIER.get(key)
    observed = KARMA_REWARDS.get(key)
    if observed is not None:
        # An observation settles which GRADE this option is -- its karma amount names the
        # band outright -- but the payout still comes from KARMA_TIERS, so editing that
        # table moves every decision including this one.
        grade = next((g for g, (_c, _a, f) in KARMA_TIERS.items()
                      if f == observed[3]), grade)
    cur, amount, fexp = KARMA_TIERS.get(grade) or (
        KARMA_DEFAULT[0], KARMA_DEFAULT[1], KARMA_DEFAULT[3])
    # The tables above stay the RECORD of what retail paid; the server rate scales the
    # payout here, at the point of grant. Default 1.0 pays the observed 5/10/15 exactly.
    # Karma (fexp) is deliberately NOT scaled -- the banner grades itself off that number
    # ("Up!" under 20, "Big Up!" from 20, "Ultimate Up!" from 100), so scaling it would
    # silently relabel every decision.
    return cur, settings.scale(amount, "karma_gems"), karma_char_for(avg_id), fexp


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
    from .core import add_currency
    return add_currency(state, cur_type, amount)

"""Soul Book: break artwork/skins, Kizuna, and the Soul Link soulbook.

Split out of the former monolithic core.py; depends only on .core.
"""


import json
import design_data as dd

from .core import (
    CHAR_BUYCOUNT_MAX,
    char_sort_list,
    SHOWGIRL_OFFSET_DEFAULT,
    _char_data_json,
    _char_id_json,
    char_star,
    helper_uid,
    karma_of,
    uid,
)



# ---- Break artwork / skins (Char 308 unlock_skin, 309 set_skin) -------------
#
# `CharSoulType`: Normal=0 (base art), **Limit=1 (大破 "Break")**, **Super=2 (超大破
# "EX Break")**, **Ultra=3 (天啓 "Apoc.")** -- the three tabs on the character gallery,
# unlocked by filling the BREAK gauge with Soulmirrors (see [[sevensins-soulmirrors]]).
#
# Two DIFFERENT scopes, which is the thing to keep straight:
#   * the UNLOCK is per CHARACTER ID   -> CharIDData.skin1/skin2/skin3 (cmd 567)
#   * the SELECTION is per COPY (uid)  -> DBCharData.skin              (cmd 528)
#
# 308 `RequestUnlockSkin(char_uid, skinType)` (0x16A0F50) sends
# `intargs=[skinType], strargs=[char_uid]`, but the reply is NOT an echo:
# `receivedUnlockSkin` (0x169BEA4) reads `intargs=[**charId**, skinType]` -- the design
# id, not the uid -- indexes `charIDDic[charId.ToString()]` with `get_Item` (throws on a
# character we never synced) and sets skin1/2/3 for skinType 1/2/3. It needs at least
# TWO intargs and no strargs at all. A charId of 0 makes it skip the flag write and only
# raise the event, so always send the real id.
#
# 309 `RequestSetSkin(char_uid, skinType)` (0x16A1030) has the same request shape and
# its reply IS an echo: `receivedSetSkin` (0x169C118) reads `strargs[0]` as the uid,
# `intargs[0]` as the type, and writes `charDic[uid].dbChar.skin` (DBCharData +0x58).
#
# `PanelCharacterGallery.OnGalleryMoldingUnlockClick` (0x16D1734) calls RequestUnlockSkin
# straight through -- no currency check, no item cost, no confirm dialog -- so unlocking
# is free and the eligibility gate is entirely the client's BREAK gauge.
CHAR_SOUL_NORMAL, CHAR_SOUL_LIMIT, CHAR_SOUL_SUPER, CHAR_SOUL_ULTRA = 0, 1, 2, 3
CHAR_SOUL_TYPES = (CHAR_SOUL_LIMIT, CHAR_SOUL_SUPER, CHAR_SOUL_ULTRA)


def unlocked_skins(state, char_id):
    """The CharSoulTypes unlocked for a character id."""
    return {int(t) for t in state.get("skins", {}).get(str(char_id), ())}


def unlock_skin(state, char_uid, skin_type):
    """Mark a CharSoulType unlocked for the cast's character id. -> that char id."""
    entry = state["roster"][char_uid]
    if int(skin_type) not in CHAR_SOUL_TYPES:
        raise ValueError(f"{skin_type} is not an unlockable CharSoulType")
    cid = str(entry["id"])
    have = state.setdefault("skins", {}).setdefault(cid, [])
    if int(skin_type) not in have:
        have.append(int(skin_type))
        have.sort()
    return entry["id"]


def set_skin(state, char_uid, skin_type):
    """Choose which artwork a COPY displays. -> the stored value."""
    entry = state["roster"][char_uid]
    skin_type = int(skin_type)
    if skin_type != CHAR_SOUL_NORMAL and skin_type not in CHAR_SOUL_TYPES:
        raise ValueError(f"{skin_type} is not a CharSoulType")
    if skin_type != CHAR_SOUL_NORMAL and \
            skin_type not in unlocked_skins(state, entry["id"]):
        raise ValueError(f"CharSoulType {skin_type} is not unlocked for {entry['id']}")
    entry["skin"] = skin_type
    return skin_type


def char_id_table(state):
    """`charIDDic` -- {char id: CharIDData}, keyed by character ID rather than copy.

    Shared by the login sync (`char_json`, as `id_tbl`) and the Soulpedia request
    (PlayerChar cmd 310 -> 567), which must agree or the pedia's completion count
    disagrees with the roster."""
    id_tbl = {}
    for uid, entry in state["roster"].items():
        db = _char_data_json(uid, entry)["dbdata"]
        cid = str(entry["id"])
        rec = _char_id_json(entry, db["star"], db["super_star"],
                            karma_of(state, entry["id"]),
                            unlocked_skins(state, entry["id"]))
        # Rule 2: keep the best copy, not the last one seen.
        prev = id_tbl.get(cid)
        if prev is None or char_soulbook_score(entry["id"], rec) > \
                char_soulbook_score(entry["id"], prev):
            id_tbl[cid] = rec
    for cid, rec in id_tbl.items():
        rec["score"] = char_soulbook_score(int(cid), rec)
        # `kset`/`klv` are CONFIRMED (2026-08-08, `tools/json_keys.py --class
        # CharIDData` on the 2.2.4 binary: BookKizunaSetData -> 'kset',
        # BookKizunaLvData -> 'klv'); they are no longer the guesses this comment used
        # to describe. The C# field names are kept alongside purely as belt-and-braces:
        # Json.NET ignores whichever does not match, and an unbound list/dict here would
        # leave BookKizunaLvData null, which receivedBookKizunaSet dereferences without
        # a guard. The same run confirmed skin1/skin2/skin3 and DBCharData's `skin`.
        rec["kset"] = rec["BookKizunaSetData"] = kizuna_set(state, cid)
        rec["klv"] = rec["BookKizunaLvData"] = kizuna_levels(state, cid)
    return id_tbl


# ---- Soul Book Kizuna (CharRpc book_kizuna_set 313 -> 569,
#                                 book_kizuna_lvup 320 -> 576) ---------------
# The "Active Effects 0/5" / "Master" panel on a cast. Two pieces of per-CHARACTER (not
# per-copy) state live on CharIDData and must be in every charIDDic we send:
#   List<uint>            BookKizunaSetData  -- the equipped support rows, max 5
#                                              (CharDefine.SoulBookKizunaMaxEquipCount)
#   Dictionary<uint,uint> BookKizunaLvData   -- row id -> level
#
# receivedBookKizunaSet (RVA 0x169C9D0) rebuilds the equipped list from
# `intargs = [charID, ...rowIds]` and looks each row's level up in BookKizunaLvData, so
# the LEVELS have to already be on the client from the char sync -- reply 569 does not
# carry them. receivedBookKizunaLvup (RVA 0x169CD30) is just
# `intargs = [charID, rowID, newLevel]`.
#
# NOT VALIDATED: level-up costs and unlock conditions live in the `soulbook_kizuna`
# design form, which our type-tree generator cannot parse (`read_str out of bounds` --
# the row has nested UnlockCondition[]/LevelVariable[] arrays under a generic base).
# The client owns the pack and gates the UI itself, so the server is permissive here.
MAX_KIZUNA_EQUIP = 5        # CharDefine.SoulBookKizunaMaxEquipCount


def _kizuna(state, char_id):
    return state.setdefault("kizuna", {}).setdefault(str(char_id),
                                                     {"set": [], "lv": {}})


def kizuna_set(state, char_id):
    return list(_kizuna(state, char_id)["set"])


def kizuna_levels(state, char_id):
    return dict(_kizuna(state, char_id)["lv"])


def set_kizuna(state, char_id, row_ids):
    """Equip support rows. -> (ok, stored rows)."""
    rows, seen = [], set()
    for r in row_ids:
        r = int(r)
        if r and r not in seen:
            seen.add(r)
            rows.append(r)
    if len(rows) > MAX_KIZUNA_EQUIP:
        return False, kizuna_set(state, char_id)
    k = _kizuna(state, char_id)
    k["set"] = rows
    # A row has to have a level entry or receivedBookKizunaSet's dictionary lookup
    # throws -- it indexes BookKizunaLvData[rowId] with no ContainsKey guard.
    for r in rows:
        k["lv"].setdefault(str(r), 0)
    return True, rows


def kizuna_level_up(state, char_id, row_id, now_lv):
    """Raise one support row's level. -> (ok, new level)."""
    k = _kizuna(state, char_id)
    cur = int(k["lv"].get(str(row_id), 0))
    if int(now_lv) != cur:
        return False, cur          # client and server disagree; refuse rather than guess
    k["lv"][str(row_id)] = cur + 1
    return True, cur + 1


# ---- Soul Link (the soulbook) ---------------------------------------------
# `bookRank` / `bookSumXp` / `bookLeftXp` on PlayerCharData, drawn as the RANK + score
# bar along the bottom of the Soulpedia. Nothing client-side computes this -- the
# DesignSoulbookScoreRow getters have no code xrefs at all -- so it is server
# authoritative and had to be reconstructed. The in-game "Soul Link Point Rules" help
# screen gives the whole table, and it matches `soulbook_score` column for column:
#
#             First Summon  Base Level  Rank  Ultra Level  Skill Rank  Karma Rank
#   Sins             28000         210  8800          400        7000         280
#   Virtues          14000         210  8800          400        5250         280
#   Riders           14000         210  8800          400        5250         280
#   *5 Awakers        3500          85  8000          400        1750         225
#   *4 Awakers        1750          65  5600          100         350         165
#
#   design column:   _base     _rankup  _star   _surmount     _awaken     _kizuna
#
# Rows 103/104 match the two Awaker lines exactly. The row id is the char's
# **`_alignment`**, confirmed by who lives in each: 100 = Lucifer/Leviathan/Satan
# (Sins), 101 = Michael/Uriel/Sariel (Virtues), 102 = Esmira/Chino (Riders), 103/104 =
# the *5 / *4 Awakers, 9001 = gremlin mobs. Our EN 2.2.7 pack has 101/102 at Sins-level
# values (28000/7000) and 100 raised to 220/9000/420/7200/300, i.e. the categories were
# rebalanced after the build that help screen was written for -- so always read the
# numbers from the pack, never from the table above.
#
# Help-screen rules, all three implemented here:
#   1. eligible only for casts of initial rarity *4 or above  -> soulbook_eligible
#   2. scored from the HIGHEST trained status among copies    -> the charIDDic record
#   3. the record is kept even if the cast is unsummoned/lost -> charIDDic is keyed by
#      character id, so it already outlives any individual copy
SOULBOOK_MIN_STAR = 4


def soulbook_eligible(char_id):
    """Rule 1: Soul Link only counts casts whose INITIAL rarity is *4 or above."""
    row = dd.row("char", char_id)
    return bool(row) and char_star(row.get("_rarity")) >= SOULBOOK_MIN_STAR


def char_soulbook_score(char_id, record):
    """Soul Link points for one cast, from its best-ever record (a CharIDData).

    `record` is the charIDDic entry, which is the high-water mark across every copy
    ever owned -- that is what help rules 2 and 3 describe."""
    row = dd.row("char", char_id)
    if not row or not soulbook_eligible(char_id):
        return 0
    w = dd.row("soulbook_score", row.get("_alignment"))
    if not w:
        return 0
    # First Summon is granted once for having ever obtained the cast; every other
    # column is a per-level rate applied to the trained value.
    return (w.get("_base", 0)
            + w.get("_rankup", 0) * record.get("lv", 0)
            + w.get("_star", 0) * record.get("star", 0)
            + w.get("_surmount", 0) * record.get("super_star", 0)
            + w.get("_awaken", 0) * record.get("skill", 0)
            + w.get("_kizuna", 0) * record.get("flv", 0))


def book_rank_for(sum_xp):
    """Highest `soulbook_reward` rank whose cumulative _exp_sum the score has reached.

    This is the rank the player is ELIGIBLE for, not the one they hold: the bar shows
    `RANK UP!` and only advances on CharRpcServerCmd.book_rank_up (311), which is why
    footage of a live account sits at RANK 97 with a score already past rank 100."""
    rank = 0
    for rid in sorted(dd.rows("soulbook_reward")):
        if sum_xp < dd.row("soulbook_reward", rid).get("_exp_sum", 0):
            break
        rank = rid
    return rank


def book_sum_xp(state):
    """Total Soul Link points across every cast ever recorded."""
    return sum(char_soulbook_score(int(cid), rec)
               for cid, rec in char_id_table(state).items())


def book_progress(state):
    """-> (bookRank, bookSumXp, bookLeftXp) for the Soul Link bar."""
    sum_xp = book_sum_xp(state)
    rank = int(state.get("book_rank", 0))
    claimed = dd.row("soulbook_reward", rank) or {}
    # "left" = points not yet consumed by the ranks already claimed.
    return (rank, sum_xp, max(0, sum_xp - claimed.get("_exp_sum", 0)))


def char_json(state):
    """PlayerCharData. `acPeriod` and the three ctor params must be present or the
    client NPEs -- see docs/GAME_SERVER.md."""
    pro_chars = {uid: _char_data_json(uid, entry)
                 for uid, entry in state["roster"].items()}
    # charIDDic is keyed by the character ID, so multiple copies collapse to one entry
    # -- that is the point of it being separate from charDic. Build it with the same
    # helper the Soulpedia sync (567) uses, or the two disagree: this used to inline
    # _char_id_json, which kept the best-copy rule but left `kset`/`klv` empty, so the
    # LOGIN charIDDic carried no Kizuna state at all.
    id_tbl = char_id_table(state)
    # Computed ONCE: book_progress -> book_sum_xp walks every cast in charIDDic and
    # scores it, so reading it per field would re-score the whole roster twice.
    book_rank, book_xp, _left = book_progress(state)
    return json.dumps({
        "pro_chars": pro_chars,
        "group_tbl": {}, "id_tbl": id_tbl,
        "formations": state["formations"], "acPeriod": [],
        # PlayerCharData.addCharCount. Roster capacity is
        # CharCapacityDefault(100) + CharCapacityPerBuycount(5) * add_char, capped at
        # CharBuycountMax(50) -> 350. Left at 0 the roster jams at 100, which the
        # Consonance partner list alone overruns.
        "add_char": int(state.get("add_char", CHAR_BUYCOUNT_MAX)),
        "showgirl": state["showgirl"],
        "state": int(state.get("showgirl_state", 0)),
        "offset": state.get("showgirl_offset", SHOWGIRL_OFFSET_DEFAULT),
        # PlayerChar.receivedHelperData (0x169B860) only accepts a uid that is IN
        # charDic; anything else -- including "" -- falls through to the
        # `helper uid error` warning that repeated once per login. The helper is the
        # cast lent to friends, so the lead of the first formation is the sane default.
        "helper": helper_uid(state),
        # The real Soul Link rank and score, NOT zeros.
        #
        # These drive the RANK bar at the foot of the Soulpedia AND the flat stat every
        # cast carries: `soulbook_reward` rank N pays atk 5N / def 2N / hp 35N,
        # cumulative, reaching +1500 / +600 / +10500 at rank 300.
        #
        # The BATTLE side already honoured it -- titan_server passes state["book_rank"]
        # into bt.Battle, which builds self.book_bonus from battle.soulbook_bonus. So the
        # bonus was genuinely applied in a fight and invisible in the menu, because the
        # cast's stat panel reads THIS payload and this payload said rank 0.
        #
        # book_progress() is the same helper the five other sync sites already use (the
        # Soulpedia request 567, the rank-up reply, and the two post-battle pushes), so
        # login now agrees with them instead of being the one caller that hardcodes.
        # `EMPTY_CHAR_JSON` in titan_server keeps its zeros on purpose: that payload has
        # no roster to score.
        "book_rank": book_rank,
        "book_xp": book_xp,
        "orgArenaTeam": [],
        "sort_list": char_sort_list(state),
        "act_collection": [],
    }, separators=(",", ":"))

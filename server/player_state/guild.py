"""Guild.

Split out of the former monolithic core.py in spirit -- this is a new subsystem and
depends only on .core.

A guild on a private server is necessarily a guild of ONE. That is not a degradation:
`PlayerGuild` keeps no local state at all, every field of `GuildInfo` arrives as
server JSON, and nothing in the client cross-checks the member count against anything.
A one-member guild with the player as Director renders correctly, and it is what
unlocks three things that were otherwise dead:

  * the Guild Weekly boss (`PlayerChallenge`; see challenge.py) -- `PanelGuild`'s
    week-field button, and with it the 28 book-8 stages that ship in the design pack
    and had no route to them;
  * Guild Pt (CurrencyType 64), which the shop already prices nine goods in;
  * daily mission 2001 and weekly 10042, which had no way to move.

**Wire keys are NOT the C# field names.** Every one below was read out of the
[JsonProperty] attribute thunks in libil2cpp.so, and they are neither the field names
nor a consistent casing convention -- `OwnerName` is `owner_name`, `UpdateTime` is
`updatetime`, `listLog` is `log`, `signRewardTbl` is `signReward`, `MemberCount` is
`mbCnt`. Do not "tidy" them.
"""

import json, time

from .core import (
    CUR_MIRA,
    _daily_period,
    grant_reward,
    helper_uid,
    uid,
)

import battle as bt

__all__ = [
    "GUILD_CREATE_MIRA", "GUILD_MEMBER_MAX", "GUILD_LOG_MAX",
    "GUILD_RANK_DIRECTOR", "GUILD_EDIT_OWNER", "GUILD_EDIT_AD",
    "GUILD_EDIT_ANNOUNCE", "GUILD_SIGN_LIMITS",
    "ERR_GUILD_OTHER", "ERR_GUILD_NIL", "ERR_GUILD_COST_FAIL",
    "ERR_GUILD_NAME_LEN", "ERR_GUILD_HAD_GUILD", "ERR_GUILD_NO_PERMISSION",
    "have_guild", "guild_status", "guild_json", "guild_members_json",
    "guild_card_json", "create_guild", "quit_guild", "disband_guild",
    "guild_sign", "guild_set_setting", "guild_edit",
]

# GuildLimit / CostValue in the client.
GUILD_CREATE_MIRA = 200000      # CostValue.NeedMira
GUILD_MEMBER_MAX = 30           # GuildLimit.MemberMax
GUILD_LOG_MAX = 100             # GuildLimit.LogMax

# GuildRank
GUILD_RANK_NONE, GUILD_RANK_DIRECTOR = 0, 1
GUILD_RANK_VICE, GUILD_RANK_MEMBER = 2, 3

# GuildStatus
GUILD_STATUS_NONE, GUILD_STATUS_MEMBER, GUILD_STATUS_APPLY = 0, 1, 2

# EditType (the `type` in intargs[0] of an edit request/reply).
GUILD_EDIT_OWNER, GUILD_EDIT_AD, GUILD_EDIT_ANNOUNCE = 1, 2, 3

# GuildRpcErrno, for the cmd-65535 error reply. That reply is not merely informative:
# the 65535 branch is the ONLY one that calls PanelLoadingWaiting.Close, so a request
# we decline has to come back as an error or the panel's block overlay never lifts.
ERR_GUILD_COOLDOWN = 141
ERR_GUILD_OTHER = 3101
ERR_GUILD_USED_NAME = 3102
ERR_GUILD_NIL = 3106
ERR_GUILD_COST_FAIL = 3107
ERR_GUILD_NAME_LEN = 3108
ERR_GUILD_HAD_GUILD = 3111
ERR_GUILD_NO_PERMISSION = 3114

# ---- the sign-in (guild check-in) ladder ----------------------------------
# `PanelGuild._countTodaySignCnt` walks `_signRewards` -- a [SerializeField]
# List<GameObject> baked into the prefab -- and for slot i reads
# `signReward.limitList[i]`, so limitList (and idList/cntList, which
# `onSignRewardClick` indexes the same way) must be AT LEAST as long as that list or
# the panel throws IndexOutOfRange on open.
#
# The prefab has exactly FIVE slots. Counted by parsing the PanelGuild MonoBehaviour
# out of ngui_prefabs_panels_community: 15 PPtrs of [SerializeField] refs, then the
# List<GameObject> length, which is 5. (UnityPy can read the raw bytes without a type
# tree; TypeTreeGeneratorAPI is not installed here.) Five entries is therefore exact,
# not generous.
#
# The thresholds are counts of member sign-ins for the day and the progress bar is
# `signSum[0] / limitList[-1]`. With one member the ladder has to be reachable by one
# person, so it is 1..5 rather than the 10/30/60/100/150 a 30-player guild would use.
GUILD_SIGN_LIMITS = (1, 2, 3, 4, 5)
# Guild Pt is **item 4**, whose row carries `_action 5` (ITEM_ACTION_CURRENCY) with
# `_param1 64` = CurrencyType.Guild -- so it is paid through grant_reward and lands in
# `currency`, not the backpack. (Item 711 is Stamina (100); it appears next to Guild Pt
# in shop.py only because goods 2407 SELLS it FOR Guild Pt.)
# This is the point of the exercise: the shop prices nine goods in Guild Pt and
# nothing else in the server pays any out.
GUILD_SIGN_REWARD_IDS = (4, 4, 4, 4, 4)
GUILD_SIGN_REWARD_COUNTS = (20, 20, 30, 30, 50)

# GuildLogInfo ids are a design-side text table we do not have a form for, so the log
# stays empty rather than inventing rows the client would render as blanks. listLog is
# initialised by GuildInfo..ctor, so omitting the key entirely is also safe -- we send
# it explicitly only to keep the shape self-documenting.


def have_guild(state):
    return bool(state.get("guild"))


def guild_status(state):
    """The `gStatus` the sync reply carries. There is no applying-to-a-guild state on
    a single-player server, so this is only ever none or member."""
    return GUILD_STATUS_MEMBER if have_guild(state) else GUILD_STATUS_NONE


def _guild_uid(state):
    return "g" + uid(state)


# ---- the payloads --------------------------------------------------------

def guild_json(state):
    """GuildInfo, for sync (528) and create (529) strargs[0].

    `signSum` MUST be non-empty: `RequestServerInfoUpdate` -- which PanelGuild fires
    from onActive every time the panel opens -- reads `signSum[0]` and throws
    ArgumentOutOfRange on an empty list before it ever sends.
    """
    g = state.get("guild") or {}
    _roll_sign_day(state)
    return json.dumps({
        "uid": g.get("uid", _guild_uid(state)),
        "name": g.get("name", ""),
        "owner_name": state["name"],
        "owner_uid": uid(state),
        "createTime": int(g.get("create_time", 0)),
        "ad_text": g.get("ad", ""),
        "announce": g.get("announce", ""),
        "signSum": [int(g.get("sign_sum", 0))],
        "editTime": int(g.get("edit_time", 0)),
        "badge": int(g.get("badge", 0)),
        "joinFree": int(g.get("join_free", 0)),
        "Area": int(g.get("area", 0)),
        "updatetime": int(time.time()),
        "log": [],
        "signReward": {
            "idList": list(GUILD_SIGN_REWARD_IDS),
            "cntList": list(GUILD_SIGN_REWARD_COUNTS),
            "limitList": list(GUILD_SIGN_LIMITS),
        },
    }, separators=(",", ":"))


def _player_info_json(state):
    """PlayerInfo -- the same shape the friend/helper lists use.

    `helper` is a List<List<int>> and the rid/rlv/rstar/rplus/rhp/ratk/rsuper_star
    properties are literally `helper[0][0..6]` (offsets 0x20..0x38 off the inner
    array), so that inner list needs all SEVEN entries or the member row renders a
    cast with no portrait.
    """
    entry = (state.get("roster") or {}).get(helper_uid(state)) or {}
    char_id = int(entry.get("id", 0))
    row = bt.dd.row("char", char_id) or {}
    star = entry.get("star") or (bt._default_star(row) if row else 1)
    super_star = int(entry.get("super_star") or 0)
    lv = int(entry.get("lv", 1))
    stats = bt._grow(row, star, lv, super_star) if row else {"hp": 0, "atk": 0}
    return {
        "uid": uid(state), "ptype": 1, "pid": uid(state),
        "alv": int((state.get("level") or {}).get("lv", 1)),
        "callnum": "", "utime": int(time.time()),
        "name": state["name"], "context": "",
        "helper": [[char_id, lv, int(star), int(entry.get("plus", 0)),
                    int(stats["hp"]), int(stats["atk"]), super_star]],
        "gname": (state.get("guild") or {}).get("name", ""),
        "skill": [], "f_skill": [],
    }


def guild_members_json(state):
    """MembersInfo, for the member-list reply (562) strargs[0].

    `fillGuildMemberData` walks MemberMongo's KEYS and does a plain `get_Item` on
    MemberRedis for each one, so **every member key must appear in both dicts** or it
    throws KeyNotFoundException. Same pairing for appliers, which is why both applier
    dicts are present-and-empty rather than omitted: all four carry a
    LuaTableConverter, and that converter only ever yields null when the key is
    absent altogether.
    """
    g = state.get("guild") or {}
    me = uid(state)
    return json.dumps({
        "memberMongo": {me: {
            "rank": int(g.get("rank", GUILD_RANK_DIRECTOR)),
            "ctb": int(g.get("contribution", 0)),
            "time": 0,                      # OfflineTime; 0 = online now
            "sign": list(g.get("member_sign", [])),
            "challengeTopScore": dict(g.get("challenge_top", {})),
        }},
        "memberRedis": {me: _player_info_json(state)},
        "applierMongo": {},
        "applierRedis": {},
    }, separators=(",", ":"))


def guild_card_json(state):
    """GuildCardInfo, for the name-card lookup reply (593)."""
    g = state.get("guild") or {}
    return json.dumps({
        "name": g.get("name", ""),
        "owner_name": state["name"],
        "badge": int(g.get("badge", 0)),
        "mbCnt": 1,
        "ad_text": g.get("ad", ""),
        "dayRank": 0,
        "btRank": 0,
        "createTime": int(g.get("create_time", 0)),
    }, separators=(",", ":"))


# ---- mutations -----------------------------------------------------------

def create_guild(state, name, ad, badge, join_free):
    """-> (ok, errno). Charges CostValue.NeedMira, exactly as the real server did."""
    if have_guild(state):
        return False, ERR_GUILD_HAD_GUILD
    name = (name or "").strip()
    if not name:
        return False, ERR_GUILD_NAME_LEN
    have = int(state["currency"].get(str(CUR_MIRA), 0))
    if have < GUILD_CREATE_MIRA:
        return False, ERR_GUILD_COST_FAIL
    state["currency"][str(CUR_MIRA)] = have - GUILD_CREATE_MIRA
    now = int(time.time())
    state["guild"] = {
        "uid": _guild_uid(state),
        "name": name,
        "ad": ad or "",
        "announce": "",
        "badge": int(badge or 0),
        "join_free": int(join_free or 0),
        "area": 0,
        "create_time": now,
        "edit_time": now,
        "rank": GUILD_RANK_DIRECTOR,
        "contribution": 0,
        "sign_sum": 0,
        "sign_day": _daily_period(),
        "sign_paid": 0,
        "member_sign": [],
        "challenge_top": {},
    }
    return True, 0


def quit_guild(state):
    """Leaving and disbanding are the same operation for a guild of one; they differ
    only in which reply command the client wants back."""
    if not have_guild(state):
        return False, ERR_GUILD_NIL
    state["guild"] = None
    return True, 0


def disband_guild(state):
    return quit_guild(state)


def _roll_sign_day(state):
    """Zero the day's sign count on the shared 4AM rollover. -> True if it rolled."""
    g = state.get("guild")
    if not g:
        return False
    period = _daily_period()
    if g.get("sign_day") == period:
        return False
    g["sign_day"] = period
    g["sign_sum"] = 0
    g["sign_paid"] = 0
    g["member_sign"] = []
    return True


def guild_sign(state):
    """The daily guild check-in, behind server cmd 310.

    -> (sign_sum, [(item_id, count), ...]) -- the new guild-wide count and whatever
    reward tiers that count just crossed. Signing twice in one day is a no-op that
    still reports the current count, because PanelGuild fires this from onActive on
    every open, not from a button.
    """
    g = state.get("guild")
    if not g:
        return 0, []
    _roll_sign_day(state)
    me = uid(state)
    if me not in g.get("member_sign", []):
        g.setdefault("member_sign", []).append(me)
        g["sign_sum"] = int(g.get("sign_sum", 0)) + 1
    total = int(g.get("sign_sum", 0))
    paid = int(g.get("sign_paid", 0))
    earned = []
    for i, limit in enumerate(GUILD_SIGN_LIMITS):
        if i < paid or total < limit:
            continue
        item_id, count = GUILD_SIGN_REWARD_IDS[i], GUILD_SIGN_REWARD_COUNTS[i]
        grant_reward(state, item_id, count)
        earned.append((item_id, count))
        g["sign_paid"] = i + 1
    return total, earned


def guild_set_setting(state, badge, join_free):
    g = state.get("guild")
    if not g:
        return False, ERR_GUILD_NIL
    g["badge"] = int(badge)
    g["join_free"] = int(join_free)
    return True, 0


def guild_edit(state, edit_type, texts):
    """EditType 1 OWNER / 2 AD / 3 ANNOUNCE. -> (ok, errno).

    OWNER is refused: handing the directorship to someone else needs someone else.
    """
    g = state.get("guild")
    if not g:
        return False, ERR_GUILD_NIL
    if edit_type == GUILD_EDIT_AD:
        g["ad"] = texts[0] if texts else ""
    elif edit_type == GUILD_EDIT_ANNOUNCE:
        g["announce"] = texts[0] if texts else ""
        g["edit_time"] = int(time.time())
    else:
        return False, ERR_GUILD_NO_PERMISSION
    return True, 0

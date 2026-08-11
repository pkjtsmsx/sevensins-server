"""Login bonus.

Split out of the former monolithic core.py; depends only on .core.
"""


import json, time
import battle as bt



# ---- login bonus ----------------------------------------------------------
# PlayerLoginBonus asks with server cmd **1** on index 0xFE16052C and the reply is cmd
# **17** on 0xFFB98ABA -- this subsystem does NOT follow the usual "reply = servercmd +
# 0x100" rule (its whole switch is 17/19/33/49). Build layout A (uint64_msg), and
# `HandleSyncCmd` bails unless strargs has **exactly one** entry: a LoginBonusSyncData.
#
# Wire keys are NOT the C# property names (read off the JsonProperty thunks):
#   LoginBonusSyncData: group_data, active, nextreset, total_day
#   GroupInfo:          group, dcnt (day), bcnt (book), tcnt (total),
#                       duration_s (start), duration_e (end), cin_book (checkin_book),
#                       cin_day (checkin_day), banner (bannerID), text_id (textID), close
#
# The ladders themselves ARE in the pack: `login_bonus` rows {_id,_group,_day,_book,
# _mail} and each `_mail` row carries the payout as `_itemID` + `_param`. Group 1 is the
# 28-day ladder, group 3 the 14-day OB event.
#
# UNVERIFIED semantics (first cut -- the panel is the oracle): `book` looks like which
# pass through the ladder you are on (login_bonus rows carry `_book`), `dcnt` the day
# reached and `cin_day` the day already claimed, with duration_s/e bounding the event.
LOGIN_BONUS_GROUP = 1          # 1 = 28-day ladder, 3 = the 14-day OB event
LOGIN_BONUS_BOOK = 1
# a wide window so the ladder is always live rather than expired
LOGIN_BONUS_WINDOW = 365 * 24 * 3600


def login_bonus_json(state):
    """strargs[0] of the login-bonus sync (cmd 17). Exactly one string, or the handler
    bails."""
    now = int(time.time())
    day = int(state.get("login_day", 1))
    claimed = int(state.get("login_claimed_day", 0))
    rows = bt.dd.rows("login_bonus")
    total_days = sum(1 for r in rows.values() if r.get("_group") == LOGIN_BONUS_GROUP)
    group = bt.dd.rows("login_bonus_group").get(LOGIN_BONUS_GROUP, {})
    return json.dumps({
        "group_data": [{
            "group": LOGIN_BONUS_GROUP,
            "dcnt": day,
            "bcnt": LOGIN_BONUS_BOOK,
            "tcnt": total_days,
            "duration_s": now - LOGIN_BONUS_WINDOW,
            "duration_e": now + LOGIN_BONUS_WINDOW,
            "cin_book": LOGIN_BONUS_BOOK,
            "cin_day": claimed,
            "banner": group.get("_banner_id") or 0,
            "text_id": group.get("_text_id") or 0,
            "close": 0,
        }],
        "active": LOGIN_BONUS_GROUP,
        "nextreset": now + 24 * 3600,
        "total_day": int(state.get("login_total_days", day)),
    }, separators=(",", ":"))

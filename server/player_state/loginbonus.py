"""Login bonus.

Split out of the former monolithic core.py; depends only on .core.
"""


import json, time
import battle as bt

from .core import QUEST_CASE_LOGIN_DAYS, bump_quest_counter
from .mail import add_mail



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
# The ladder is entirely SERVER-driven: `PlayerLoginBonus.RequestServerSync` is the only
# thing the client ever sends (cmd 1), and the panel has no claim button -- tapping a day
# icon does nothing by design, and `PanelLoginBonus.UpdateCurWeekData` just paints what
# the sync said. So a ladder that never moves is a server that never rolled it over, not
# a click the client dropped.
#
# What the panel does with the numbers (read off UpdateBonusData/UpdateCurWeekData):
# `_curBonusDay` = dcnt -> `_curBonusWeek = ceil(dcnt/7)`, and the NEXT! marker sits at
# `_nextBonusIndex = (dcnt % 7) - 1` (6 when dcnt is a multiple of 7). Every icon BEFORE
# that index is drawn as already received. So dcnt is the day you are ON and have not
# been paid for yet -- dcnt = claimed + 1 -- not the count of days received. cin_book /
# cin_day are the just-checked-in pair the animation path reads instead.
LOGIN_BONUS_GROUP = 1          # 1 = 28-day ladder, 3 = the 14-day OB event
# a wide window so the ladder is always live rather than expired
LOGIN_BONUS_WINDOW = 365 * 24 * 3600


def _ladder(group=LOGIN_BONUS_GROUP):
    """{day: login_bonus row} for a group, e.g. 28 rows for group 1."""
    return {int(r["_day"]): r
            for r in bt.dd.rows("login_bonus").values()
            if r.get("_group") == group and r.get("_day")}


def advance_login_bonus(state, now=None):
    """Roll the ladder forward on the first login of each UTC day and pay the day out.

    The payout is MAIL, not a direct grant: every `login_bonus._mail` points at a
    DesignMailForm row whose `_itemID`/`_param` IS the reward and whose subject is
    "Login Bonus DAY{custom}" with a 7-day expiry. Handing it over as mail is what the
    data describes, and it keeps the reward claimable later rather than needing the panel
    to be open at the right moment.

    One day per calendar day, with no back-pay for days away: a player who skips a week
    resumes at the next rung rather than collecting seven mails at once. Finishing the
    last rung starts the next book at day 1.

    Returns [(day, item_id, count)] for what was paid (empty if already rolled today).
    """
    now = int(now if now is not None else time.time())
    today = now // 86400                        # UTC day number; nextreset is midnight
    if int(state.get("login_last_day", 0)) == today:
        return []

    ladder = _ladder()
    if not ladder:
        return []
    day = int(state.get("login_claimed_day", 0)) + 1
    book = int(state.get("login_book", 1))
    if day > max(ladder):
        day, book = 1, book + 1

    paid = []
    row = ladder.get(day)
    mail_row = bt.dd.rows("mail").get(int(row["_mail"])) if row else None
    if mail_row and mail_row.get("_itemID"):
        item, count = int(mail_row["_itemID"]), int(mail_row.get("_param") or 1)
        add_mail(state, int(row["_mail"]), {item: count}, custom=day, timestamp=now)
        paid.append((day, item, count))

    state["login_claimed_day"] = day
    state["login_book"] = book
    # Announce the check-in exactly ONCE -- see login_bonus_json's cin_day note.
    state["login_checkin_day"] = day
    state["login_total_days"] = int(state.get("login_total_days", 0)) + 1
    state["login_last_day"] = today
    # "Day N Log in!" -- an Achievements family (case 9), counted in LIFETIME days, not
    # consecutive ones. Set rather than increment so an account that logged in before
    # this was wired lands on its real total instead of starting from zero.
    bump_quest_counter(state, QUEST_CASE_LOGIN_DAYS,
                       to=int(state["login_total_days"]))
    return paid


def login_bonus_json(state):
    """strargs[0] of the login-bonus sync (cmd 17). Exactly one string, or the handler
    bails."""
    now = int(time.time())
    claimed = int(state.get("login_claimed_day", 0))
    book = int(state.get("login_book", 1))
    total_days = len(_ladder())
    # dcnt is the rung being worked on, so it leads `claimed` by one -- see the note
    # above. It must stay inside the ladder or _curBonusWeek runs off the tab strip.
    day = min(claimed + 1, total_days) if total_days else 1
    group = bt.dd.rows("login_bonus_group").get(LOGIN_BONUS_GROUP, {})
    return json.dumps({
        "group_data": [{
            "group": LOGIN_BONUS_GROUP,
            "dcnt": day,
            "bcnt": book,
            "tcnt": total_days,
            "duration_s": now - LOGIN_BONUS_WINDOW,
            "duration_e": now + LOGIN_BONUS_WINDOW,
            "cin_book": book,
            # cin_day is a ONE-SHOT "you just checked in", not standing state. In
            # PlayerLoginBonus.OnClientCmdReceived's cmd-17 branch the GroupInfo field at
            # +44 -- cin_day -- is the gate on `EnqueueBonusGroup`, and a non-empty queue
            # is what makes SceneMain.SetCurUIDirty re-open the panel. Reporting it every
            # sync therefore re-opens the login bonus on every panel switch. It also
            # drives the check-in ANIMATION: isShowAnimMode reads cin_book/cin_day where
            # the normal path reads bcnt/dcnt. So: the day just paid on the sync that
            # follows the rollover, 0 on every sync after that.
            "cin_day": int(state.get("login_checkin_day", 0)),
            "banner": group.get("_banner_id") or 0,
            "text_id": group.get("_text_id") or 0,
            "close": 0,
        }],
        "active": LOGIN_BONUS_GROUP,
        # midnight UTC, the same boundary advance_login_bonus rolls on
        "nextreset": (now // 86400 + 1) * 86400,
        "total_day": int(state.get("login_total_days", claimed)),
    }, separators=(",", ":"))

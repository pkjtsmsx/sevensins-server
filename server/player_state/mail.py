"""Mail.

Split out of the former monolithic core.py; depends only on .core.
"""


import json, time

from .core import (
    grant_reward,
    uid,
)



# ---- mail -----------------------------------------------------------------
# PlayerMail's dispatcher switch: replies are EVEN, requests ODD --
#   rply 2  SetMailList          <- req 3  (list)
#   rply 4  set_UnreadMailCount
#   rply 6  (unidentified)
#   rply 8  ReceiveAllAttachments <- req 5 (receive one) / 7 (receive all)
#   rply 10 DeleteMail            <- req 9
#
# **The list is CHUNKED**: `SetMailList(id, dataEnd, strArg)` appends strArg to a
# per-request-id buffer and only parses when `dataEnd` (intargs[1]) == 1. The old stub
# sent [0, 0], so it never finalised -- harmless only because it also sent "[]".
#
# A mail element's keys are the numeric strings "1".."10" (read off the JObject lookups
# in SetMailList), NOT names:
#   "1"  formatId  -> the DesignMailForm row, which supplies subject/content art
#   "2"  uid       -> string id, echoed back when claiming
#   "3"  read      -> bool; false puts it in UnreadMailList
#   "4"  content override
#   "5"  timestamp
#   "6"  nested object whose "7" is the attachment dict {item id: count}
#   "8"  custom    -> replaces the literal "{custom}" in the row's subject/content
#   "9"  expired   -> bool
#   "10" updatetime
# "{attachment}" in the row text is replaced by Mail.GetAttachmentString(). Nested
# values are read with ToString() then re-parsed, so plain nested JSON objects work;
# ConvertJsonTable only exists to rewrite an empty "[]" into "{}".
MAIL_SUBJECT_CUSTOM = ""


def _mail_json_one(m):
    return {
        "1": m.get("format_id", 0),
        "2": str(m.get("uid", "")),
        "3": bool(m.get("read", False)),
        "4": m.get("content", ""),
        "5": int(m.get("timestamp", 0)),
        "6": {"7": {str(k): int(v) for k, v in (m.get("items") or {}).items()}},
        "8": str(m.get("custom", MAIL_SUBJECT_CUSTOM)),
        "9": bool(m.get("expired", False)),
        "10": int(m.get("updatetime", 0)),
    }


def mail_list_json(state):
    """strargs[0] of the mail-list reply (cmd 2). Send it with dataEnd = 1."""
    return json.dumps([_mail_json_one(m) for m in state.get("mail", [])],
                      separators=(",", ":"))


def unread_mail_count(state):
    return sum(1 for m in state.get("mail", []) if not m.get("read"))


def claim_mail(state, uids=None):
    """Grant the attachments of the named mails (or every unread one when uids is None)
    and mark them read.

    The reply to a receive request is cmd 8 carrying **the list of claimed uids**;
    `ReceiveAllAttachments` deserialises it as List<string> and, for each match, marks the
    mail read, moves it to the read list and decrements the unread count. It does NOT
    grant anything -- it only pops the item display -- so the granting is ours, and the
    matching syncs have to be pushed like every other reward.

    Returns (claimed_uids, buckets_touched).
    """
    claimed, buckets = [], set()
    for m in state.get("mail", []):
        if m.get("read") or m.get("expired"):
            continue
        if uids is not None and str(m["uid"]) not in {str(u) for u in uids}:
            continue
        for item_id, count in (m.get("items") or {}).items():
            buckets.add(grant_reward(state, int(item_id), int(count)))
        m["read"] = True
        m["updatetime"] = int(time.time())
        claimed.append(str(m["uid"]))
    return claimed, buckets


def add_mail(state, format_id, items=None, custom="", timestamp=None):
    """Queue a mail. `items` is {item_id: count} and is what the player claims."""
    mails = state.setdefault("mail", [])
    uid = str(max((int(m["uid"]) for m in mails), default=0) + 1)
    mails.append({
        "uid": uid, "format_id": int(format_id), "items": dict(items or {}),
        "custom": str(custom), "read": False, "expired": False,
        "timestamp": int(timestamp if timestamp is not None else time.time()),
        "updatetime": int(time.time()),
    })
    return uid

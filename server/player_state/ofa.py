"""OFA: lobby entries and the ad-banner strip.

Split out of the former monolithic core.py; depends only on .core.
"""


import json, time
import battle as bt



# ---- OFA (lobby entries and the ad-banner strip) ---------------------------
# `HandleSyncStaticOFABanner` (cmd 257) reads
#   strargs[0] -> List<OFABannerData>, sorted then indexed into StaticBannerDic by `id`
#   strargs[1] -> Dictionary<int,int> QuestToOFAMapping
# OFABannerData wire keys (from the JsonProperty thunks, none match the field names
# except type/status): **idx** (Index), **bgt** (BeginTime), **edt** (EndTime),
# **id** (OFAID), **type**, **strarg**, **status**.
#
# `id` refers to a `oneforall` (DesignOFAForm) row, which carries the art and the prefab
# variant: {_banner, _smbanner, _prefabName_v2, _group, _quest_cond, _shop_cond}.
# `PlayerOFA.RenewLobbyEntry` rebuilds groups **201** and **202** -- those are the lobby
# entry buttons (TriggerShop-*/BulletinShop-*), which is why our lobby is missing the
# NOVICE ULTRA PACK / SIGNUP REWARD / WEEKLY BEST SELLER row. `UIMainAdBanner` counts
# QuestToOFAMapping, so an empty one is the `_curBannerIndex out of range, idx=0` warning
# and the blank banner strip.
#
# Deliberately starting with a SHORT list: each entry pulls a prefab variant out of the
# bundles, and a missing prefab is exactly the kind of thing that has hung panels all day.
# Grow OFA_ENTRIES once a small set is proven to render.
# Verified on device 2026-08-03: these two produce real bulletin TABS with the correct
# artwork (リンボランド ★7日遊園体験 and 大罪×魔星 7日間パック, straight off each row's
# `_banner`), and the sync raises no exception. What is still blank is the bulletin's
# CONTENT PANE -- an OFABannerData entry is enough to create the entry and its art, but
# the body comes from the prefab variant's own data source (`strarg`, and the shop/quest
# state each variant reads), none of which we serve yet.
# Groups 201 and 202 are exactly what `RenewLobbyEntry` rebuilds, i.e. the lobby entry
# buttons the real game shows (NOVICE ULTRA PACK / SIGNUP REWARD / WEEKLY BEST SELLER /
# DEMON DESCENDED) and which ours is missing entirely. Group 1 holds the bulletin
# entries. Published wholesale rather than hand-picked, since the aim is to match the
# original lobby.
OFA_ENTRY_GROUPS = (1, 201, 202)


def _ofa_entry_ids():
    rows = bt.dd.rows("oneforall")
    return tuple(sorted(k for k, v in rows.items()
                        if v.get("_group") in OFA_ENTRY_GROUPS))


# Publishing ALL 55 rows of those groups threw `Index was out of range` out of an event
# handler (2026-08-03), so some entry in the set needs data we do not serve. Two entries
# are known good and render real bulletin tabs with correct art:
#   1     group 1   variant/BulletinQuest-Novice7Day
#   10511 group 202 common/BulletinComp-7DaySubscription_JP
# Set to None to publish every row in OFA_ENTRY_GROUPS once the bad entry is found --
# bisecting the 55 would identify it.
# 10511 was DROPPED 2026-08-05: it has no row in the EN 2.2.7 pack at all (group None,
# empty), which is the source of the recurring `集成式介面沒有row ID: 10511` warning. It
# rendered a second, Japanese-labelled roulette entry in the lobby that opened the SAME
# wheel as the real one -- live footage at EoS shows only one. It is a JP-era leftover we
# kept publishing after the EN migration.
OFA_ENTRIES = (1,)        # None = every row in OFA_ENTRY_GROUPS; () = publish nothing
OFA_QUEST_MAP = {}        # quest id -> OFA id, drives the lobby ad-banner strip

# EVENT banners (cmd 258) are a separate list from the static ones, and they are what
# gates the lobby roulette button: `HandleSyncEventOFABanner`'s RefreshGroupedDic
# (0x1960D3C) buckets each entry under its `oneforall` row's `_group`, and
# MenuBtnUpdater.UpdateRoulette hides the button whenever that group is empty or expired.
#
# Row **200019** (`common_ex+/TriggerEvent-FreeRoulette`) is the only member of group
# **213** in the EN 2.2.7 pack -- it is the roulette entry. Publishing it lights up the
# type-14 button whose prefab-authored Param1 is 213 and leaves the other one (a
# different, empty group) disabled, which is the single-entry lobby live footage shows.
OFA_EVENT_ENTRIES = (200019,)


def _ofa_content_strarg(ofa_id):
    """The banner's `strarg` -- it is NOT a free-text argument, it is the CONTENT.

    `OFABannerData..ctor(string strarg)` (0x195F004) is the only constructor, so
    Newtonsoft uses it and binds the `strarg` property to it; the ctor then does
    `DeserializeObject<OFAContentData>(strarg)` into the `ContentData` field (+0x38).
    Nothing else ever assigns ContentData -- cmd 259 does not. So an empty `strarg`
    leaves it NULL, and `PanelOneForAllMenu.OnSyncOFAContent` (0x158E360) ends with
    `CreateContent(banner.ContentData)`, which dereferences it: that was the
    `event execution error, message=Object reference not set to an instance of an object`
    and the black screen behind the roulette entry.

    `CreateContent` (0x158E534) only needs **id**: it does `DesignOFAForm.GetRow(OFAID)`,
    takes that row's prefab-name field, localizes it through
    `DesignPanelLocalizeForm.GetLocalizedPanelName`, and loads it.

    Wire keys from the 2.2.4 JsonProperty thunks (tools/read_json_keys.py):
    OFAContentData = **id / type / jstr / qstr / pstr** (OFAID, Type, JumpStr, QuestStr,
    ProgressStr). The same run reproduces OFABannerData's known idx/bgt/edt/id/type/
    strarg/status, which is what validates the method.
    """
    return json.dumps({"id": int(ofa_id), "type": 0,
                       "jstr": "", "qstr": "", "pstr": ""},
                      separators=(",", ":"))


def ofa_static_banner_json():
    """strargs[0] of cmd 257 -- the static banner list."""
    now = int(time.time())
    rows = bt.dd.rows("oneforall")
    out = []
    ids = _ofa_entry_ids() if OFA_ENTRIES is None else OFA_ENTRIES
    for i, ofa_id in enumerate(ids):
        if ofa_id not in rows:
            continue
        out.append({"idx": i, "bgt": 0, "edt": now + 365 * 24 * 3600,
                    "id": int(ofa_id), "type": 0,
                    "strarg": _ofa_content_strarg(ofa_id), "status": 0})
    return json.dumps(out, separators=(",", ":"))


def ofa_event_banner_json():
    """strargs[0] of cmd 258 -- the EVENT banner list (same OFABannerData shape).

    `status` MUST be non-zero. Unlike the static list, the cmd-258 handler folds each
    parsed banner into `EventBannerDic` by OFAID and, when `Status` (OFABannerData+0x30)
    is 0, *removes* the key instead of setting it -- so a status-0 entry is accepted,
    logged clean, and then silently dropped before RefreshGroupedDic runs. Read off the
    disassembly at 0x196079C..0x19607D4; it is why `status: 0` left group 213 empty and
    the roulette button hidden even though the reply was going out correctly.

    `edt` must be comfortably in the future: UpdateRoulette disables the button when
    GetNearestUnixEndTime(group) - now < 1 and no entry in the group is permanent.
    """
    now = int(time.time())
    rows = bt.dd.rows("oneforall")
    out = []
    for i, ofa_id in enumerate(OFA_EVENT_ENTRIES):
        if ofa_id not in rows:
            continue
        out.append({"idx": i, "bgt": 0, "edt": now + 365 * 24 * 3600,
                    "id": int(ofa_id), "type": 0,
                    "strarg": _ofa_content_strarg(ofa_id), "status": 1})
    return json.dumps(out, separators=(",", ":"))


def ofa_shop_cond(ofa_id):
    """The `oneforall` row's `_shop_cond` -- the shop id cmd 259 must name.

    0 for entries with no shop attached (the roulette, 200019). The client only uses it
    to make sure PlayerShop.ShopDic has an entry, so an unknown id is harmless.
    """
    row = bt.dd.rows("oneforall").get(int(ofa_id)) or {}
    return int(row.get("_shop_cond") or 0)


def ofa_quest_map_json():
    """strargs[1] of cmd 257 -- QuestToOFAMapping."""
    return json.dumps({str(k): int(v) for k, v in OFA_QUEST_MAP.items()},
                      separators=(",", ":"))

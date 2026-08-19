#!/usr/bin/env python3
"""The guild, end to end on the state layer.

What these checks are actually protecting:

  * the JSON WIRE KEYS. Every one of them was read out of a [JsonProperty] attribute
    thunk in libil2cpp.so and none of them is the C# field name -- `OwnerName` is
    `owner_name`, `signRewardTbl` is `signReward`, `MemberCount` is `mbCnt`. A rename
    here does not fail loudly; Newtonsoft just leaves the field null and the panel
    renders blank or throws deep inside NGUI.
  * the two shapes that make the client THROW rather than degrade: `signSum` must be
    non-empty (RequestServerInfoUpdate reads signSum[0] on every panel open) and the
    signReward arrays must be at least as long as PanelGuild's five `_signRewards`
    prefab slots.
  * every key of MemberMongo appearing in MemberRedis, which fillGuildMemberData
    assumes with a bare Dictionary get_Item.
  * Guild Pt actually landing in `currency` 64. It is item 4, an `_action 5` currency
    item -- paying it through grant_item instead would put a phantom stack in the bag
    and leave the shop unable to spend it.

    python3 test_guild.py
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

_TMP = tempfile.mkdtemp(prefix="sevensins-guild-test-")
os.environ["SEVENSINS_ACCOUNTS"] = _TMP

import player_state as ps                              # noqa: E402
from player_state import guild as gd                   # noqa: E402
from player_state.core import _default, _seed_roster   # noqa: E402

_fail = 0


def check(name, cond, detail=""):
    global _fail
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        _fail += 1


def fresh():
    st = _default(1000001)
    _seed_roster(st)
    st["currency"]["16"] = 10 ** 6      # Mira, to afford the 200k creation cost
    return st


def with_guild():
    st = fresh()
    ok, _ = gd.create_guild(st, "Seven Sinners", "come one come all", 3, 1)
    assert ok
    return st


def check_create():
    st = fresh()
    check("a fresh account is in no guild", not gd.have_guild(st))
    check("gStatus is none", gd.guild_status(st) == 0)

    # CostValue.NeedMira -- the real server charged it, so we do.
    st["currency"]["16"] = 199999
    ok, errno = gd.create_guild(st, "Too Poor", "", 0, 0)
    check("creation is refused without the Mira", not ok)
    check("  ...as cost_fail (3107)", errno == gd.ERR_GUILD_COST_FAIL, str(errno))

    st["currency"]["16"] = 250000
    ok, errno = gd.create_guild(st, "", "", 0, 0)
    check("an empty name is refused", not ok and errno == gd.ERR_GUILD_NAME_LEN,
          str(errno))

    ok, errno = gd.create_guild(st, "Seven Sinners", "hi", 3, 1)
    check("creation succeeds", ok, str(errno))
    check("  ...and charges exactly 200,000 Mira",
          st["currency"]["16"] == 50000, str(st["currency"]["16"]))
    check("gStatus is now member (1)", gd.guild_status(st) == 1)

    ok, errno = gd.create_guild(st, "Another", "", 0, 0)
    check("a second guild is refused as had_guild (3111)",
          not ok and errno == gd.ERR_GUILD_HAD_GUILD, str(errno))


def check_guild_json():
    st = with_guild()
    g = json.loads(gd.guild_json(st))

    # These are the [JsonProperty] names, NOT the field names. See the module docstring.
    want = {"uid", "name", "owner_name", "owner_uid", "createTime", "ad_text",
            "announce", "signSum", "editTime", "badge", "joinFree", "Area",
            "updatetime", "log", "signReward"}
    check("GuildInfo carries exactly the wire keys the client reads",
          set(g) == want, str(set(g) ^ want))
    check("owner_uid is the player's own uid", g["owner_uid"] == ps.uid(st))
    check("owner_name is the player's name", g["owner_name"] == st["name"])

    # RequestServerInfoUpdate does signSum[0] before it sends anything, and PanelGuild
    # fires it from onActive -- an empty list throws on every open of the panel.
    check("signSum is a NON-EMPTY list", isinstance(g["signSum"], list)
          and len(g["signSum"]) >= 1, str(g["signSum"]))

    sr = g["signReward"]
    check("signReward uses idList/cntList/limitList",
          set(sr) == {"idList", "cntList", "limitList"}, str(set(sr)))
    # PanelGuild._countTodaySignCnt indexes limitList[i] for i over _signRewards,
    # which the prefab bakes at 5.
    for k in ("idList", "cntList", "limitList"):
        check(f"  signReward.{k} covers all 5 prefab slots", len(sr[k]) >= 5,
              f"{len(sr[k])}")
    check("the ladder is reachable by ONE member",
          max(sr["limitList"][:5]) <= 1 * len(sr["limitList"][:5]),
          str(sr["limitList"]))


def check_members_json():
    st = with_guild()
    m = json.loads(gd.guild_members_json(st))
    check("MembersInfo carries all four dicts",
          set(m) == {"memberMongo", "applierMongo", "memberRedis", "applierRedis"},
          str(set(m)))
    # fillGuildMemberData walks memberMongo's keys and does MemberRedis[key] with no
    # containment check -- a missing key is a KeyNotFoundException, not a blank row.
    check("every memberMongo key has a memberRedis entry",
          set(m["memberMongo"]) <= set(m["memberRedis"]),
          str(set(m["memberMongo"]) - set(m["memberRedis"])))
    check("the applier dicts are present-and-empty, not absent",
          m["applierMongo"] == {} and m["applierRedis"] == {})

    me = m["memberMongo"][ps.uid(st)]
    check("the player is Director (rank 1)", me["rank"] == gd.GUILD_RANK_DIRECTOR,
          str(me))
    check("MongoMember uses rank/ctb/time/sign/challengeTopScore",
          set(me) == {"rank", "ctb", "time", "sign", "challengeTopScore"}, str(set(me)))

    info = m["memberRedis"][ps.uid(st)]
    # rid/rlv/rstar/rplus/rhp/ratk/rsuper_star are helper[0][0..6]; a short inner list
    # means the member row draws a cast with no portrait.
    check("PlayerInfo.helper[0] has all SEVEN entries",
          len(info["helper"][0]) == 7, str(info["helper"]))
    check("  ...and names a real cast", info["helper"][0][0] > 0, str(info["helper"]))
    check("PlayerInfo uses the short wire keys (ptype/pid/utime/gname/f_skill)",
          {"ptype", "pid", "utime", "gname", "f_skill"} <= set(info), str(set(info)))
    check("guildName is carried as gname", info["gname"] == "Seven Sinners",
          info["gname"])


def check_card_json():
    st = with_guild()
    c = json.loads(gd.guild_card_json(st))
    check("GuildCardInfo uses mbCnt/ad_text/dayRank/btRank",
          set(c) == {"name", "owner_name", "badge", "mbCnt", "ad_text",
                     "dayRank", "btRank", "createTime"}, str(set(c)))
    check("the member count is 1", c["mbCnt"] == 1)


def check_sign():
    """The daily check-in. This is the only Guild Pt faucet in the whole server."""
    st = with_guild()
    check("the account starts with no Guild Pt", st["currency"]["64"] == 0)

    total, earned = gd.guild_sign(st)
    check("signing once counts one", total == 1, str(total))
    check("  ...and crosses the first tier", earned == [(4, 20)], str(earned))
    # Item 4 is `_action 5` with `_param1 64`: a CURRENCY, so it must NOT be a bag item.
    check("Guild Pt lands in currency 64", st["currency"]["64"] == 20,
          str(st["currency"]["64"]))
    check("  ...and NOT in the backpack", ps.item_count(st, 4) == 0)

    # PanelGuild fires this from onActive on every open, not from a button, so the
    # second call of the day has to be a no-op that still reports the count.
    total2, earned2 = gd.guild_sign(st)
    check("signing again the same day pays nothing", earned2 == [], str(earned2))
    check("  ...and does not double-count", total2 == 1, str(total2))
    check("  ...leaving the balance alone", st["currency"]["64"] == 20)

    # The 4AM rollover is the same one every other daily in the server shares.
    st["guild"]["sign_day"] = "1999-01-01"
    total3, earned3 = gd.guild_sign(st)
    check("a new day resets the count", total3 == 1, str(total3))
    check("  ...and re-arms the ladder", earned3 == [(4, 20)], str(earned3))
    check("  ...paying again", st["currency"]["64"] == 40, str(st["currency"]["64"]))


def check_edit_and_quit():
    st = with_guild()
    ok, _ = gd.guild_edit(st, gd.GUILD_EDIT_AD, ["new advert"])
    check("the advert can be edited", ok and st["guild"]["ad"] == "new advert")
    ok, _ = gd.guild_edit(st, gd.GUILD_EDIT_ANNOUNCE, ["read this"])
    check("the announcement can be edited",
          ok and st["guild"]["announce"] == "read this")
    # Handing the directorship over needs somebody to hand it to.
    ok, errno = gd.guild_edit(st, gd.GUILD_EDIT_OWNER, ["x", "y"])
    check("transferring ownership is refused as no_permission (3114)",
          not ok and errno == gd.ERR_GUILD_NO_PERMISSION, str(errno))

    ok, _ = gd.guild_set_setting(st, 7, 0)
    check("badge/joinFree can be set",
          ok and st["guild"]["badge"] == 7 and st["guild"]["join_free"] == 0)

    ok, _ = gd.quit_guild(st)
    check("quitting works", ok and not gd.have_guild(st))
    check("  ...dropping gStatus back to none", gd.guild_status(st) == 0)
    ok, errno = gd.quit_guild(st)
    check("quitting twice is guild_nil (3106)",
          not ok and errno == gd.ERR_GUILD_NIL, str(errno))


def check_persistence():
    """`guild` is a key added after existing accounts were written; load() backfills
    defaults with setdefault, so an old save must come back guild-less, not broken."""
    st = with_guild()
    ps.save(st)
    again = ps.load(1000001)
    check("the guild survives a save/load round trip",
          gd.have_guild(again) and again["guild"]["name"] == "Seven Sinners",
          str(again.get("guild")))

    del again["guild"]
    ps.save(again)
    old = ps.load(1000001)
    check("an account written before the guild key loads clean",
          "guild" in old and old["guild"] is None, str(old.get("guild")))



def check_sign_reward_labels():
    """`SignRewardTbl.CountList` is a RUNNING TOTAL, not the per-tier amount.

    `UISignReward.SetRewardData` renders slot 0 as `CountList[0]` and every later slot
    as `"+" + (CountList[N] - CountList[N-1])` -- the INCREMENT between consecutive
    entries. Sending our per-tier list straight through made the Signup Bonus row read
    "20 / +0 / +10 / +0 / +20" (the differences of 20,20,30,30,50) instead of the
    intended 20/+20/+30/+30/+50. Nothing errors; the numbers are just wrong.
    """
    st = with_guild()
    tbl = json.loads(ps.guild_json(st))["signReward"]
    cnt = tbl["cntList"]
    check("cntList is strictly increasing (it is cumulative)",
          all(b > a for a, b in zip(cnt, cnt[1:])), str(cnt))
    # Transcribe SetRewardData exactly and compare against what each tier pays.
    rendered = [cnt[0]] + [cnt[i] - cnt[i - 1] for i in range(1, len(cnt))]
    check("what the client renders equals what each tier pays",
          rendered == list(gd.GUILD_SIGN_REWARD_COUNTS),
          f"{rendered} vs {list(gd.GUILD_SIGN_REWARD_COUNTS)}")
    check("  ...and no tier renders as +0",
          all(v > 0 for v in rendered[1:]), str(rendered))
    # All three lists are indexed by the same slot, so they must stay the same length.
    check("idList/cntList/limitList agree in length",
          len({len(tbl["idList"]), len(cnt), len(tbl["limitList"])}) == 1,
          str({k: len(v) for k, v in tbl.items()}))

    # The payout is per-tier and must NOT become cumulative.
    total_paid = 0
    for i in range(len(gd.GUILD_SIGN_LIMITS)):
        st["guild"]["sign_sum"] = gd.GUILD_SIGN_LIMITS[i]
        _sum, earned = gd.guild_sign(st)
        total_paid += sum(c for _i, c in earned)
    check("signing all the way up pays the cumulative total once",
          total_paid == cnt[-1], f"{total_paid} vs {cnt[-1]}")


def main():
    for fn in (check_create, check_guild_json, check_members_json, check_card_json,
               check_sign, check_sign_reward_labels, check_edit_and_quit,
               check_persistence):
        print(f"\n{fn.__name__}:")
        fn()
    print(f"\n{_fail} failure(s)")
    return 1 if _fail else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        import shutil
        shutil.rmtree(_TMP, ignore_errors=True)

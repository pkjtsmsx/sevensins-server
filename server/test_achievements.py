#!/usr/bin/env python3
"""The Achievements tab (`_type 2`, `_group 20`) -- 476 rows.

Asked for 2026-08-18. The important finding is that **most of them were never ours to
fix**: `PlayerQuest.GetQuestValue` (0x1963020) opens with

    if ((unsigned)(case_id - 1001) < 0x3E8) return GetQuestCntFromDataType1(...)

so every case in **1001..2000** is computed CLIENT-side from data it already holds --
1001 account rank, 1002 story clears, 1003 perfect clears, 1005. That is why "Advance
to Rank 16" reads 15/16 (the account's real rank) while our own `1001_0` counter sits
at 12: the client never looks at it. Those ~260 rows work as long as the underlying
state does.

The rest come back through the ordinary `quest_db` counter, and three families had
nothing bumping them at all:

    6   obtain starshard `_case_v1`   -> store_rune, the choke point every shard passes
    9   lifetime login days           -> advance_login_bonus
    11  Karma rank-ups performed      -> grant_karma, counting every rank crossed

(21, rune upgrade levels, was already wired. 2 and 32 are arena entries and wins --
no arena subsystem, so those 84 rows stay unreachable and that is honest.)

    python3 test_achievements.py
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

_TMP = tempfile.mkdtemp(prefix="sevensins-achv-test-")
os.environ["SEVENSINS_ACCOUNTS"] = _TMP

import battle as bt                                     # noqa: E402
import player_state as ps                               # noqa: E402

_fail = 0
CLIENT_COMPUTED = range(1001, 2001)     # GetQuestValue's own range test


def check(name, cond, detail=""):
    global _fail
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        _fail += 1


def achievements():
    return [r for r in bt.dd.rows("quest").values()
            if r.get("_type") == 2 and r.get("_group") == 20]


def check_the_split():
    ach = achievements()
    check("the Achievements tab is 476 rows", len(ach) == 476, str(len(ach)))
    client = [r for r in ach if r.get("_case_id") in CLIENT_COMPUTED]
    server = [r for r in ach if r.get("_case_id") not in CLIENT_COMPUTED]
    check("most are client-computed (case 1001..2000)", len(client) > len(server),
          f"{len(client)} client vs {len(server)} server")
    check("  ...and the rank family is among them",
          all(r.get("_case_id") in CLIENT_COMPUTED
              for r in ach if "Advance to Rank" in (r.get("_name_en") or "")))
    # Everything server-side must be a case we either drive or knowingly cannot.
    ours = {ps.QUEST_CASE_OBTAIN_RUNE, ps.QUEST_CASE_LOGIN_DAYS,
            ps.QUEST_CASE_KARMA_RANKUP, ps.QUEST_CASE_RUNE_LEVELS_TOTAL}
    arena = {2, 32}
    unknown = {r.get("_case_id") for r in server} - ours - arena
    check("every server-side achievement case is accounted for", not unknown,
          str(sorted(unknown)))
    check("  ...and the only unreachable ones are arena",
          {r.get("_case_id") for r in server} & arena == arena)


def check_obtain_a_starshard():
    st = ps.load(1000060)
    st["backpack"]["2"] = {}
    key = f"{ps.QUEST_CASE_OBTAIN_RUNE}_201101"
    check("nothing credited before the shard exists", not st["quest_db"].get(key))
    ps.grant_rune(st, 201101, 1)
    check("granting ★1 Endearment ① credits that exact shard",
          st["quest_db"].get(key) == 1, str(st["quest_db"].get(key)))
    # `_case_v1` is a discriminator: another shard must not credit this one.
    ps.grant_rune(st, 201201, 2)
    check("  ...and a DIFFERENT shard does not credit it",
          st["quest_db"].get(key) == 1, str(st["quest_db"].get(key)))
    check("  ...it credits its own instead",
          st["quest_db"].get(f"{ps.QUEST_CASE_OBTAIN_RUNE}_201201") == 1)
    # Every "Starshards Hunter" row must name a shard we can actually produce.
    rows = [r for r in achievements()
            if r.get("_case_id") == ps.QUEST_CASE_OBTAIN_RUNE]
    missing = [r["_case_v1"] for r in rows if not bt.dd.row("item", r.get("_case_v1"))]
    check("every Starshards Hunter names a real shard", not missing, str(missing[:3]))


def check_login_days():
    st = ps.load(1000061)
    st["login_total_days"] = 0
    st.pop("login_last_day", None)
    st["quest_db"].pop(f"{ps.QUEST_CASE_LOGIN_DAYS}_0", None)
    list(ps.advance_login_bonus(st))
    key = f"{ps.QUEST_CASE_LOGIN_DAYS}_0"
    check("a login credits the lifetime day count", st["quest_db"].get(key) == 1,
          str(st["quest_db"].get(key)))
    # It is a high-water SET, so an account with history lands on its real total.
    st["login_total_days"] = 40
    st.pop("login_last_day", None)
    list(ps.advance_login_bonus(st))
    check("  ...and an account with history lands on its real total",
          st["quest_db"].get(key) == 41, str(st["quest_db"].get(key)))
    # Twice in one day must not double-count.
    before = st["quest_db"].get(key)
    list(ps.advance_login_bonus(st))
    check("  ...but a second login the same day does not",
          st["quest_db"].get(key) == before, str(st["quest_db"].get(key)))


def check_karma_rankups():
    st = ps.load(1000062)
    key = f"{ps.QUEST_CASE_KARMA_RANKUP}_0"
    st["quest_db"].pop(key, None)
    was = int(ps.karma_of(st, 10001).get("flv", 0))
    k = ps.grant_karma(st, 10001, 10 ** 5)
    # Ranks GAINED, not the rank landed on -- a cast starts at rank 1, so reaching
    # rank 30 is 29 rank-ups, and the achievement counts the increases.
    check("a multi-rank karma gain credits EVERY rank crossed",
          st["quest_db"].get(key) == k["flv"] - was,
          f"{st['quest_db'].get(key)} vs {k['flv']} - {was}")
    check("  ...which is many, not one", k["flv"] - was > 5, str(k["flv"] - was))
    # xp that crosses nothing credits nothing.
    before = st["quest_db"].get(key)
    ps.grant_karma(st, 10001, 1)
    check("  ...and xp that crosses no rank credits nothing",
          st["quest_db"].get(key) == before, str(st["quest_db"].get(key)))


def check_arena_is_honestly_dead():
    """84 rows we cannot drive. Better named than silently pretended-at."""
    rows = [r for r in achievements() if r.get("_case_id") in (2, 32)]
    check("the arena achievements are 84 rows", len(rows) == 84, str(len(rows)))
    st = ps.load(1000063)
    check("  ...and nothing in the server claims to move them",
          not any(k.startswith(("2_", "32_")) for k in st["quest_db"]),
          str([k for k in st["quest_db"] if k.startswith(("2_", "32_"))]))


def main():
    for fn in (check_the_split, check_obtain_a_starshard, check_login_days,
               check_karma_rankups, check_arena_is_honestly_dead):
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

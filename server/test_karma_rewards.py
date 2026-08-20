#!/usr/bin/env python3
"""Karma (Kizuna) rank-ups pay their Rank Bonus.

Reported 2026-08-18: "when casts level up their karma ranking, we're not getting this
pop up nor the rank rewards". `grant_karma` advanced flv/fxp and stopped there -- it
never looked at the reward table, so every Rank Bonus row was silently skipped.

The rewards are in `char_flv`, one row per (cast, rank), discriminated by
`_unlock_type`:

    5 -> pays `_bonus_item_id` x `_bonus_item_cnt`   (2416 rows -- ours to hand over)
    2 -> unlocks a Kizuna Quest                      (the client's own gating)
    3 / 6 -> stat bonuses the client derives itself

Only type 5 is a payout. Screenshot of Lucifer's list, for the record: rank 1 Kizuna
Quest, rank 2 Diamond x50, rank 3 HP Increase, rank 4 Kizuna Quest, ranks 5/6
Diamond x50, rank 7 Holy Blood x35 -- which is exactly what the table yields.

**One grant can cross several ranks**, and every crossed rank pays.

    python3 test_karma_rewards.py
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

_TMP = tempfile.mkdtemp(prefix="sevensins-karma-test-")
os.environ["SEVENSINS_ACCOUNTS"] = _TMP

import battle as bt                                     # noqa: E402
import player_state as ps                               # noqa: E402
import titan_server as ts                               # noqa: E402

_fail = 0
LUCIFER = 10001


def check(name, cond, detail=""):
    global _fail
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        _fail += 1


def check_table_matches_the_screenshot():
    want = {2: [(1, 50)], 5: [(1, 50)], 6: [(1, 50)], 7: [(3, 35)],
            1: [], 3: [], 4: []}
    for rank, expect in sorted(want.items()):
        got = ps.karma_rank_rewards(LUCIFER, rank - 1, rank)
        check(f"Lucifer rank {rank} pays {expect or 'nothing'}", got == expect, str(got))
    check("only `_unlock_type` 5 rows are treated as payouts",
          ps.KARMA_UNLOCK_ITEM == 5, str(ps.KARMA_UNLOCK_ITEM))
    # Every cast with a table must have at least one paying rank, or the whole
    # mechanic is dead for them.
    chars = {int(r["_char_id"]) for r in bt.dd.rows("char_flv").values()
             if r.get("_char_id")}
    payless = [c for c in sorted(chars)[:40]
               if not ps.karma_rank_rewards(c, 0, 99)]
    check("casts with a Karma table have paying ranks", not payless,
          str(payless[:5]))


def check_a_single_rank_up_pays_once():
    st = ps.load(1000030)
    st["karma"] = {str(LUCIFER): {"flv": 1, "fxp": 0}}
    before = int(st["currency"]["1"])
    need = ps.char_flv_need_xp(
        (bt.dd.row("char", LUCIFER) or {}).get("_rarity"), 1)
    k = ps.grant_karma(st, LUCIFER, need)
    check("the rank advances by one", k["flv"] == 2, str(k["flv"]))
    check("  ...and pays only that rank's bonus", k["_paid"] == [(1, 50)], str(k["_paid"]))
    # **Mailed, not granted.** The rank-up splash says so itself -- text 115605,
    # "Sent to mailbox", printed beside the reward on the Karma RANK screen. So the
    # balance must NOT move; the items wait in the mail until claimed.
    check("  ...WITHOUT touching the diamond balance",
          int(st["currency"]["1"]) == before,
          f"{before} -> {st['currency']['1']}")
    mails = st.get("mail") or []
    check("  ...and arriving as mail instead", bool(mails), str(mails))
    check("  ...carrying the 50 diamonds",
          mails and mails[-1]["items"].get("1") == 50, str(mails[-1] if mails else None))
    check("  ...titled with the rank reached",
          mails and mails[-1]["custom"] == "Karma Rank 2", str(mails[-1]["custom"]))


def check_multi_rank_pays_every_rank_crossed():
    """The user's point: you can rank up several times at once."""
    st = ps.load(1000031)
    before = int(st["currency"]["1"])
    k = ps.grant_karma(st, LUCIFER, 10 ** 5)
    check("one grant can cross many ranks", k["flv"] > 5, str(k["flv"]))
    every = ps.karma_rank_rewards(LUCIFER, 0, k["flv"])
    check("  ...and pays every paying rank in the range",
          k["_paid"] == every, f"{len(k['_paid'])} vs {len(every)}")
    diamonds = sum(c for i, c in every if i == 1)
    # Every crossed rank rides in ONE mail -- the splash announces a rank, and several
    # ranks crossed at once should not bury the player in mail.
    mails = st.get("mail") or []
    check("  ...all in a single mail", len(mails) == 1, str(len(mails)))
    check("  ...for the full diamond total",
          mails and mails[-1]["items"].get("1") == diamonds,
          f'{mails[-1]["items"] if mails else None} vs {diamonds}')
    check("  ...which is more than a single rank would pay",
          diamonds > 50, str(diamonds))
    check("  ...and the balance still has not moved",
          int(st["currency"]["1"]) == before,
          f'{before} -> {st["currency"]["1"]}')

    # Ranking up again from there must not re-pay what was already banked.
    mid = len(st.get("mail") or [])
    again = ps.grant_karma(st, LUCIFER, 10 ** 5)
    check("re-granting at max pays nothing twice",
          len(st.get("mail") or []) == mid, str(again["_paid"]))


def check_no_rank_up_pays_nothing():
    st = ps.load(1000032)
    st["karma"] = {str(LUCIFER): {"flv": 2, "fxp": 0}}
    before = int(st["currency"]["1"])
    k = ps.grant_karma(st, LUCIFER, 1)
    check("xp that does not cross a rank pays nothing",
          k["_paid"] == [] and int(st["currency"]["1"]) == before, str(k))


def check_the_payout_is_pushed():
    """A grant the client is never told about may as well not have happened."""
    st = ps.load(1000033)
    k = ps.grant_karma(st, LUCIFER, 10 ** 5)
    msgs = ts.karma_reward_msgs(st, k)
    check("a paying rank-up produces push messages", bool(msgs), str(len(msgs)))
    check("nothing to push when nothing was paid",
          ts.karma_reward_msgs(st, {"_paid": []}) == [])


def check_gifts_report_the_bonus():
    st = ps.load(1000034)
    shape = ps.give_gifts(st, LUCIFER, [])
    check("give_gifts returns (ok, xp, used, rank_paid)", len(shape) == 4, str(shape))
    check("  ...and refuses cleanly with no items", shape[0] is False)

    # **The gift path has to hand the REAL karma dict to karma_reward_msgs.** It used to
    # build a synthetic {"_paid": rank_paid}, which silently dropped `_rows` -- so the
    # 563s never went out and PanelEvilUp closed on an empty queue. karma_of() returns
    # the same dict grant_karma stashed both on.
    st2 = ps.load(1000036)
    st2["karma"] = {str(LUCIFER): {"flv": 1, "fxp": 0}}
    ps.grant_item(st2, 487, 20)
    ok, _xp, _used, paid = ps.give_gifts(st2, LUCIFER, [(487, 10)])
    check("a gift that ranks up succeeds", ok, str(ok))
    k = ps.karma_of(st2, LUCIFER)
    check("  ...and karma_of carries the splash rows", bool(k.get("_rows")),
          str(k.get("_rows")))
    check("  ...and the bonus lines", bool(paid), str(paid))
    msgs = ts.karma_reward_msgs(st2, k)
    check("  ...so the gift path emits the 563s",
          len(msgs) >= len(k["_rows"]), f"{len(msgs)} msgs, {len(k['_rows'])} rows")
    # The old synthetic dict must not be able to produce them -- guards the regression.
    check("  ...which a synthetic {_paid} dict could NOT",
          len(ts.karma_reward_msgs(st2, {"_paid": paid})) < len(msgs))



def check_the_rank_up_splash_is_triggered():
    """The RANK UP splash is a SERVER trigger, not something the client infers.

    `PanelEvilUp.OnPanelDirty` pops one entry off `PlayerChar.FlvLevelUpNotifyList` and
    shows it; on an EMPTY list it calls ClosePanel immediately -- which is exactly the
    "brief flash" the splash was doing. Only `PlayerChar.getFlvRewards` fills that list,
    and it is **cmd 563** with intargs[0] = ONE char_flv row id.

    Chain confirmed from the client: gift -> update_friendly (552) raises CharEvent 3
    (one of 530-533/547/548/551/552/560-562/595/597/598) -> UICharacterRoom.
    OnCharRoomUpdate sees kizunaLevel differ from its snapshot -> PlayKizunaLevelTextEffect
    -> tween group 100 -> LaunchPanelLevelUp -> PanelEvilUp. Every step of that was
    already firing in logcat; only the queue was empty.
    """
    st = ps.load(1000035)
    st["karma"] = {str(LUCIFER): {"flv": 1, "fxp": 0}}
    k = ps.grant_karma(st, LUCIFER, 10 ** 5)
    check("a rank-up reports the crossed char_flv rows", bool(k.get("_rows")),
          str(k.get("_rows")))
    check("  ...in ascending rank order",
          k["_rows"] == sorted(k["_rows"]), str(k["_rows"]))
    # Every crossed rank contributes a row, not just the paying ones.
    rows_all = ps.karma_rank_rows(LUCIFER, 1, k["flv"])
    check("  ...one per rank crossed, paying or not",
          k["_rows"] == rows_all and len(rows_all) >= k["flv"] - 1,
          f"{len(k['_rows'])} rows for {k['flv'] - 1} ranks")
    # ...and they must be real rows the client can resolve in DesignCharFLvForm.
    bad = [r for r in k["_rows"] if not bt.dd.row("char_flv", r)]
    check("  ...and every id resolves in char_flv", not bad, str(bad))

    msgs = ts.karma_reward_msgs(st, k)
    check("the server emits one 563 per crossed rank",
          len(msgs) >= len(k["_rows"]), f"{len(msgs)} msgs for {len(k['_rows'])} rows")
    # **Order matters.** OnPanelDirty consumes one queued row per dirty pass and closes
    # the panel on a pass that finds the queue empty, so anything landing AFTER the
    # splash opens eats the next row. With one rank crossed that is show-then-close.
    tail = msgs[-len(k["_rows"]):]
    check("  ...and they are the LAST messages sent",
          all(m in tail for m in msgs[-len(k["_rows"]):]) and len(tail) == len(k["_rows"]),
          f"{len(tail)} of {len(msgs)}")

    # A grant that crosses nothing must not pop a splash.
    quiet = ps.grant_karma(st, LUCIFER, 0)
    check("no rank-up means no splash rows", not quiet.get("_rows"),
          str(quiet.get("_rows")))



def check_avg_choice_is_one_based_on_the_wire():
    """The AVG sync's locked-option field is 1-BASED; the choice request is 0-based.

    `AvgUIOptions.UpdateAVGOptionLockState`:

        v13 = _replayMode ? 0 : mOptionTag[0]
        if (v13 <= 0):  lock all, then unlock by the digits of mOptionTag[1]
        else:           v15 = 10^(v13 - 1);  unlock ONLY btnOptions[digit - 1]

    -- v13 is a 1-based position and 0 means "undecided", the same convention as the
    unlock digits. `RequestServerAvgSelectOption` however sends a 0-BASED index, so the
    stored value must be shifted on the way out. Echoing it raw broke both cases:
    picking the FIRST option sent 0 and re-opened the entire scene (all options
    selectable again on a replay), and picking any other locked in the option BEFORE the
    one actually chosen.

    Getting this right also delivers the replay skip for free: `OnBtnClick` dispatches
    straight through when `_skipPerform` is set, which the locked-in branch sets.
    """
    st = ps.load(1000037)
    check("an undecided scene reports 0", ps.avg_choice_wire(st, 10102) == 0,
          str(ps.avg_choice_wire(st, 10102)))
    check("  ...and avg_choice says None, not 0",
          ps.avg_choice(st, 10102) is None, str(ps.avg_choice(st, 10102)))

    # Option 0 is a REAL answer -- the first button -- and must not read as undecided.
    ps.set_avg_choice(st, 10102, 0)
    check("picking the FIRST option reports 1, not 0",
          ps.avg_choice_wire(st, 10102) == 1, str(ps.avg_choice_wire(st, 10102)))
    check("  ...which is what stops the scene re-opening",
          ps.avg_choice_wire(st, 10102) > 0)

    ps.set_avg_choice(st, 10103, 2)
    check("the third option reports 3", ps.avg_choice_wire(st, 10103) == 3,
          str(ps.avg_choice_wire(st, 10103)))
    check("  ...and the stored value stays 0-based",
          ps.avg_choice(st, 10103) == 2, str(ps.avg_choice(st, 10103)))

    # Every stored choice must round-trip to the button the player actually pressed.
    for opt in range(4):
        st2 = ps.load(1000038 + opt)
        ps.set_avg_choice(st2, 99000 + opt, opt)
        wire = ps.avg_choice_wire(st2, 99000 + opt)
        check(f"option {opt} -> wire {opt + 1} -> btnOptions[{opt}]",
              wire == opt + 1 and wire - 1 == opt, str(wire))

    # A scene pays once; re-deciding on the same scene is refused.
    check("re-deciding a scene is refused",
          ps.set_avg_choice(st, 10103, 0) is False)
    check("  ...and does not overwrite the original pick",
          ps.avg_choice(st, 10103) == 2, str(ps.avg_choice(st, 10103)))



def check_avg_chapter_character():
    """Each chapter's decisions pay karma to that chapter's cast.

    Chapter 1 is Jacqueline (the tutorial portrait). Chapter 2 is Caillen -- it was
    paying Jacqueline only because every unmapped decision fell through to the default.
    """
    check("chapter 2 is mapped to Caillen",
          ps.KARMA_CHAPTER_CHAR.get(2) == 10981, str(ps.KARMA_CHAPTER_CHAR))
    ch1 = [10101, 10102, 10103, 10107]
    ch2 = [20101, 20202, 20901, 20902, 21001]
    for a in ch1:
        check(f"avg {a} is chapter 1 -> Jacqueline",
              ps.avg_chapter(a) == 1 and ps.karma_reward(a, 0)[2] == 11001,
              f"chapter {ps.avg_chapter(a)} char {ps.karma_reward(a, 0)[2]}")
    for a in ch2:
        check(f"avg {a} is chapter 2 -> Caillen",
              ps.avg_chapter(a) == 2 and ps.karma_reward(a, 0)[2] == 10981,
              f"chapter {ps.avg_chapter(a)} char {ps.karma_reward(a, 0)[2]}")
    # 10102 is played by the client but appears in NO stage's AVG columns -- scenes
    # chain onward from the entry point the stage lists. It must still resolve, via the
    # id's own <chapter><stage><seq> layout.
    check("a mid-chain scene still resolves its chapter",
          ps.avg_chapter(10102) == 1, str(ps.avg_chapter(10102)))
    # The character override must not disturb the amounts of a documented decision.
    cur, amt, cid, fexp = ps.karma_reward(10103, 1)
    check("a documented decision keeps its amounts",
          (amt, fexp) == (10, 20), f"{amt} gems +{fexp}")
    check("  ...and its chapter's cast", cid == 11001, str(cid))
    # Both casts must be real char rows, or the AVG reply names a portrait that fails.
    for cid in set(ps.KARMA_CHAPTER_CHAR.values()):
        check(f"char {cid} is a real cast", bool(bt.dd.row("char", cid)))


def main():
    for fn in (check_table_matches_the_screenshot, check_a_single_rank_up_pays_once,
               check_multi_rank_pays_every_rank_crossed, check_no_rank_up_pays_nothing,
               check_the_payout_is_pushed, check_gifts_report_the_bonus,
               check_the_rank_up_splash_is_triggered,
               check_avg_choice_is_one_based_on_the_wire,
               check_avg_chapter_character):
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

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



def check_avg_choice_is_per_difficulty():
    """Three difficulties, three options: one option per run, locked out thereafter.

    Stages 1101/1201/1301 are the SAME scene ("Third Faction", 1-1) at _difficulty 1/2/3
    and share their AVG ids, so a choice is permanent PER DIFFICULTY and a full clear
    uses up all three options.

    `UpdateAVGOptionLockState` reads the two tags together:

        v13 = _replayMode ? 0 : tag[0]
        if (v13 <= 0):  lock all, then UNLOCK btnOptions[d-1] for each digit d of tag[1]
        else:
            v17 = tag[1] / 10^(v13-1) % 10
            if v17 >= 1: lock all, UNLOCK only btnOptions[v17-1]
            else:        LOCK btnOptions[d-1] for each digit d of tag[1]

    so tag[0] is the option taken ON THIS DIFFICULTY (1-based, 0 = undecided) and tag[1]
    is the identity mask 4321 with the digits of options used on OTHER difficulties
    zeroed. A flat 4321 made the "already chosen" test true for every tag[0], which is
    why Hard showed the same single option as Easy instead of the two untried ones.
    """
    AVG = 10103

    def client(tag0, tag1, buttons=3):
        """Transcribe UpdateAVGOptionLockState -> (selectable buttons, pays?).

        `_skipPerform` is what decides the payout: OnBtnClick dispatches
        AvgUIOptionsEvent 3 (OnAvgOptionsSkipover -- no request, no reward) when it is
        set, and 1 (OnAvgOptionsSelected -> RequestServerAvgSelectOption) when clear.
        """
        if tag0 <= 0:
            unlocked = {tag1 // 10 ** i % 10 - 1
                        for i in range(buttons) if tag1 // 10 ** i % 10 >= 1}
            return unlocked, False              # _skipPerform = 1
        v17 = tag1 // 10 ** (tag0 - 1) % 10
        if v17 >= 1:
            return {v17 - 1}, False             # _skipPerform = 1
        locked = {tag1 // 10 ** i % 10 - 1
                  for i in range(buttons) if tag1 // 10 ** i % 10 >= 1}
        return set(range(buttons)) - locked, True   # _skipPerform = 0 -> pays

    def client_unlocked(tag0, tag1, buttons=3):
        return client(tag0, tag1, buttons)[0]

    def client_pays(tag0, tag1, buttons=3):
        return client(tag0, tag1, buttons)[1]

    st = ps.load(1000040)
    check("undecided on Easy offers everything",
          client_unlocked(*ps.avg_sync_tags(st, AVG, 1)) == {0, 1, 2},
          str(ps.avg_sync_tags(st, AVG, 1)))
    check("  ...and PAYS -- a first decision must reward",
          client_pays(*ps.avg_sync_tags(st, AVG, 1)),
          str(ps.avg_sync_tags(st, AVG, 1)))

    ps.set_avg_choice(st, AVG, 0, 1)                       # Easy -> option 1
    check("  ...and after choosing, only that option",
          client_unlocked(*ps.avg_sync_tags(st, AVG, 1)) == {0},
          str(ps.avg_sync_tags(st, AVG, 1)))
    check("  ...and a replay does NOT pay again",
          not client_pays(*ps.avg_sync_tags(st, AVG, 1)),
          str(ps.avg_sync_tags(st, AVG, 1)))
    check("HARD offers the two UNTRIED options",
          client_unlocked(*ps.avg_sync_tags(st, AVG, 2)) == {1, 2},
          str(ps.avg_sync_tags(st, AVG, 2)))
    check("  ...and PAYS, because it is a fresh decision",
          client_pays(*ps.avg_sync_tags(st, AVG, 2)),
          str(ps.avg_sync_tags(st, AVG, 2)))

    ps.set_avg_choice(st, AVG, 1, 2)                       # Hard -> option 2
    check("  ...and after choosing on Hard, only that one",
          client_unlocked(*ps.avg_sync_tags(st, AVG, 2)) == {1},
          str(ps.avg_sync_tags(st, AVG, 2)))
    check("NIGHTMARE is down to the last option",
          client_unlocked(*ps.avg_sync_tags(st, AVG, 3)) == {2},
          str(ps.avg_sync_tags(st, AVG, 3)))
    check("  ...and still pays for it",
          client_pays(*ps.avg_sync_tags(st, AVG, 3)),
          str(ps.avg_sync_tags(st, AVG, 3)))

    ps.set_avg_choice(st, AVG, 2, 3)
    check("Easy still shows its own pick afterwards",
          client_unlocked(*ps.avg_sync_tags(st, AVG, 1)) == {0},
          str(ps.avg_sync_tags(st, AVG, 1)))

    # A scene pays once PER DIFFICULTY -- three difficulties, three payouts.
    check("re-deciding the same difficulty is refused",
          ps.set_avg_choice(st, AVG, 1, 1) is False)
    check("  ...and leaves that difficulty's pick alone",
          ps.avg_choice(st, AVG, 1) == 0, str(ps.avg_choice(st, AVG, 1)))

    # tag[0] must never be 0. Zero takes the `v13 <= 0` branch, which sets
    # _skipPerform = 1 -> event 3 -> OnAvgOptionsSkipover -> no request and no reward.
    # For an undecided scene it points at a FREE digit slot instead, so v17 == 0 keeps
    # _skipPerform clear and the decision pays.
    st2 = ps.load(1000041)
    t0, t1 = ps.avg_sync_tags(st2, AVG, 1)
    check("an undecided scene never reports tag0 0", t0 > 0, str((t0, t1)))
    check("  ...and points at a digit slot that is empty",
          t1 // 10 ** (t0 - 1) % 10 == 0, str((t0, t1)))
    ps.set_avg_choice(st2, AVG, 0, 1)
    check("  ...while a decided one is 1-BASED (option 0 -> 1)",
          ps.avg_sync_tags(st2, AVG, 1)[0] == 1,
          str(ps.avg_sync_tags(st2, AVG, 1)))

    # Saves written before per-difficulty keying stored a bare int.
    st3 = ps.load(1000042)
    st3["avg_choices"] = {str(AVG): 2}
    check("a legacy flat choice migrates to difficulty 1",
          ps.avg_choice(st3, AVG, 1) == 2, str(ps.avg_choice(st3, AVG, 1)))
    check("  ...and still locks that option out elsewhere",
          client_unlocked(*ps.avg_sync_tags(st3, AVG, 2)) == {0, 1},
          str(ps.avg_sync_tags(st3, AVG, 2)))

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



def check_avg_chapter_resolution():
    """avg ids are allocated CONTINUOUSLY, not restarted per chapter.

    Chapter 2 runs 20101..21001 and chapter 3 carries straight on at 21101, so the old
    `avg_id // 10000` fallback read "2" for both. It agreed with the stage on only 383 of
    1,680 scenes -- right for chapters 1 and 2, which is exactly the range a spot-check
    would have covered, and wrong for everything after.
    """
    from player_state import karma
    import design_data as dd

    stages = dd.rows("stage") or {}
    idx = karma._avg_stage_index()
    wrong = [(a, s) for a, s in idx.items()
             if int((stages.get(s) or {}).get("_difficulty") or 0) == 1
             and karma.avg_chapter(a) != int(s) // 1000]
    check("every indexed scene resolves to its stage's chapter",
          not wrong, f"{len(wrong)} wrong, e.g. {wrong[:3]}")

    # The exact boundary: 21001 is the last chapter-2 scene, 21101 the first of ch3.
    check("21001 (stage 2-10) is chapter 2", karma.avg_chapter(21001) == 2,
          str(karma.avg_chapter(21001)))
    check("21101 (stage 3-1) is chapter 3, not 2", karma.avg_chapter(21101) == 3,
          str(karma.avg_chapter(21101)))
    check("a mid-chain id interpolates to its neighbour's chapter",
          karma.avg_chapter(10102) == 1, str(karma.avg_chapter(10102)))

    # Observed in play: the chapter-3 decision at 3-6 pays Matina. Under the old rule
    # this id read as chapter 2 and would have paid Caillen, so it is the regression
    # case worth pinning.
    check("21601 (stage 3-6) is chapter 3", karma.avg_chapter(21601) == 3,
          str(karma.avg_chapter(21601)))
    check("...and pays Matina, not Caillen",
          karma.karma_char_for(21601) == karma.KARMA_CHAPTER_CHAR[3],
          str(karma.karma_char_for(21601)))



def check_decision_grades():
    """Every decision pays one of each banner grade across its three options.

    The reward tracks how CRUEL the choice is -- Lucifer is a demon king, so the meanest
    option pays most and the kindest least -- and each scene has exactly three options,
    so the three grades map one-to-one. Which option deserves which is not yet known, so
    they are dealt arbitrarily; what must hold regardless is that a scene never pays one
    grade twice, including the scenes where one option is a pinned retail observation.
    """
    from player_state import karma

    scenes = sorted({avg for avg, _ in karma.KARMA_DECISION_TIER})
    check("the decision table covers every scene", len(scenes) == 96, str(len(scenes)))

    bad = []
    for avg in scenes:
        karmas = sorted(karma.karma_reward(avg, i)[3] for i in range(3))
        if karmas != [10, 20, 100]:
            bad.append((avg, karmas))
    check("every scene pays one Up!, one Big Up! and one Ultimate Up!",
          not bad, f"{len(bad)} wrong, e.g. {bad[:3]}")

    # The grades straddle the client's own banner thresholds: OptionButton.SetReward
    # picks "Up!" under 20, "Big Up!" from 20, "Ultimate Up!" from 100.
    grades = sorted(f for _, _, f in karma.KARMA_TIERS.values())
    check("the three grades land in the three banner bands",
          grades[0] < 20 <= grades[1] < 100 <= grades[2], str(grades))

    # A payout is recorded once and must survive reloads and replays.
    a = [karma.karma_reward(21601, i) for i in range(3)]
    b = [karma.karma_reward(21601, i) for i in range(3)]
    check("a decision pays the same on every call", a == b)

    # Default amounts are the retail ones read off the tutorial footage: 5 / 10 / 15.
    check("an observed option pays its retail amount",
          karma.karma_reward(10107, 1)[1] == 15, str(karma.karma_reward(10107, 1)))

    # KARMA_TIERS is the balance knob, and it must move EVERY decision -- including the
    # three with retail observations. Those observations name the grade, not the payout;
    # if they set the amount too, chapter 1 would stay unscaled while the rest moved.
    old = dict(karma.KARMA_TIERS)
    try:
        karma.KARMA_TIERS = {1: (karma.CUR_CASH, 25, 10),
                             2: (karma.CUR_CASH, 50, 20),
                             3: (karma.CUR_CASH, 100, 100)}
        check("the knob moves a dealt grade",
              karma.karma_reward(21601, 0)[1] == 100, str(karma.karma_reward(21601, 0)))
        check("...and an observed one too",
              karma.karma_reward(10107, 1)[1] == 100, str(karma.karma_reward(10107, 1)))
        # The banner bands are client-side at 20 and 100, so karma must not move.
        check("karma still lands one per banner band",
              sorted(karma.karma_reward(21601, i)[3] for i in range(3)) == [10, 20, 100])
    finally:
        karma.KARMA_TIERS = old
    check("the knob defaults to retail amounts", karma.karma_reward(21601, 0)[1] == 15)


def check_split_stacks_spend_whole():
    """A split stack must spend across slots -- the single-slot bug gave items free."""
    st = ps.load(1000031)
    bag = st["backpack"].setdefault(str(ps.BP_STORAGE_NORMAL), {})
    bag.clear()
    # the split only ever comes from an outside writer, so build it by hand
    bag["7"] = {"iid": 424242, "amount": 100}
    bag["9"] = {"iid": 424242, "amount": 50}
    check("has_item sees the split total", ps.has_item(st, 424242, 150))
    check("spend refuses more than the total", not ps.spend_item(st, 424242, 151))
    check("  ...and deducts nothing on refusal",
          ps.item_count(st, 424242) == 150, str(ps.item_count(st, 424242)))
    check("spend drains across the slots", ps.spend_item(st, 424242, 120))
    check("  ...leaving the remainder", ps.item_count(st, 424242) == 30,
          str(ps.item_count(st, 424242)))
    check("  ...and empties drained slots", "7" not in bag, str(sorted(bag)))


def main():
    for fn in (check_split_stacks_spend_whole,
               check_table_matches_the_screenshot, check_a_single_rank_up_pays_once,
               check_multi_rank_pays_every_rank_crossed, check_no_rank_up_pays_nothing,
               check_the_payout_is_pushed, check_gifts_report_the_bonus,
               check_the_rank_up_splash_is_triggered,
               check_avg_choice_is_per_difficulty,
               check_avg_chapter_character, check_avg_chapter_resolution,
               check_decision_grades):
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

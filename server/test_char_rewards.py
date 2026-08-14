#!/usr/bin/env python3
"""Quest rewards that are CHARACTERS must grant the cast, not bag an unopenable item.

Quest 31015 pays item 930 "★4 Caillen of Braveheart" -- `_action 2`, a box whose
`_param1` resolves to nothing in the pack. Granting it as a bag item is doubly wrong:
`GetItemSpace` has no case for `_action 2`, so the client files it in no inventory tab
and it is INVISIBLE, and the character never arrives. The real payload is the cast.

    python3 test_char_rewards.py

Uses its own SEVENSINS_ACCOUNTS tempdir so it never touches real save data.
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

_TMP = tempfile.mkdtemp(prefix="sevensins-charreward-test-")
os.environ["SEVENSINS_ACCOUNTS"] = _TMP        # before player_state imports

import battle as bt           # noqa: E402
import player_state as ps     # noqa: E402
from player_state.core import _default, _seed_roster   # noqa: E402

_fail = 0

CAILLEN = 10981
BOX_ITEM = 930            # _action 2, the design row's stand-in
CHAR_ITEM = 110984        # _action 1, _param1 = 10981, ★4 -- what the footage shows
QUEST = 31015


def check(name, cond, detail=""):
    global _fail
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        _fail += 1


def fresh():
    st = _default(1000001)
    _seed_roster(st)
    return st


def main():
    # ---- the resolver -----------------------------------------------------
    # The box and the character item share an IDENTICAL Chinese name; that is the
    # link. The EN names differ ("Caillen of Braveheart" vs "Braveheart Caillen") and
    # would not join up.
    check("box resolves to the cast", ps.char_reward_of(BOX_ITEM) == (CAILLEN, 4,
                                                                      CHAR_ITEM),
          str(ps.char_reward_of(BOX_ITEM)))
    check("the character item resolves to itself",
          ps.char_reward_of(CHAR_ITEM) == (CAILLEN, 4, CHAR_ITEM),
          str(ps.char_reward_of(CHAR_ITEM)))
    # 110984/5/6 are the same cast at ★4/★5/★6 -- the STAR comes from the item, not
    # from the char row's own rarity (which is 3).
    check("star comes from the item, not the char row",
          ps.char_reward_of(110986) == (CAILLEN, 6, 110986)
          and (bt.dd.row("char", CAILLEN) or {}).get("_rarity") == 3,
          str(ps.char_reward_of(110986)))

    for iid in (202, 545, 1, 102):
        check(f"ordinary item {iid} is not a cast", ps.char_reward_of(iid) is None,
              str(ps.char_reward_of(iid)))
    # Real boxes (bundles, luckybags, starshard sets) must stay ordinary items.
    check("a bundle box is not a cast", ps.char_reward_of(925) is None,
          str(ps.char_reward_of(925)))

    # ---- claiming ---------------------------------------------------------
    st = fresh()
    before = len(st["roster"])
    bag_before = len(st["backpack"].get("1", {}))
    rewards, new_chars = ps.complete_quests(st, [QUEST])

    check("one cast granted", len(new_chars) == 1, str(new_chars))
    check("roster grew by one", len(st["roster"]) == before + 1,
          f"{before} -> {len(st['roster'])}")
    entry = st["roster"][new_chars[0]]
    check("granted the right cast", entry["id"] == CAILLEN, str(entry))
    check("granted at the item's star", entry["star"] == 4, str(entry))
    check("nothing was added to the bag",
          len(st["backpack"].get("1", {})) == bag_before,
          f"{bag_before} -> {len(st['backpack'].get('1', {}))}")
    check("popup reports the CHARACTER item, not the box",
          rewards == [(QUEST, CHAR_ITEM, 1)], str(rewards))

    # ---- the create payload ----------------------------------------------
    # receivedCreateChar deserialises strargs[0] as Dictionary<uid, CharData>.
    blob = json.loads(ps.char_create_json(st, new_chars))
    check("create payload is keyed by uid", list(blob) == new_chars, str(list(blob)))
    cd = blob[new_chars[0]]
    check("carries a dbdata CharData", isinstance(cd.get("dbdata"), dict), str(cd)[:80])
    check("dbdata names the cast and star",
          cd["dbdata"].get("id") == CAILLEN and cd["dbdata"].get("star") == 4,
          str(cd["dbdata"])[:120])
    check("unknown uids are skipped, not nulled",
          json.loads(ps.char_create_json(st, ["nope"])) == {})


    # ---- a quest that pays a BUNDLE ---------------------------------------
    # Quest 31019 (Lucifer's Note step 19) pays item 1200006 "Evolution TUT Bundle",
    # `_action 2`. Same double wall as the storefront bundles: GetItemSpace has no case
    # for `_action 2` so no inventory tab holds it, and EnqueItemPopupInfo strips it
    # from the reward popup -- claiming the goal handed over something invisible and
    # said nothing. Contents come from the row's CHINESE `_note1`, and live footage of
    # the claim shows exactly the two cards this asserts.
    st3 = fresh()
    st3["sp_quests"]["31018"] = {"id": 31018, "a_time": 0, "cnt": 1, "status": 1}
    roster_before = len(st3["roster"])
    gems_before = ps.item_count(st3, 556)
    rewards3, new3 = ps.complete_quests(st3, [31019])

    check("the bundle is never bagged whole",
          not any(e.get("iid") == 1200006
                  for e in st3["backpack"].get("1", {}).values()))
    check("its cast line reaches the roster", len(new3) == 1, str(new3))
    check("and it is Jacqueline at ★4",
          st3["roster"][new3[0]]["id"] == 11001
          and st3["roster"][new3[0]]["star"] == 4, str(st3["roster"][new3[0]]))
    check("its item line reaches the bag",
          ps.item_count(st3, 556) == gems_before + 1200,
          f"{gems_before} -> {ps.item_count(st3, 556)}")
    check("the roster grew by exactly one",
          len(st3["roster"]) == roster_before + 1)
    # Reply 513 carries one (item, count); it must name a row the popup mask keeps.
    check("the headline is the `_action 1` cast item",
          rewards3 == [(31019, 111004, 1)], str(rewards3))
    check("which survives EnqueItemPopupInfo's mask",
          (bt.dd.row("item", rewards3[0][1]) or {}).get("_action") == 1)
    # Both lines are needed for the side-by-side popup the live game shows.
    lines = ps.goods_payout_lines(st3, 1200006)
    check("both bundle lines are reported for the popup",
          lines == [(111004, 1), (556, 1200)], str(lines))

    # ---- ordinary rewards are untouched -----------------------------------
    st2 = fresh()
    rewards2, new2 = ps.complete_quests(st2, [31013])      # pays an Awaker Scroll
    check("an item quest grants no cast", new2 == [], str(new2))
    check("an item quest still reports its item",
          rewards2 and rewards2[0][1] == 202, str(rewards2))
    check("and it reached the bag",
          any(e.get("iid") == 202 for e in st2["backpack"].get("1", {}).values()))

    print("\n" + ("ALL PASSED" if not _fail else f"{_fail} FAILED"))
    return 1 if _fail else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        import shutil
        shutil.rmtree(_TMP, ignore_errors=True)

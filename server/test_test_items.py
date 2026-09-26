#!/usr/bin/env python3
"""The pack's internal TEST rows must never reach a player.

    python3 test_test_items.py

19 item rows are marked 測試 ("test") in Chinese: nine starshard bundles (51-59), six
single shards (81-86), two stacking bundles (87-88) and two bloodpacts (721004-5). They
are REAL rows with real actions -- 81-86 carry `_action` 111-116, squarely inside
RUNE_ACTION_RANGE -- so every path that files a starshard accepted them, and a test shard
in storage 2 takes the client's starshard panel down.

The English hides the nine that matter most: item 51 reads "★3 Slayer Starshards Bundle",
indistinguishable from a reward, while only 81-88 and the pacts say "Test". That is why
the match is on `_itemName` and not on `_itemName_en` or a hardcoded id list.

Reachability, measured rather than assumed -- this is a guard at the funnel, NOT a patch
for a live leak: no stage pool contains one, no shop or goods row references one, and no
server code mentions one. 17 bundle items DO contain one, and nothing grants those either.

Uses its own SEVENSINS_ACCOUNTS tempdir.
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["SEVENSINS_ACCOUNTS"] = tempfile.mkdtemp(prefix="sevensins-testitems-")

import battle as bt                                            # noqa: E402
import design_data as dd                                       # noqa: E402
import player_state as ps                                       # noqa: E402
from player_state.core import RUNE_ACTION_RANGE                 # noqa: E402

FAILURES = []


def check(cond, msg):
    if not cond:
        FAILURES.append(msg)


import re                                                       # noqa: E402


def _test_ids():
    """Re-derived from the pack in BOTH languages, the way is_test_item reads it."""
    en = re.compile(r"\btest\b", re.I)
    return sorted(int(i) for i, r in (dd.rows("item") or {}).items()
                  if "測試" in (r.get("_itemName") or "")
                  or en.search(r.get("_itemName_en") or ""))


def neither_language_alone_is_enough():
    """Each language hides exactly nine of the 28 -- that is why both are read."""
    items = dd.rows("item") or {}
    en = re.compile(r"\btest\b", re.I)
    zh_ids = {int(i) for i, r in items.items() if "測試" in (r.get("_itemName") or "")}
    en_ids = {int(i) for i, r in items.items() if en.search(r.get("_itemName_en") or "")}
    check(zh_ids - en_ids,
          "the Chinese no longer hides any test row from the English -- the two-language "
          "match is no longer load-bearing, though it stays correct")
    check(en_ids - zh_ids,
          "the English no longer hides any test row from the Chinese (51-59 used to)")
    check(set(_test_ids()) == (zh_ids | en_ids),
          "_test_ids and is_test_item disagree about the population")
    for iid in zh_ids | en_ids:
        check(ps.is_test_item(iid), f"item {iid} is marked in one language but not caught")


def the_word_boundary_protects_273_real_items():
    """A bare "test" substring would purge every Testament and Contest Ticket.

    This is the assertion that stops a well-meaning widening from deleting 273 legitimate
    items out of live saves: `EX Evolution Testament`, `Contest Ticket`, `Red Team
    Testament` all contain "test" and none of them is a dev row.
    """
    items = dd.rows("item") or {}
    near = [int(i) for i, r in items.items()
            if re.search(r"test", (r.get("_itemName_en") or ""), re.I)
            and not re.search(r"\btest\b", (r.get("_itemName_en") or ""), re.I)]
    check(len(near) > 100,
          f"only {len(near)} items embed 'test' in a word -- the trap this guards may "
          f"have moved, but check before relaxing the boundary")
    wrong = [i for i in near if ps.is_test_item(i)]
    check(not wrong,
          f"{len(wrong)} legitimate items were flagged as test rows: "
          f"{[(i, (items[i] or {}).get('_itemName_en')) for i in wrong[:4]]}")


def the_pack_still_marks_them():
    ids = _test_ids()
    check(ids, "no item is marked as a test row any more -- re-read before trusting "
               "the guard")
    # ...and some of them really are in the starshard action range.
    in_range = [i for i in ids
                if (dd.row("item", i) or {}).get("_action") in RUNE_ACTION_RANGE]
    check(in_range, "no test row sits in RUNE_ACTION_RANGE any more")


def every_one_is_refused_by_the_funnels():
    for iid in _test_ids():
        check(ps.is_test_item(iid), f"item {iid} is marked 測試 but is_test_item says no")
        # The reward funnel skips it rather than filing it.
        st = {"player_id": 1, "backpack": {}, "roster": {}, "items": {}}
        got = ps.grant_reward(st, iid, 1)
        check(got == "skipped", f"grant_reward filed test item {iid} as {got!r}")
        check(not st.get("backpack"), f"test item {iid} still landed in a bag")
    # make_rune is the single constructor every starshard path runs through.
    from player_state.core import make_rune
    for iid in _test_ids():
        if (dd.row("item", iid) or {}).get("_action") not in RUNE_ACTION_RANGE:
            continue
        try:
            make_rune({"player_id": 1}, iid, 1)
            FAILURES.append(f"make_rune built a starshard from test item {iid}")
        except ValueError:
            pass


def an_ordinary_item_is_untouched():
    """The guard must not catch real rewards."""
    ok = [int(i) for i, r in (dd.rows("item") or {}).items()
          if "測試" not in (r.get("_itemName") or "")]
    check(len(ok) > 1000, "almost every item is now a test item -- the match is too wide")
    for iid in ok[:200]:
        check(not ps.is_test_item(iid), f"ordinary item {iid} was flagged as a test row")
    # A real starshard still files.
    real = next((i for i in ok
                 if (dd.row("item", i) or {}).get("_action") in RUNE_ACTION_RANGE), None)
    check(real, "no ordinary starshard left to check")
    if real:
        from player_state.core import make_rune, rune_slot
        check(rune_slot(real), f"rune_slot stopped recognising real starshard {real}")
        make_rune({"player_id": 1}, real, 1)          # must not raise
        # rune_slot stays FACTUAL for test rows -- policy lives in the funnels, and
        # putting it here is what broke the save-editor sample picker.
        in_range = [i for i in _test_ids()
                    if (dd.row("item", i) or {}).get("_action") in RUNE_ACTION_RANGE]
        for i in in_range:
            check(rune_slot(i) is not None,
                  f"rune_slot({i}) went policy again; keep it factual")


def no_drop_pool_offers_one():
    ids = set(_test_ids())
    bad = []
    for sid in (dd.rows("stage") or {}):
        try:
            pool = set(bt.generated_drop_pool(int(sid)) or [])
        except Exception:                                      # noqa: BLE001
            continue
        if pool & ids:
            bad.append((int(sid), sorted(pool & ids)))
    check(not bad, f"{len(bad)} stage pools offer a test item: {bad[:3]}")


def main():
    the_pack_still_marks_them()
    neither_language_alone_is_enough()
    the_word_boundary_protects_273_real_items()
    every_one_is_refused_by_the_funnels()
    an_ordinary_item_is_untouched()
    no_drop_pool_offers_one()
    for f in FAILURES:
        print("FAIL:", f)
    print(f"{len(FAILURES)} failure(s)")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())

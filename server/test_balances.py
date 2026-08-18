#!/usr/bin/env python3
"""Starting balances, the stamina curve, and that rewards visibly move them.

Background (2026-08-18): "gems and stamina rewards do not increase either value ...
despite the reward popup showing". The grants always landed -- the account files showed
diamonds climbing past the 999,999 seed and stamina past its 999 cap -- but the seed was
so high that nothing a reward gave could be seen. The plumbing was never the bug.

**The stamina cap is the CLIENT's number.** `PanelPlayerLevelUpResult.SetNewLevelInfo`
(0x1592838) computes `5 * newLevel + 10` inline and prints it on the rank-up popup, and
`Energy` has no SetEnergyCap -- the bar's cap can only come from our `energy_cap`. If
the two ever disagree the popup announces one number while the bar shows another, which
is the whole defect class this came from. So this file pins the server to the client.

    python3 test_balances.py
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

_TMP = tempfile.mkdtemp(prefix="sevensins-balance-test-")
os.environ["SEVENSINS_ACCOUNTS"] = _TMP

import player_state as ps                               # noqa: E402
from player_state.core import _default, _seed_roster    # noqa: E402

_fail = 0


def check(name, cond, detail=""):
    global _fail
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        _fail += 1


def client_cap(lv):
    """The client's own formula, transcribed from the level-up popup."""
    return 5 * lv + 10


def check_cap_matches_the_client():
    for lv in (1, 2, 3, 10, 15, 28, 76, 100, 200):
        check(f"cap at lv {lv} is the client's {client_cap(lv)}",
              ps.stamina_cap_for_level(lv) == client_cap(lv),
              str(ps.stamina_cap_for_level(lv)))
    # Level 1 has to be playable: stage 1-1 costs 5 AP.
    import battle as bt
    ap = int((bt.dd.row("stage", 1101) or {}).get("_ap") or 0)
    check("a level-1 account can afford at least three runs of 1-1",
          ps.stamina_cap_for_level(1) >= ap * 3, f"{ps.stamina_cap_for_level(1)} vs {ap}x3")


def check_new_account():
    st = _default(1000002)
    check("a new account no longer starts at 999,999 diamonds",
          st["currency"]["1"] == ps.STARTER_DIAMONDS, str(st["currency"]["1"]))
    check("  ...and it is a number a reward can visibly move",
          st["currency"]["1"] <= 10000, str(st["currency"]["1"]))
    en = st["energy"]["1"]
    check("stamina starts at the level-1 cap, not 999",
          en["cap"] == ps.stamina_cap_for_level(1) and en["energy"] == en["cap"], str(en))


def check_rewards_now_move_the_numbers():
    """The actual thing that was reported."""
    st = ps.load(1000003)
    st["energy"]["1"] = {"energy": 3, "cap": ps.stamina_cap_for_level(1)}
    gems_before = int(st["currency"]["1"])
    ps.grant_reward(st, 1, 150)          # Diamond x150
    gained = int(st["currency"]["1"]) - gems_before
    check("a 150-gem reward adds exactly 150", gained == 150, str(gained))
    check("  ...and that is a visible fraction of the balance",
          gained / max(gems_before, 1) > 0.01,
          f"{gained} on {gems_before}")

    stam_before = int(st["energy"]["1"]["energy"])
    ps.grant_reward(st, 5, 20)           # Stamina x20
    check("a 20-stamina reward adds exactly 20",
          int(st["energy"]["1"]["energy"]) - stam_before == 20,
          str(st["energy"]["1"]))
    check("  ...and the bar was not already full, so it shows",
          stam_before < st["energy"]["1"]["cap"], str(stam_before))


def check_rank_up_raises_and_fills():
    st = ps.load(1000004)
    st["level"]["lv"] = 1
    st["energy"]["1"] = {"energy": 2, "cap": ps.stamina_cap_for_level(1)}
    levelled, old, new = ps.grant_player_xp(st, 10 ** 5)
    check("ranking up happens", levelled and new > old, f"{old}->{new}")
    check("  ...raises the cap to the client's number",
          st["energy"]["1"]["cap"] == client_cap(new), str(st["energy"]["1"]))
    check("  ...and fills the bar, so the popup is not an empty promise",
          st["energy"]["1"]["energy"] == st["energy"]["1"]["cap"], str(st["energy"]["1"]))
    # A bar already over cap (from stamina items) must not be clawed back.
    st["energy"]["1"]["energy"] = 9999
    ps.grant_player_xp(st, 10 ** 5)
    check("an over-cap bar survives a rank-up",
          st["energy"]["1"]["energy"] == 9999, str(st["energy"]["1"]))


def check_migration_is_one_shot_and_safe():
    st = ps.load(1000005)
    st["currency"]["1"] = 999999
    st["energy"]["1"] = {"energy": 1299, "cap": 999}
    st["level"]["lv"] = 15
    st.pop("balance_rev", None)
    ps.save(st)

    note = ps.migrate_starting_balances(st)
    check("the migration reports what it changed", bool(note), repr(note))
    check("  ...lowering the diamond seed", st["currency"]["1"] == ps.STARTER_DIAMONDS,
          str(st["currency"]["1"]))
    check("  ...and refitting stamina to the level's cap",
          st["energy"]["1"] == {"energy": client_cap(15), "cap": client_cap(15)},
          str(st["energy"]["1"]))
    check("  ...after copying the account file",
          os.path.isfile(ps.path_for(1000005) + ".bak-prebalance"))
    check("running it again is a no-op", ps.migrate_starting_balances(st) == "")

    # It must never RAISE a balance -- an account below the seed keeps what it earned.
    st2 = ps.load(1000006)
    st2["currency"]["1"] = 12
    st2.pop("balance_rev", None)
    ps.migrate_starting_balances(st2)
    check("an account below the seed is not topped up", st2["currency"]["1"] == 12,
          str(st2["currency"]["1"]))

    # The flag must NOT live in _default: load() setdefaults defaults into old saves,
    # which would backfill it and stop the migration ever running.
    check("the revision flag is not seeded into _default",
          "balance_rev" not in _default(1000007))


def main():
    for fn in (check_cap_matches_the_client, check_new_account,
               check_rewards_now_move_the_numbers, check_rank_up_raises_and_fills,
               check_migration_is_one_shot_and_safe):
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

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
    """MAX Stamina, from the client's "+2 MAX Stamina" ladder."""
    return min(148 + 2 * lv, 300)


def client_recovered(lv):
    """The "Stamina Recovered" figure -- `_lbAP` = 5 * newLevel + 10."""
    return 5 * lv + 10


def check_the_two_numbers_are_not_the_same_one():
    """The rank-up screen has TWO stamina widgets and they are different figures.

    A Rank 2 screenshot names them: "20 Stamina Recovered" and "+2 MAX Stamina". An
    earlier pass read `5*lv+10` (the recovery) as the cap and got 15 stamina at rank 1.
    """
    check("rank 2 recovers 20, matching the screenshot",
          ps.stamina_recovered_on_rank_up(2) == 20,
          str(ps.stamina_recovered_on_rank_up(2)))
    check("MAX Stamina goes up by exactly +2 a rank",
          all(client_cap(lv + 1) - client_cap(lv) == 2 for lv in range(1, 76)))
    check("  ...and the two are NOT the same number",
          ps.stamina_recovered_on_rank_up(2) != ps.stamina_cap_for_level(2),
          f"{ps.stamina_recovered_on_rank_up(2)} vs {ps.stamina_cap_for_level(2)}")


def check_cap_matches_the_client():
    for lv in (1, 2, 3, 10, 15, 28, 76, 77, 100, 200):
        check(f"MAX Stamina at lv {lv} is {client_cap(lv)}",
              ps.stamina_cap_for_level(lv) == client_cap(lv),
              str(ps.stamina_cap_for_level(lv)))
    check("rank 1 is 150, as reported from the live game",
          ps.stamina_cap_for_level(1) == 150, str(ps.stamina_cap_for_level(1)))
    # The badge hides at 2*lv >= 153, which IS the cap reaching its 300 ceiling.
    check("the cap stops growing exactly where the client hides the +2 badge",
          ps.stamina_cap_for_level(76) == 300 and ps.stamina_cap_for_level(77) == 300
          and (2 * 76 < 153) and not (2 * 77 < 153),
          f"{ps.stamina_cap_for_level(76)}/{ps.stamina_cap_for_level(77)}")
    # Level 1 has to be playable: stage 1-1 costs 5 AP.
    import battle as bt
    ap = int((bt.dd.row("stage", 1101) or {}).get("_ap") or 0)
    check("a level-1 account can afford a real session of 1-1",
          ps.stamina_cap_for_level(1) >= ap * 10, f"{ps.stamina_cap_for_level(1)} vs {ap}x10")


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


def check_rank_up_pays_what_the_screen_says():
    st = ps.load(1000004)
    st["level"]["lv"] = 1
    st["energy"]["1"] = {"energy": 5, "cap": ps.stamina_cap_for_level(1)}
    levelled, old, new = ps.grant_player_xp(st, 100)
    check("ranking up happens", levelled and new == 2, f"{old}->{new}")
    check("  ...raises MAX Stamina to +2", st["energy"]["1"]["cap"] == client_cap(2),
          str(st["energy"]["1"]))
    check("  ...and hands over the Stamina Recovered figure, not a refill",
          st["energy"]["1"]["energy"] == 5 + client_recovered(2),
          str(st["energy"]["1"]))

    # Two ranks in one go must pay both, which is what the screen does back to back.
    st2 = ps.load(1000008)
    st2["level"]["lv"] = 1
    st2["energy"]["1"] = {"energy": 0, "cap": ps.stamina_cap_for_level(1)}
    _l, o, n = ps.grant_player_xp(st2, 10 ** 4)
    want = sum(client_recovered(x) for x in range(o + 1, n + 1))
    check(f"gaining {n - o} ranks at once pays every one of them",
          st2["energy"]["1"]["energy"] == want,
          f"{st2['energy']['1']['energy']} vs {want}")

    # A bar over cap (stamina items stack past MAX) must never be clawed back.
    st["energy"]["1"]["energy"] = 9999
    before = st["energy"]["1"]["energy"]
    ps.grant_player_xp(st, 10 ** 5)
    check("an over-cap bar is never lowered by a rank-up",
          st["energy"]["1"]["energy"] >= before, str(st["energy"]["1"]))


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



def check_rev1_clamp_is_repaired():
    """Accounts clamped to 3000 must not be stranded there when the starter goes back up.

    STARTER_DIAMONDS was briefly 3000 on 2026-08-18. The migration is gated on
    `balance_rev`, so simply restoring the constant to 10000 would leave every account
    that logged in during that window sitting at 3000 FOREVER -- the migration never
    runs again. Revision 2 exists to repair exactly that.

    It has to be narrow in both directions:
      * only an account carrying rev 1 AND exactly the value the clamp wrote;
      * and the downward clamp must NOT re-run on an already-migrated account, because
        by rev 2 a balance above the starter is diamonds the player EARNED. Confiscating
        those would be a worse bug than the one being fixed.
    """
    from player_state.core import (_default, _seed_roster, migrate_starting_balances,
                                   STARTER_DIAMONDS, STARTER_DIAMONDS_REV1,
                                   BALANCE_REVISION)

    def acct(rev, diamonds):
        st = _default(1); _seed_roster(st)
        st["balance_rev"] = rev
        st["currency"]["1"] = diamonds
        return st

    check("the repair revision is ahead of the one that clamped",
          BALANCE_REVISION > 1, str(BALANCE_REVISION))
    check("  ...and the starter is back above the clamped value",
          STARTER_DIAMONDS > STARTER_DIAMONDS_REV1,
          f"{STARTER_DIAMONDS} vs {STARTER_DIAMONDS_REV1}")

    owed = STARTER_DIAMONDS - STARTER_DIAMONDS_REV1

    st = acct(1, STARTER_DIAMONDS_REV1)
    migrate_starting_balances(st)
    check("an account clamped by rev 1 is made whole",
          int(st["currency"]["1"]) == STARTER_DIAMONDS, str(st["currency"]["1"]))

    # **Credit the difference, not the target.** The first real account checked was on
    # 3100, not 3000 -- it had earned 100 since. Matching the exact clamp value would
    # have silently skipped it.
    st = acct(1, STARTER_DIAMONDS_REV1 + 100)
    migrate_starting_balances(st)
    check("  ...even after earning since the clamp",
          int(st["currency"]["1"]) == STARTER_DIAMONDS_REV1 + 100 + owed,
          str(st["currency"]["1"]))

    st = acct(1, 500)
    migrate_starting_balances(st)
    check("  ...and after SPENDING since the clamp, refunded what it cost them",
          int(st["currency"]["1"]) == 500 + owed, str(st["currency"]["1"]))

    st = acct(1, STARTER_DIAMONDS + 2000)
    migrate_starting_balances(st)
    check("  ...and earned diamonds are NOT confiscated",
          int(st["currency"]["1"]) == STARTER_DIAMONDS + 2000 + owed,
          str(st["currency"]["1"]))

    st = acct(0, 999999)
    migrate_starting_balances(st)
    check("a never-migrated seed account is still clamped down",
          int(st["currency"]["1"]) == STARTER_DIAMONDS, str(st["currency"]["1"]))

    # Idempotent: running twice must not move anything.
    st = acct(1, STARTER_DIAMONDS_REV1)
    migrate_starting_balances(st)
    once = int(st["currency"]["1"])
    migrate_starting_balances(st)
    check("the repair is one-time", int(st["currency"]["1"]) == once,
          f"{once} -> {st['currency']['1']}")


def main():
    for fn in (check_the_two_numbers_are_not_the_same_one,
               check_cap_matches_the_client, check_new_account,
               check_rewards_now_move_the_numbers, check_rank_up_pays_what_the_screen_says,
               check_migration_is_one_shot_and_safe,
               check_rev1_clamp_is_repaired):
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

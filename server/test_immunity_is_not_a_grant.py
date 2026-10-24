#!/usr/bin/env python3
""""Gains immunity to Charm" does not grant Charm.

    python3 test_immunity_is_not_a_grant.py

`_GRANTS` matches `gains`, so the first sentence of Asmodeus's note -- "Asmodeus gains
immunity to Enchant and Charm." -- was graded a STRONG match for Charm and returned
before the sentence that actually casts it. All three of the Charm effect's fields then
came off the wrong sentence: no trigger at all, so the rule builder dropped it; `allies`
for a side; and 1 turn where the prose says 2. 19 of her 23 variants compiled that way,
which is why Devil's Tempt never did anything.

Both checks matter, and the second is the one that nearly went wrong twice. The skip is
bounded by what the sentence is ABOUT, so it must not swallow a sentence that states an
immunity AND grants something else -- Leviathan's Jealousy Vortex is exactly that shape,
and an early version of the fix cost Gale and Slow their trigger. Asserted on compiled
data, since that is where the damage showed up.
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tools"))
os.environ["SEVENSINS_ACCOUNTS"] = tempfile.mkdtemp(prefix="sevensins-immunity-test-")

import battle as bt                                            # noqa: E402
from engine import passives as P, specs                         # noqa: E402
import compile_skills as cs                                      # noqa: E402

FAILURES = []


def check(cond, msg):
    if not cond:
        FAILURES.append(msg)


def the_clause_picker_reads_past_an_immunity():
    """Unit-level, on the exact sentences that produced the bug."""
    note = ("Queen's Aura: Asmodeus gains immunity to Enchant and Charm. When a battle "
            "starts, grants all allies Charm immunity for 1 turn.\nDevil' s Tempt: When "
            "defeated, cast Charm on the enemy with the highest ATK for 2 turns.")
    clause, strong = cs._clause_for(note, "Charm", _with_strength=True)
    check(strong and "When defeated" in (clause or ""),
          f"Charm matched {clause!r} instead of the sentence that casts it")
    # The row NAMED for an immunity must still find its own sentence -- for it the
    # immunity phrase IS the grant.
    for name, want in (("Charm Immunity", "grants all allies"),
                       ("Enchant Immunity", "gains immunity")):
        clause, strong = cs._clause_for(note, name, _with_strength=True)
        check(strong and want in (clause or ""),
              f"{name} matched {clause!r}, wanted the sentence containing {want!r}")


def a_mixed_sentence_keeps_its_grants():
    """An immunity phrase must not swallow the rest of its sentence.

    "gains immunity to Freeze for 3 turns and inflict Gale on all allies and Slow on all
    enemies" -- the skip covers `Freeze` and nothing else.
    """
    note = ("Jealousy Vortex: When a battle starts, gains immunity to Freeze for 3 turns "
            "and inflict Gale on all allies and Slow on all enemies.")
    for name in ("Gale", "Slow", "Freeze Immunity"):
        clause, strong = cs._clause_for(note, name, _with_strength=True)
        check(strong, f"{name} lost its clause to the immunity skip: {clause!r}")


def devils_tempt_fires_on_her_own_death():
    """End to end on compiled data: the right trigger, and the right single target."""
    sk = specs.skills()
    tempts = [sid for sid, sp in sk.items()
              if (sp.get("name") or "").startswith("Devil's Tempt")]
    check(tempts, "no Devil's Tempt in the compiled data -- wrong fixture")
    on_death = [sid for sid in tempts
                if any(e.get("trigger") == "on_death"
                       and ((e.get("status") or {}).get("name")) == "Charm"
                       for e in sk[sid].get("effects") or [])]
    check(len(on_death) >= 19,
          f"only {len(on_death)} of {len(tempts)} Devil's Tempt variants compile the "
          "Charm clause with on_death")

    b = bt.Battle(1101, [10061, 10401])             # Asmodeus + an ally
    asmodeus = b.units["101"]
    enemies = [u for u in b.units.values() if u.team == 2]
    check(len(enemies) >= 2, "stage 1101 fielded fewer than 2 enemies")
    for i, u in enumerate(enemies):
        u.atk = 9000 if i == 0 else 1000
    before = {u.order: {s.name for s in u.statuses} for u in enemies}
    asmodeus.hp = 0
    units = list(b.units.values())
    P.fire_all(P.ON_DEATH, units, units, ctx={"victim": asmodeus})
    got = {u.order: {s.name for s in u.statuses} - before[u.order] for u in enemies}
    check("Charm" in got[enemies[0].order],
          f"the top-ATK enemy was not Charmed by her death: {got}")
    for u in enemies[1:]:
        check("Charm" not in got[u.order],
              f"Charm hit {u.order} too -- the prose names ONE enemy: {got}")


def main():
    for fn in (the_clause_picker_reads_past_an_immunity,
               a_mixed_sentence_keeps_its_grants,
               devils_tempt_fires_on_her_own_death):
        fn()
    for f in FAILURES:
        print("  FAIL", f)
    print(f"\n{'all OK' if not FAILURES else str(len(FAILURES)) + ' failure(s)'}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())

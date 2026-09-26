#!/usr/bin/env python3
"""The fuzzer must actually field what a real party carries.

    python3 test_fuzz_coverage.py

THE BUG THIS EXISTS FOR. tools/battle_fuzz.py built fights from bare char ids. Everything
a cast carries into a real fight -- equipment, Soulmirrors, bloodpact, the Consonance and
Skill Up stat ladders, the Consonance master passive -- is attached to a ROSTER ENTRY by
`player_state.roster.battle_team`, and none of it reaches a Unit built from a bare id.

So 4,000 fights reported "0 findings" while the master passive had never once been on the
field, and the same was true of gear and both stat ladders. The sweep was green about
code it never ran.

A fixed list of things to check would rot the same way, so this test ENUMERATES the
annotators out of roster.py and fails on any that never influences a fuzzed party. Add
`_annotate_foo` tomorrow and this fails until the fuzzer fields it -- which is the point.

Uses its own SEVENSINS_ACCOUNTS tempdir; it never reads or writes a real account.
"""
import inspect
import os
import random
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
os.environ["SEVENSINS_ACCOUNTS"] = tempfile.mkdtemp(prefix="sevensins-fuzzcov-")

import ai_arena as A                                           # noqa: E402
import battle as bt                                            # noqa: E402
import battle_fuzz as F                                        # noqa: E402
from player_state import roster as rs                          # noqa: E402

FAILURES = []


def check(cond, msg):
    if not cond:
        FAILURES.append(msg)


def _sample_parties(n=24, richness=1.0):
    """Parties the way the fuzzer builds them."""
    rng = random.Random(11)
    pool = [int(c) for c in A.cast_pool()] if hasattr(A, "cast_pool") else None
    if not pool:
        import design_data as dd
        pool = [int(c) for c, r in (dd.rows("char") or {}).items()
                if r.get("_type") == 1 and r.get("_order")]
    out = []
    for _ in range(n):
        team = tuple(rng.sample(pool, 5))
        party = A.loadout_party(team, 60, random.Random(rng.randrange(1 << 30)),
                                richness)
        # None means the loadout path raised and the fight silently fell back to bare
        # casts -- which is exactly how this sweep came to be green about untested code.
        # The item pools are filtered to rollable pieces, so at richness 1.0 every team
        # must kit out; a None here is a regression in that filtering, not a tolerable
        # miss.
        assert party is not None, f"loadout_party returned None for {team}"
        out.append(party)
    return out


def every_annotator_influences_a_party():
    """Enumerated from roster.py, so a new annotator is covered the day it lands.

    Each annotator is WRAPPED and the real loadout path is run, rather than each being
    probed in isolation: several only write anything when the state actually holds the
    thing they resolve (a bloodpact in storage 4, a Karma rank), so calling one against an
    empty state proves nothing either way.
    """
    annotators = [n for n, f in vars(rs).items()
                  if n.startswith("_annotate_") and inspect.isfunction(f)]
    check(annotators, "no _annotate_* functions found -- roster.py moved?")

    wrote = {n: 0 for n in annotators}
    originals = {n: getattr(rs, n) for n in annotators}

    def wrap(name, fn):
        def inner(state, entry, *a, **kw):
            before = dict(entry)
            out = fn(state, entry, *a, **kw)
            if entry != before:
                wrote[name] += 1
            return out
        return inner

    try:
        for n in annotators:
            setattr(rs, n, wrap(n, originals[n]))
        parties = _sample_parties()
    finally:
        for n, f in originals.items():
            setattr(rs, n, f)

    flat = [e for p in parties for e in p if isinstance(e, dict)]
    check(flat, "loadout_party produced no roster entries at all")
    for name in sorted(annotators):
        check(wrote[name],
              f"{name} ran but never wrote anything to a fuzzed entry -- the sweep is "
              f"green about code it does not exercise. Field it in "
              f"tools/ai_arena.loadout_party.")


def the_loadout_actually_reaches_units():
    """Entries are only half of it: the Unit has to receive them."""
    rng = random.Random(5)
    import design_data as dd
    pool = [int(c) for c, r in (dd.rows("char") or {}).items()
            if r.get("_type") == 1 and r.get("_order") and bt.master_passive(int(c))]
    check(pool, "no cast with a master passive to test with")
    team = tuple(rng.sample(pool, 5))
    party = A.loadout_party(team, 60, random.Random(9), 1.0)
    b = A.mirror_battle(team, 60, party=party)

    players = [u for u in b.units.values() if u.team == bt.TEAM_PLAYER]
    check(players, "mirror battle fielded no player units")
    check(any(getattr(u, "max_hp", 0) for u in players), "units have no HP")

    # Gear: a kitted unit must out-stat a bare one of the same cast and level.
    bare = A.mirror_battle(team, 60, party=None)
    bare_by_cast = {u.char_id: u for u in bare.units.values()
                    if u.team == bt.TEAM_PLAYER}
    richer = sum(1 for u in players
                 if u.char_id in bare_by_cast
                 and u.max_hp > bare_by_cast[u.char_id].max_hp)
    check(richer, "no kitted unit has more HP than its bare twin -- gear/ladders are "
                  "not reaching Unit")

    # The master passive: an extra skill the bare unit does not have.
    extra = sum(1 for u in players
                if u.char_id in bare_by_cast
                and len(u.skills or []) > len(bare_by_cast[u.char_id].skills or []))
    check(extra, "no unit gained the Consonance master passive")

    # BOTH sides, not just the player's.
    enemies = [u for u in b.units.values() if u.team == bt.TEAM_ENEMY]
    check(any(len(u.skills or []) > len(bare_by_cast.get(u.char_id, u).skills or [])
              for u in enemies),
          "the enemy side fights bare -- an effect is only half-tested that way")


def the_bare_path_survives():
    """richness 0 must still produce a runnable fight; that path is covered too."""
    rng = random.Random(2)
    import design_data as dd
    pool = [int(c) for c, r in (dd.rows("char") or {}).items()
            if r.get("_type") == 1 and r.get("_order")]
    team = tuple(rng.sample(pool, 5))
    check(F.party_for({"team": team, "level": 50, "richness": 0.0,
                       "loadout_seed": 1}) is None,
          "richness 0 still built a loadout")
    b = A.mirror_battle(team, 50, party=None)
    check(b.units, "a bare mirror battle built no units")


def a_loadout_is_reproducible():
    """The same seed must build the same party, or a repro line is a lie.

    THE BUG THIS EXISTS FOR. `grant_bloodpact` took no `rng` (its two siblings,
    `grant_rune` and `grant_soulmirror`, both did), so the pact's ReplaceSkill rows were
    drawn from the global `random`. The first finding the loadout sweep produced -- a unit
    starved of turns for a whole fight -- could not be reproduced from its own repro line,
    because the pact differed on every run. The finding is simply gone.

    Determinism is the property that makes a fuzzer useful rather than merely alarming.
    """
    team = (11021, 10651, 20821, 20321, 20041)

    def build():
        p = A.loadout_party(team, 250, random.Random(381483515), 0.35)
        return [(e.get("id"), e.get("pact_iid"), e.get("pact_lv"), e.get("master_lv"),
                 tuple(sorted((e.get("gear_bonus") or {}).items())))
                for e in (p or [])]

    runs = [build() for _ in range(3)]
    check(runs[0], "loadout_party built nothing for a fixed seed")
    check(runs[0] == runs[1] == runs[2],
          "the same loadout seed built DIFFERENT parties -- something in the grant path "
          "is using the global random, so a fuzz finding cannot be reproduced")
    # A different seed must actually differ, or "deterministic" is hiding a constant.
    other = A.loadout_party(team, 250, random.Random(999), 1.0)
    check([(e.get("id"), e.get("pact_iid")) for e in (other or [])]
          != [(r[0], r[1]) for r in runs[0]],
          "a different seed built an identical party -- the rng is not reaching the draw")


def accounts_are_never_touched():
    """The sweep must not be able to read or write real save data (CLAUDE.md s.10)."""
    real = os.path.join(HERE, "accounts")
    acct = os.environ.get("SEVENSINS_ACCOUNTS", "")
    check(acct and os.path.abspath(acct) != os.path.abspath(real),
          f"SEVENSINS_ACCOUNTS points at real save data: {acct}")
    import player_state as ps
    check(os.path.abspath(getattr(ps, "STATE_DIR", acct)) != os.path.abspath(real),
          "player_state resolved its account dir to server/accounts/")


def main():
    every_annotator_influences_a_party()
    the_loadout_actually_reaches_units()
    the_bare_path_survives()
    a_loadout_is_reproducible()
    accounts_are_never_touched()
    for f in FAILURES:
        print("FAIL:", f)
    print(f"{len(FAILURES)} failure(s)")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())

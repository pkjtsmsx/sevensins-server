#!/usr/bin/env python3
"""Which compiled effects the engine actually does something with. -> a coverage matrix.

**Why this exists.** The compiled artifact is a small, closed vocabulary -- nine ops over
43,527 effects -- and the engine dispatches on that vocabulary in two independent places:
`core.execute` for a skill being USED, and `passives._derived_rules` for a skill being
HELD. Nothing kept those two in step. `core.execute` handled all nine ops; the passive
path handled five, and `damage` on one trigger out of five, so 261 compiled passive
damage effects -- every counterattack in the game among them -- were read off disk,
carried through the spec, and then silently dropped on the floor.

That bug was found by accident while chasing something else. This finds the rest of its
family on purpose, and it is the cheap half of "audit the effect taxonomy": the taxonomy
is already the engine's shape, so what is worth auditing is COVERAGE of it.

**It probes, it does not grep.** Every cell is measured by building a synthetic spec
carrying ONE real effect and running the real function on it, then looking for output.
A matrix built by scanning source for `op == "..."` tells you a branch exists, not that
it produces anything, and it rots the first time the code moves. This cannot: if the
engine stops handling something, the probe stops producing output.

**Only COMPLETE effects are probed.** An effect missing the number it needs produces
nothing for a reason that is not the engine's fault, and counting those as gaps would
bury the real ones. `tools/` has no business guessing which of those are recoverable --
that is `compile_skills.py`'s problem and it is reported separately at the bottom.

    python3 tools/effect_coverage.py            # the matrix and the gaps
    python3 tools/effect_coverage.py --verbose  # plus a sample skill id per cell
"""
import argparse
import collections
import glob
import json
import os
import random
import sys
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(os.path.dirname(HERE), "server")
sys.path.insert(0, SERVER)

from engine import core, passives, specs          # noqa: E402

# RESOLVED AT IMPORT, ON PURPOSE. The first version of this tool reached for
# `passives._derived_rules` -- which does not exist, the function is `_compiled_rules` --
# inside a `try/except Exception` meant for effects that blow up while running. The
# AttributeError was swallowed on every call, so every passive cell came back "not
# handled" and the tool cheerfully reported 20,028 gaps including the counterattacks
# fixed an hour earlier. A coverage tool that fails silently is worse than none, because
# it is believed. Binding the entry points here means a rename kills the run instead.
_EXECUTE = core.execute
_PASSIVE_RULES = passives._compiled_rules

# What each op needs before it can do anything, so an effect starved of data is never
# reported as an engine gap. `apply_status` is the awkward one: plenty of statuses need
# no magnitude at all (immunity, taunt, a plain stun), so the requirement is the status
# id rather than a number.
REQUIRED = {
    "damage": lambda e: e.get("coefficient") is not None,
    "heal": lambda e: e.get("percent") is not None,
    "modify_gauge": lambda e: e.get("percent") is not None,
    "revive": lambda e: e.get("percent") is not None,
    "modify_cd": lambda e: e.get("turns") is not None,
    "follow_up": lambda e: e.get("skill") is not None,
    "apply_status": lambda e: (e.get("status") or {}).get("id") is not None,
    # A cleanse needs a category that some status can actually HAVE. 'unknown' is the
    # compiler failing to decode the category-block operand, and no status in the
    # registry carries it, so such an effect can never match anything -- a data defect,
    # not an engine one, and counting it as a gap blamed the wrong component.
    "remove_status": lambda e: ((e.get("category") or (e.get("status") or {}).get(
        "category")) in PROBE_CATEGORIES) or bool((e.get("status") or {}).get("id")),
    "attack_rider": lambda e: True,
}

# Every way an Outcome can show that something happened. Kept as a list rather than a
# per-op expectation on purpose: the question this tool asks is "did the engine do
# ANYTHING with this effect", and an op that lands in a channel other than the obvious
# one is still handled. Being wrong about which channel is a different audit.
# `immune` belongs here: a status the engine processed and the TARGET resisted is
# handled -- the engine ran the effect and recorded the refusal, which is the behaviour
# the client's "IMMUNE" float depends on. Leaving it out counted every resisted
# application as "nothing happened" and invented gaps out of working code.
#
# `skipped` deliberately does NOT belong here. That is the engine saying it declined to
# run the effect, which is exactly what this tool is looking for.
CHANNELS = ("strikes", "statuses", "heals", "gauge", "revives", "cooldowns", "immune")

# Per-cell sample size. Deliberately generous: the gap list EXTRAPOLATES a sample's miss
# rate onto the whole cell, so a small sample makes the ranking noisy rather than merely
# imprecise. At 12 a cell whose true miss rate was 2% (`heal/None`, 3 of 150 measured by
# hand) drew one miss and reported 54 of 649. A handled effect exits on its first seed,
# so the cost of raising this falls almost entirely on cells that really do have gaps.
SAMPLES_PER_CELL = 60


def _registry_categories():
    """Every category a status in the registry actually has.

    READ, not hardcoded. `remove_category` matches a cleanse's category against the HELD
    status's, so a probe field missing one reports "unhandled" for want of something to
    remove -- and the hand-written list this replaces was already wrong, carrying `misc`
    and `other` while missing `stat_up` and `content`.
    """
    import json as _json
    path = os.path.join(SERVER, "battle_data", "statuses.json")
    with open(path) as fh:
        return sorted({v.get("category") for v in _json.load(fh).values()
                       if v.get("category")})


PROBE_CATEGORIES = _registry_categories()


def _statuses():
    """One live status per removable category, so a cleanse always has something to bite."""
    from engine import status as est
    return [est.Active(status_id=9000 + i, name=f"Probe {cat}", kind="other",
                       category=cat, remaining=3)
            for i, cat in enumerate(PROBE_CATEGORIES)]


def _field():
    """A field rigged so every op has something to act on.

    A bare caster-and-enemy pair is not enough, and getting this wrong is how the first
    run of this tool reported `remove_status`, `revive`, `damage` and `follow_up` as
    unhandled when all four work: a cleanse with no statuses on the field, a revive with
    nobody dead and a damage effect with nobody targetable all produce nothing for
    reasons that have nothing to do with the engine.
    """
    caster = core.Unit(order="1", team=0, max_hp=100000, hp=60000, atk=3000,
                       defence=1500, spd=200)
    ally = core.Unit(order="2", team=0, max_hp=100000, hp=50000, atk=2000,
                     defence=1000, spd=150)
    dead = core.Unit(order="3", team=0, max_hp=100000, hp=0, atk=2000,
                     defence=1000, spd=150)          # so revive has a candidate
    foe = core.Unit(order="4", team=1, max_hp=100000, hp=60000, atk=3000,
                    defence=1500, spd=200)
    foe2 = core.Unit(order="5", team=1, max_hp=100000, hp=60000, atk=3000,
                     defence=1500, spd=200)
    for u in (caster, ally, foe, foe2):
        u.statuses = _statuses()
    return caster, [caster, ally, dead, foe, foe2]


def _summary(out):
    """A comparable fingerprint of an Outcome -- what happened, not how hard."""
    return (len(out.strikes), len(out.statuses), len(out.heals), len(out.gauge),
            len(out.revives), len(out.cooldowns), len(out.children),
            len(out.immune),
            sum(1 for s in out.statuses if not s.applied),
            tuple(sorted(getattr(s, "status_id", 0) or 0 for s in out.statuses)))


def _probe_spec(spec, effects):
    """A spec carrying exactly `effects`, keeping whatever the parent said about shape."""
    return {"id": spec.get("id"), "group": spec.get("group"),
            "type": spec.get("type"), "swings": spec.get("swings") or 1,
            "target": spec.get("target"), "effects": list(effects)}


def _run(spec, effects, seed):
    caster, units = _field()
    try:
        return _summary(_EXECUTE(caster, _probe_spec(spec, effects), units,
                                 rng=random.Random(seed), round_no=1,
                                 # The player's pick, for the `select: count` breadth
                                 # rule -- `resolve_targets` leads with it and fills the
                                 # rest by slot, which makes a multi-target probe
                                 # reproducible instead of order-dependent.
                                 chosen="4"))
    except Exception:
        # An effect that RAISES is not a coverage question -- that is a bug, and the
        # fuzzer is what hunts those. Report the seed as inconclusive.
        return None


def _probe_active(spec, eff):
    """-> True if ADDING this effect changes what `core.execute` produces.

    DIFFERENTIAL, not "did anything come out". Several ops only show up as a change
    against a baseline: a `remove_status` produces a removal event that a bare run does
    not, a `revive` brings back a unit nobody else touched. Comparing with-against-
    without also means a cell cannot pass on output some OTHER effect produced.

    Repeated across seeds because plenty of effects are gated on a stated probability or
    fall to CONDITIONAL_POLICY, which rolls -- one unlucky seed would report a handled
    effect as a gap.
    """
    for seed in range(SAMPLES_PER_CELL):
        with_eff = _run(spec, [eff], seed)
        without = _run(spec, [], seed)
        if with_eff is not None and without is not None and with_eff != without:
            return True
    return False


def _probe_passive(spec, eff):
    """-> True if the passive path turns this effect into a Rule."""
    try:
        return bool(_PASSIVE_RULES(_probe_spec(spec, [eff])))
    except Exception:
        return False


def load_effects():
    """-> [(skill_id, spec, effect)] for every effect in the compiled artifact."""
    ids = set()
    for path in glob.glob(os.path.join(SERVER, "battle_data", "skills", "**", "*.json"),
                          recursive=True):
        if path.endswith("_index.json"):
            continue
        try:
            blob = json.load(open(path))
        except Exception:
            continue
        if isinstance(blob, dict):
            ids.update(int(k) for k in blob if str(k).isdigit())
    out = []
    for sid in sorted(ids):
        spec = specs.skill(sid) or {}
        for eff in spec.get("effects") or []:
            out.append((sid, spec, eff))
    return out


def coverage():
    """-> (rows, gaps, starved). The whole matrix, without printing any of it.

    Split out so `server/test_effect_coverage.py` can assert on the gap set instead of
    parsing this tool's output. The run is ~0.5s, so the suite can afford to hold the
    line on every commit rather than waiting for somebody to remember to run the tool.
    """
    effects = load_effects()
    # (op, trigger) -> complete effects, and separately the starved ones.
    cells = collections.defaultdict(list)
    starved = collections.Counter()
    untargetable = collections.Counter()
    for sid, spec, eff in effects:
        op = eff.get("op")
        if not REQUIRED.get(op, lambda _e: True)(eff):
            starved[op] += 1
            continue
        # NOT PROBEABLE THROUGH `execute`, and not a gap. `resolve_targets` returns []
        # for `select` in (None, "none", "unknown") by design: these are the status-row
        # pseudo-skills, which reach the field through `status.run_nested` rather than
        # by being cast. Probing them anyway put 21 of the first 60 `apply_status/None`
        # misses into the gap list as engine faults when the engine was right.
        if (spec.get("target") or {}).get("select") in (None, "none", "unknown"):
            untargetable[op] += 1
            continue
        cells[(op, eff.get("trigger"))].append((sid, spec, eff))

    rows, gaps = [], []
    for (op, trigger), members in sorted(cells.items(),
                                         key=lambda kv: (kv[0][0], str(kv[0][1]))):
        # SAMPLED AT RANDOM, seeded so the run repeats. Taking the first N sorts by
        # skill id, and the low ids are the status-row pseudo-skills whose target is
        # `select: none` -- nothing to hit. 52 of the 8,751 `damage/None` effects are
        # like that and the first twelve were all of them, so the busiest cell in the
        # game reported as an engine gap.
        # crc32, NOT hash(): str.__hash__ is salted per process (PYTHONHASHSEED), so a
        # `hash()`-seeded sample is a DIFFERENT sample every run. That made this tool
        # non-reproducible and, worse, intermittently wrong -- a cell whose handled
        # members are a minority would pass on one run and report a gap on the next.
        picker = random.Random(zlib.crc32(f"{op}/{trigger}".encode()))
        sample = picker.sample(members, min(SAMPLES_PER_CELL, len(members)))
        # A FRACTION, not a boolean. "Any sampled member worked" is the wrong question:
        # a cell is routinely part-handled, because whether an effect can be paid often
        # depends on the effect rather than on the op. `damage / after_action` is the
        # case that forced this -- 62 of its members carry a resolved `select` and the
        # rest do not, so a boolean called the whole cell closed and hid 38 effects.
        act_ok = sum(1 for _s, sp, e in sample if _probe_active(sp, e))
        # A passive rule needs a trigger the rule table knows; an untriggered effect is
        # an ACTIVE effect and its passive cell is not a gap, it is not applicable.
        if trigger in passives.ALL_TRIGGERS:
            pas_ok = sum(1 for _s, sp, e in sample if _probe_passive(sp, e))
        else:
            pas_ok = None
        n = len(sample)
        rows.append((op, trigger, len(members), act_ok, pas_ok, n, sample[0][0]))

        def _gap(path, ok):
            if ok is None or ok == n:
                return
            # Scale the sample's miss rate back onto the cell, so the gap list ranks by
            # how many effects it actually costs rather than by how many cells it spans.
            missing = int(round(len(members) * (n - ok) / n))
            gaps.append((path, op, trigger, missing, len(members), sample[0][0]))

        _gap("passive", pas_ok)
        if trigger is None:
            _gap("active", act_ok)
    return rows, gaps, starved, untargetable, len(effects)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--verbose", action="store_true",
                    help="name a sample skill id for every cell")
    args = ap.parse_args()

    effects = load_effects()
    disagreements = parity(effects)
    rows, gaps, starved, untargetable, total = coverage()
    print(f"{total} effects, {len(rows)} (op, trigger) cells, "
          f"{sum(starved.values())} starved of the data they need\n")

    def mark(ok, n):
        if ok is None:
            return "  -  "
        if ok == n:
            return " yes "
        return " NO  " if ok == 0 else f"{ok}/{n}"

    print(f"{'op':<15}{'trigger':<18}{'effects':>9}  {'active':^6} {'passive':^7}"
          + ("  sample" if args.verbose else ""))
    print("-" * (50 + 16 + (10 if args.verbose else 0)))
    for op, trigger, total_n, act_ok, pas_ok, n, sid in rows:
        line = (f"{op:<15}{str(trigger):<18}{total_n:>9}  "
                f"{mark(act_ok, n):^6} {mark(pas_ok, n):^7}")
        if args.verbose:
            line += f"  {sid}"
        print(line)

    print()
    # Parity: the two paths on the SAME effect. A coverage row can read `yes / yes`
    # while the two produce different numbers -- see the note above PARITY_TARGETS.
    print(f"PARITY -- {len(disagreements)} effect(s) where the active and passive paths "
          f"disagree on amount or recipient")
    for sid, eff, act, pas in disagreements[:10]:
        print(f"  skill {sid} {eff.get('op')} "
              f"target={eff.get('target')} basis={eff.get('basis')} "
              f"count={eff.get('count')}")
        print(f"      active  {act}")
        print(f"      passive {pas}")
    if len(disagreements) > 10:
        print(f"  ... and {len(disagreements) - 10} more")

    print()
    if gaps:
        print("GAPS -- effects the engine carries and does nothing with, "
              "worst first:\n")
        for path, op, trigger, miss, total_n, sid in sorted(gaps, key=lambda g: -g[3]):
            of = f" of {total_n}" if miss != total_n else ""
            print(f"  {miss:>6} effects{of:<10}  {path:<8} {op} / {trigger}"
                  f"   (e.g. skill {sid})")
        print(f"\n  {sum(g[3] for g in gaps)} effects total.")
    else:
        print("No gaps: every complete effect produces something on every path "
              "that applies to it.")

    if untargetable:
        print("\nNot probed -- specs whose own target is `none`/`unknown`, so `execute`\n"
              "resolves no targets by design. These reach the field through\n"
              "`status.run_nested`, not by being cast:\n")
        for op, n in untargetable.most_common():
            print(f"  {n:>6} {op}")

    if starved:
        print("\nNot counted above -- effects missing the data they need. These are a "
              "\ncompile_skills.py problem, not an engine one:\n")
        for op, n in starved.most_common():
            print(f"  {n:>6} {op}")
    return 1 if gaps else 0


if __name__ == "__main__":
    sys.exit(main())


# --- parity: the two paths agree on the SAME effect --------------------------------
#
# Coverage asks "does this path do anything with the effect". Parity asks the harder
# question -- "do the two paths do the SAME thing" -- and it is the one that catches the
# bug class coverage cannot see: a field that travels on the effect, is honoured by one
# consumer, and is silently dropped by the other. Three of those shipped:
#
#   revive `count`   -- core raised EVERY fallen ally, passives raised exactly one.
#                       Same field, same artifact, two implementations, two directions.
#   heal `basis`     -- passives forced `self_max_hp`, so a heal stated as 100% of ATK
#                       paid 100% of the holder's MAX HP. Punica's Guard Breath healed
#                       ~19x what the prose says, every turn, on a live party.
#   heal `count`     -- `allies_lowest` became "the whole party".
#
# None of them raised, none of them failed a suite, and the artifact was RIGHT the whole
# time. A human found the first on a phone; this is what finds the next one.
#
# Only effects whose recipient vocabulary BOTH paths understand are compared -- caster,
# allies, allies_lowest. A skill aimed at enemies and a passive held by a unit genuinely
# select differently, and reporting that as disagreement would bury the real rows.
PARITY_TARGETS = ("caster", "allies", "allies_lowest")
PARITY_OPS = ("heal", "revive")


def _core_amounts(spec, eff):
    """-> {order: amount} the ACTIVE path produces for one effect."""
    caster, units = _field()
    out = _EXECUTE(caster, _probe_spec(spec, [eff]), units,
                   rng=random.Random(0), round_no=1, chosen="4")
    rows = out.heals if eff["op"] == "heal" else out.revives
    key = "amount" if eff["op"] == "heal" else "hp"
    return {r["target"]: r[key] for r in rows}


# An id no real skill uses, so the probe's rules can be seeded into the derived-rule
# cache and fired through the REAL `fire`. Calling `fire` with the effect's own skill id
# would run that skill's WHOLE rule list, not the one effect under test -- which is how
# the first run of this reported five revive disagreements that were the harness
# comparing a single probed effect against a passive's full behaviour.
_PROBE_ID = -424242


def _passive_amounts(spec, eff):
    """-> {order: amount} the PASSIVE path produces for the same effect."""
    caster, units = _field()
    probe = _probe_spec(spec, [dict(eff, trigger="turn_start")])
    probe["type"] = "passive"
    rules = _PASSIVE_RULES(probe)
    if not rules:
        return {}
    passives._COMPILED[_PROBE_ID] = rules
    try:
        fired = passives.fire("turn_start", caster, _PROBE_ID, units,
                              ctx={"rng": random.Random(0)}) or []
    finally:
        passives._COMPILED.pop(_PROBE_ID, None)
    out = {}
    for row in fired:
        if len(row) == 3 and row[1] in ("heal", "revive"):
            out[row[0].order] = row[2]
    return out


def parity(effects):
    """Compare the two paths on every effect both can express. -> [(sid, eff, a, b)]."""
    bad = []
    for sid, spec, eff in effects:
        if eff.get("op") not in PARITY_OPS:
            continue
        if eff.get("target") not in PARITY_TARGETS:
            continue
        if not REQUIRED[eff["op"]](eff):
            continue
        try:
            a, b = _core_amounts(spec, eff), _passive_amounts(spec, eff)
        except Exception:                                       # noqa: BLE001
            continue
        if not b:                       # the passive path declined it -- a COVERAGE gap
            continue
        if a != b:
            bad.append((sid, eff, a, b))
    return bad

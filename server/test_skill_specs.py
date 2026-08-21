#!/usr/bin/env python3
"""Phase-0 static harness: validate the compiled skill specs.

The whole point of compiling skills offline is that a wrong skill becomes a visible data
problem instead of a mystery at turn 7 of a fight. That is only true if something
actually checks the data -- this is that something.

What it asserts, and why each one exists:

  * **every referenced status resolves.** An `apply_status` naming a status row that does
    not exist would silently do nothing at runtime.
  * **every `follow_up` resolves, and the graph is acyclic.** Follow-ups are recursive --
    a type-7 sub-skill is a full spec with its own targeting and swings, so `execute()`
    calls itself. A cycle would hang the server rather than the client.
  * **attack skills have at least one swing.** Zero swings means the client's cinematic
    fires Damage tags that consume nothing, and the swing animates with no number -- the
    exact silent bug this whole effort started from.
  * **targeting resolves to a rule we understand.** `select: "unknown"` means we would be
    guessing at breadth, which is how AoE skills ended up hitting one target.
  * **decode coverage does not regress.** Partially-decoded skills are legitimate today
    (the trigger opcodes are undecoded), but the number must not silently grow.

    python3 test_skill_specs.py
"""
import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SKILLS_DIR = os.path.join(HERE, "battle_data/skills")

# Baselines, measured 2026-08-20. These are a ratchet: they may improve as opcodes are
# decoded, and a regression is a failure. Update them deliberately, never to make a test
# pass.
BASELINE_FULLY_DECODED = 6964
BASELINE_PARTIAL = 5235
BASELINE_SKILLS = 14410

_fail = 0


def check(name, cond, detail=""):
    global _fail
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        _fail += 1


def _follow_ups(specs, sid):
    """-> the skills `sid` chains into via follow_up."""
    return [e["skill"] for e in specs.get(sid, {}).get("effects", [])
            if e["op"] == "follow_up" and e["skill"] in specs]


def load():
    """-> {skill id (int): spec}, merged across the per-cast files.

    Shared skills are duplicated into each owning cast's file so that each file is
    self-contained; merging here is therefore expected to see the same id twice, and the
    copies must be identical.
    """
    specs, dupes = {}, 0
    for path in glob.glob(os.path.join(SKILLS_DIR, "**/*.json"), recursive=True):
        if os.path.basename(path) == "_index.json":
            continue
        with open(path) as f:
            for sid, spec in json.load(f).items():
                sid = int(sid)
                if sid in specs:
                    dupes += 1
                    if specs[sid] != spec:
                        check(f"duplicate spec for {sid} is identical across files",
                              False, path)
                specs[sid] = spec
    return specs, dupes


def main():
    if not os.path.isdir(SKILLS_DIR):
        print(f"no compiled specs at {SKILLS_DIR} -- run tools/compile_skills.py")
        return 1
    specs, dupes = load()

    print("\ncorpus:")
    check("every skill row compiled", len(specs) == BASELINE_SKILLS,
          f"{len(specs)} vs {BASELINE_SKILLS}")
    check("shared skills are duplicated, not lost", dupes > 0, str(dupes))

    print("\nreferential integrity:")
    missing_status = []
    for sid, s in specs.items():
        for e in s["effects"]:
            st = e.get("status")
            if st and st.get("name") is None:
                missing_status.append((sid, st.get("id")))
    check("every apply/remove_status names a real status",
          not missing_status, f"{len(missing_status)} unresolved, e.g. {missing_status[:3]}")

    bad_follow = [(sid, e["skill"]) for sid, s in specs.items()
                  for e in s["effects"]
                  if e["op"] == "follow_up" and e["skill"] not in specs]
    check("every follow_up targets a real skill",
          not bad_follow, f"{len(bad_follow)}, e.g. {bad_follow[:3]}")

    # Follow-ups recurse, so a cycle would hang execute() rather than the client.
    #
    # Three-colour DFS, NOT a plain visited set: a shared visited set reports a DIAMOND
    # (two follow-up paths converging on one sub-skill) as a cycle, which is exactly the
    # false positive the first version of this test produced -- 42 of them. Cycle
    # detection has to track the CURRENT PATH; `BLACK` then keeps it linear.
    WHITE, GREY, BLACK = 0, 1, 2
    colour = {}

    def has_cycle(start):
        colour[start] = GREY
        stack = [(start, iter(_follow_ups(specs, start)))]
        while stack:
            node, it = stack[-1]
            nxt = next(it, None)
            if nxt is None:
                colour[node] = BLACK
                stack.pop()
                continue
            c = colour.get(nxt, WHITE)
            if c == GREY:                       # back-edge into the current path
                return True
            if c == WHITE:
                colour[nxt] = GREY
                stack.append((nxt, iter(_follow_ups(specs, nxt))))
        return False

    roots = {sid for sid, s in specs.items()
             if any(e["op"] == "follow_up" for e in s["effects"])}
    cycles = [r for r in roots
              if colour.get(r, WHITE) == WHITE and has_cycle(r)]
    check("the follow_up graph is acyclic", not cycles,
          f"{len(cycles)} cyclic, e.g. {cycles[:3]}")

    print("\nclient contract:")
    ATTACK = {"com_attack", "skill", "sp_skill", "sub_skill"}
    zero_swing = [sid for sid, s in specs.items()
                  if s["type"] in ATTACK
                  and any(e["op"] == "damage" for e in s["effects"])
                  and not s["swings"]]
    check("every damaging skill has >= 1 swing", not zero_swing,
          f"{len(zero_swing)}, e.g. {zero_swing[:5]}")

    # Scoped to skills that actually TARGET something. The 17 rows with an unresolvable
    # `_target` are arena field effects and similar passives -- e.g. 7002101
    # `[力]屬性競技場場域效果` ("[STR] attribute arena field effect") -- which deal no damage
    # and apply nothing to a chosen unit. Their raw values (41, 42, 309, 421-423, 3102,
    # 3502) have no `23000 + _target` label because the client never renders one for them.
    # They stay honestly marked "unknown" in the data rather than being faked.
    needs_target = [sid for sid, s in specs.items()
                    if any(e["op"] in ("damage", "apply_status", "remove_status")
                           for e in s["effects"])
                    and s["type"] in ATTACK]
    unknown_target = [sid for sid in needs_target
                      if specs[sid]["target"].get("select") == "unknown"]
    check("targeting resolves for every skill that targets", not unknown_target,
          f"{len(unknown_target)} of {len(needs_target)}, e.g. {unknown_target[:5]}")

    # A skill that targets nothing must not carry damage, and vice versa.
    contradictory = [sid for sid, s in specs.items()
                     if s["target"].get("select") == "none"
                     and any(e["op"] == "damage" for e in s["effects"])
                     and s["type"] in ATTACK]
    check("no attack skill both damages and targets nothing",
          not contradictory, f"{len(contradictory)}, e.g. {contradictory[:5]}")

    print("\ndecode coverage (a ratchet -- must not regress):")
    full = sum(1 for s in specs.values() if s["effects"] and not s.get("unknown"))
    partial = sum(1 for s in specs.values() if s.get("unknown"))
    check("fully-decoded skills have not regressed",
          full >= BASELINE_FULLY_DECODED, f"{full} < {BASELINE_FULLY_DECODED}")
    check("partially-decoded skills have not grown",
          partial <= BASELINE_PARTIAL, f"{partial} > {BASELINE_PARTIAL}")
    print(f"        (fully {full}, partial {partial}, no effects "
          f"{len(specs) - full - partial})")

    # Undecoded opcodes must stay OUT of `effects` -- an engine walking that list would
    # skip them silently, which is the failure mode the split exists to prevent.
    leaked = [sid for sid, s in specs.items()
              if any(str(e.get("op", "")).startswith("raw_") for e in s["effects"])]
    check("no undecoded opcode leaked into `effects`", not leaked,
          f"{len(leaked)}, e.g. {leaked[:5]}")

    print(f"\n{_fail} failure(s)")
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(main())

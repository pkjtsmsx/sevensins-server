#!/usr/bin/env python3
"""Fuzz the battle engine with random fields, hunting crashes and silent wire violations.

    python3 tools/battle_fuzz.py                  # a quick pass
    python3 tools/battle_fuzz.py --fights 4000    # a real hunt
    python3 tools/battle_fuzz.py --seed 12        # a different corner of the space

## Why this exists

The engine's own suites walk the whole skill corpus, but always on a handful of hand-built
fields. What they never produce is COMBINATIONS: five arbitrary casts, their passives all
live at once, statuses landing on units that already hold conflicting ones, revives firing
into a field where the turn queue has already been pruned. Sampling teams at random walks
straight into that space -- `tools/ai_arena.py` hit a `ValueError` in `engine.core._report`
within eighty teams while trying to do something else entirely.

## Two classes of finding, and the second is the dangerous one

  * **Crashes.** An exception mid-turn kills the fight. On a phone the player sees the
    battle stop responding, and the traceback goes to a crash log nobody is reading.
  * **Silent wire violations.** These are worse, and they are this project's signature bug:
    the server applies everything correctly, sends a payload that breaks a client contract,
    and the client's generic handler SWALLOWS the error. No stack, no animation, the
    attacker never yields its turn, and the fight hangs with nothing wrong server-side.
    The confirmed instance is an unservable status id: element [1] of a status row is a
    SKILL id that the client feeds to `DesignSkillForm.GetRow`, which THROWS on a miss --
    see AttackBehavior.updateStatus (0x1be32d0). Nothing in the server would ever notice.

    The other confirmed instance is a duplicate `c` in the FIRST DamageInfo group.
    AttackBehavior.PlayStart (0x1be0a2c) clears MainDamagerDic and then does an unguarded
    Dictionary.Add keyed by `c` over DmgInfo[0], so a repeat throws "An item with the same
    key has already been added. Key: 101" -- observed on device -- before any damage is
    animated. Only group 0 reaches that loop, and md == 5 rows are exempt; a duplicate in a
    later group is malformed (the row is processed twice) but cannot hang. Both are
    reported, labelled. See BATTLE_ENGINE_PLAN.md phase 9.

So the invariants below are checked on the REAL payload, on every attack, rather than
trusting that a fight which did not raise is a fight that works.
"""
import argparse
import json
import os
import random
import sys
import traceback
import collections

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "server"))

import ai_arena as A                                           # noqa: E402
import battle as bt                                            # noqa: E402
import design_data as dd                                       # noqa: E402
from engine import specs as _specs                             # noqa: E402
from engine import status as _status                           # noqa: E402

VALID_MD = {1, 2, 3, 4, 5}  # noqa: E501
VALID_MD = VALID_MD | {10097}   # DamageMode.Immunity -- an "IMMUNE" floating text, moves no HP


def check_wire(combo, battle):
    """-> [violation strings] for one attack payload. Silent-failure class only."""
    bad = []
    if combo is None:
        return bad
    if combo.get("caster") not in battle.units:
        bad.append(f"caster {combo.get('caster')!r} is not on the field")
    for gi, group in enumerate(combo.get("data") or []):
        names = [row.get("c") for row in group]
        if len(names) != len(set(names)):
            dupes = [n for n, k in collections.Counter(names).items() if k > 1]
            # Group 0 is THE hang. AttackBehavior.PlayStart (0x1be0a2c) clears
            # MainDamagerDic and then does an UNGUARDED Dictionary.Add keyed by `c` over
            # DmgInfo[0] only -- a repeat throws "an item with the same key has already
            # been added", inside a handler that swallows it, BEFORE any damage is
            # animated. The attacker never yields its turn and the fight stalls forever
            # with the server none the wiser. Rows with md == 5 take a branch that writes
            # no dictionary, so they cannot trigger it.
            # A later group cannot hang, but a repeat there is still wrong: the row is
            # processed twice, double-counting CriCount and drawing two damage numbers
            # for one swing.
            if gi == 0 and any(r.get("md") != 5 for r in group
                               if r.get("c") in dupes):
                bad.append(f"group 0 names {dupes} twice (HANGS the fight)")
            else:
                bad.append(f"group {gi} names {dupes} twice")
        for row in group:
            if row.get("c") not in battle.units:
                bad.append(f"group {gi} names unknown unit {row.get('c')!r}")
            dmg = row.get("dmg")
            if not isinstance(dmg, int):
                bad.append(f"group {gi} unit {row.get('c')} has non-int dmg {dmg!r}")
            if row.get("die") not in (0, 1):
                bad.append(f"group {gi} unit {row.get('c')} has die={row.get('die')!r}")
            if row.get("md") not in VALID_MD:
                bad.append(f"group {gi} unit {row.get('c')} has md={row.get('md')!r}")
            for st in row.get("status") or []:
                # Exactly three. `AttackBehavior.updateStatus` (0x1be32d0) reads
                # [0], [1] and [2] behind `size <= 1` / `size <= 2` guards that throw
                # ArgumentOutOfRange, and skips a 1-element row outright -- so a short
                # row either kills the turn or silently drops the status.
                if not (isinstance(st, list) and len(st) in (3, 6)):
                    bad.append(f"group {gi} status row malformed: {st!r}")
                    continue
                if st[0] not in battle.units:
                    bad.append(f"group {gi} status row names unknown unit {st[0]!r}")
                if not isinstance(st[1], int) or not isinstance(st[2], int):
                    bad.append(f"group {gi} status row not ints: {st!r}")
                    continue
                # THE loading-screen hang. Element [1] is a SKILL id: the client feeds
                # it to `DesignSkillForm.GetRow`, which THROWS on a miss rather than
                # returning null, then feeds that row's status id to
                # `DesignStatusForm.GetRow`. A synthesised negative id -- passives mint
                # them for statuses with no design row -- is fatal to hand over.
                # `status.wire_status_id` is supposed to filter these out; this checks
                # the PAYLOAD rather than trusting the three call sites that build it.
                # Only rounds != 0 reaches the lookup: `updateStatus` gates it on
                # element [2], and 0 means remove.
                if st[2] and not (st[1] > 0 and _specs.status(st[1])):
                    bad.append(f"group {gi} status row has unservable id {st[1]!r}")
    return bad


def _spec_missing(skill_id):
    """-> True if the new engine has no spec, i.e. attack_cmd_json builds by hand."""
    try:
        return _specs.skill(skill_id) is None
    except Exception:                                          # noqa: BLE001
        return True


def check_cmd(cmd, battle):
    """-> [violation strings] for the BattleCmd ENVELOPE around the attack.

    `combo` is the part everyone looks at, but the same message carries the turn queue
    and the per-unit sync the client re-renders the whole field from. A stale order in
    `line` or a unit missing from `sync` is the same silent class as a duplicate `c`:
    the payload parses, the handler swallows what it cannot resolve, and the fight is
    quietly wrong rather than loudly broken.
    """
    bad = []
    for order in cmd.get("line") or []:
        if order not in battle.units:
            bad.append(f"line names unknown unit {order!r}")
    for order, row in (cmd.get("sync") or {}).items():
        if order not in battle.units:
            bad.append(f"sync names unknown unit {order!r}")
            continue
        if not (isinstance(row, list) and len(row) == 4):
            bad.append(f"sync row for {order} malformed: {row!r}")
            continue
        max_hp, hp = row[0], row[1]
        if not all(isinstance(x, (int, float)) for x in row):
            bad.append(f"sync row for {order} not numeric: {row!r}")
        elif not (0 <= hp <= max_hp):
            bad.append(f"sync says {order} hp {hp} outside [0, {max_hp}]")
    missing = set(battle.units) - set((cmd.get("sync") or {}))
    if missing:
        bad.append(f"sync omits units {sorted(missing)}")
    combos = cmd.get("combo") or []
    if len(combos) != 1:
        bad.append(f"envelope carries {len(combos)} combos, expected 1")
    return bad


def check_state(battle):
    """-> [violation strings] for the battle's own state after a turn."""
    bad = []
    for order, u in battle.units.items():
        if not (0 <= u.hp <= u.max_hp):
            bad.append(f"{order} hp {u.hp} outside [0, {u.max_hp}]")
        if not (0 <= float(u.scv) <= 100.0):
            bad.append(f"{order} move gauge {u.scv} outside [0, 100]")
        if any(c < 0 for c in u.cooldowns):
            bad.append(f"{order} negative cooldown {u.cooldowns}")
    return bad


def where(exc):
    """-> 'file:line' of the deepest frame inside our own code, for deduplication."""
    frames = traceback.extract_tb(exc.__traceback__)
    ours = [f for f in frames if "/server/" in f.filename or "/tools/" in f.filename]
    f = (ours or frames)[-1]
    return f"{os.path.basename(f.filename)}:{f.lineno}"


def compare_restored(before, after):
    """-> [violation strings] where a restored battle diverges from the live one.

    The resume path reconstructs from the stashed constructor args and replays the waves
    already cleared, then patches the dynamic state back in. That is a lot of moving parts
    for something the player only ever exercises after a server restart, i.e. exactly when
    nobody is watching.
    """
    bad = []
    if set(before.units) != set(after.units):
        bad.append(f"restored roster differs: {sorted(set(before.units) ^ set(after.units))}")
        return bad
    for order, u in before.units.items():
        v = after.units[order]
        if u.hp != v.hp:
            bad.append(f"{order} hp {u.hp} restored as {v.hp}")
        if u.cooldowns != v.cooldowns:
            bad.append(f"{order} cooldowns {u.cooldowns} restored as {v.cooldowns}")
        if len(u.statuses) != len(v.statuses):
            bad.append(f"{order} holds {len(u.statuses)} statuses, restored with "
                       f"{len(v.statuses)}")
    if before.wave != after.wave:
        bad.append(f"wave {before.wave} restored as {after.wave}")
    return bad


def one_fight(cfg, cap=200, cover=None):
    """Play one randomised fight. -> [(kind, key, detail)] findings."""
    found = []
    cover = cover if cover is not None else collections.Counter()
    rng = random.Random(cfg["seed"])
    acted, turns_played = {}, 0
    try:
        if cfg["kind"] == "mirror":
            b = A.mirror_battle(cfg["team"], cfg["level"])
        else:
            b = A.stage_battle(cfg["stage"], list(cfg["team"]), cfg["level"], 1)
    except Exception as exc:                                   # noqa: BLE001
        return [("crash", f"{type(exc).__name__} @ {where(exc)} (setup)", str(exc))]

    for _turn in range(cap):
        if not b.team_alive(1):
            break
        if not b.team_alive(2):
            if not b.has_next_wave():
                break
            try:
                b.advance_wave()
                cover["wave_advances"] += 1
                continue
            except Exception as exc:                           # noqa: BLE001
                found.append(("crash", f"{type(exc).__name__} @ {where(exc)}", str(exc)))
                break
        actor = b.acting_unit()
        if actor is None:
            break
        target_team = 2 if actor.team == 1 else 1
        try:
            move = A.CHOOSERS[cfg["chooser"]](b, target_team)
        except Exception as exc:                               # noqa: BLE001
            found.append(("crash", f"{type(exc).__name__} @ {where(exc)} (chooser)",
                          str(exc)))
            break
        if not move:
            break
        att, dfn, skill_id, slot = move
        acted[att] = acted.get(att, 0) + 1
        turns_played += 1
        attacker, target = b.units.get(att), b.units.get(dfn)
        if attacker is not None and target is not None:
            target = b._forced_target(attacker) or target
        # `attack_cmd_json`, NOT `bridge.attack_combo`. They are not the same payload
        # and the difference is exactly where a wire bug would hide: after the bridge
        # returns, `attack_cmd_json` still folds queued out-of-band status rows onto the
        # lead DamageInfo (`_drain_pending_status`), and for any skill the new engine has
        # no spec for it discards the bridge entirely and builds the groups by hand. A
        # fuzzer that checked the bridge's return value would pass both of those without
        # ever having looked at them.
        try:
            raw = b.attack_cmd_json(att, target.order if target else dfn, skill_id,
                                    rng=rng)
            cmd = json.loads(raw)
        except Exception as exc:                               # noqa: BLE001
            found.append(("crash", f"{type(exc).__name__} @ {where(exc)}",
                          f"skill {skill_id}: {exc}"))
            break
        cover["attacks"] += 1
        combos = cmd.get("combo") or []
        combo = combos[0] if combos else None
        cover["legacy_path" if _spec_missing(skill_id) else "engine_path"] += 1
        if combo:
            for group in combo.get("data") or []:
                for row in group:
                    if row.get("die"):
                        cover["deaths"] += 1
                    cover["status_rows"] += len(row.get("status") or [])
        for v in check_wire(combo, b) + check_cmd(cmd, b) + check_state(b):
            found.append(("wire", v.split(" ")[0] + " " + " ".join(v.split(" ")[2:4]),
                          f"skill {skill_id}: {v}"))
        try:
            b.spend_skill(att, slot)
            b.end_turn()
        except Exception as exc:                               # noqa: BLE001
            found.append(("crash", f"{type(exc).__name__} @ {where(exc)} (end_turn)",
                          str(exc)))
            break
        # Resume only makes sense for a real stage: `restore_battle` rebuilds from the
        # stashed constructor args, which a hand-built mirror field does not have.
        if cfg["kind"] == "stage" and cfg.get("resume_at") == _turn:
            try:
                restored = bt.restore_battle(b.to_state())
                cover["resumes"] += 1
                for v in compare_restored(b, restored):
                    found.append(("resume", v.split(" ")[1] if len(v.split(" ")) > 1
                                  else "state", v))
            except Exception as exc:                           # noqa: BLE001
                found.append(("crash", f"{type(exc).__name__} @ {where(exc)} (resume)",
                              str(exc)))
    found += check_starvation(b, acted, turns_played)
    return found


def check_starvation(b, acted, turns_played):
    """A unit that lives through a long fight and never once acts. -> [violations]

    THE INVARIANT THIS FUZZER WAS MISSING. Beelzebub's passive put a move-gauge block on
    herself -- the pack's English says "on all allies" where the Chinese says 敵方,
    enemies -- and because a blocked bar never fills, she never reached the front of the
    queue, so the block's own duration never ticked and she sat out the entire fight. It
    was found by a human watching a phone, after 2,000 fuzzed fights and 164,801 attacks
    said nothing: every check here was about the shape of a payload or an exception, and
    a unit silently doing nothing produces neither.

    Deliberately conservative, because a slow unit legitimately misses a short fight and
    a real enemy lock is a real game state: only a unit that is ALIVE at the end of a
    fight of 30+ turns, with zero turns taken, while others took plenty. Under that bar
    there is no legitimate reading -- the unit is being denied its turn by something that
    is not going to stop.
    """
    if turns_played < 30:
        return []
    out = []
    for order, u in b.units.items():
        if not u.alive or acted.get(order):
            continue
        # A unit can miss a whole fight for a legitimate reason: it is simply SLOW. A
        # mixed-pool mirror match fields support casts whose SPD is a rounding error
        # beside a striker's, and their bar climbs 0.1 in ten turns -- no bug, just a bad
        # unit. What is never legitimate is a bar that CANNOT rise, so the signal is the
        # block, not the absence of turns. Without this the check fired 57 times on its
        # first sweep and every one of them was a slow unit.
        if not _status.blocks_gauge_gain(u):
            continue
        held = [getattr(s, "name", "?") for s in getattr(u, "statuses", [])]
        out.append(("starved", f"unit never acts (team {u.team})",
                    f"{order} alive with {u.hp}/{u.max_hp} HP took 0 of {turns_played} "
                    f"turns and its gauge is still blocked; holds {held}"))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--fights", type=int, default=400)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--cap", type=int, default=200)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    pools = {p: A.playable(p) for p in ("player", "mob", "mixed")}
    stages = A.multiwave_stages(200, min_waves=1)
    choosers = sorted(A.CHOOSERS)

    findings = collections.defaultdict(list)
    cover = collections.Counter()
    casts = set()
    for i in range(args.fights):
        pool = rng.choice(list(pools))
        cfg = {
            "kind": rng.choice(("mirror", "mirror", "stage")),
            "team": tuple(rng.sample(pools[pool], 5)),
            "level": rng.choice((1, 15, 40, 80, 150, 250, 400)),
            "stage": rng.choice(stages),
            "chooser": rng.choice(choosers),
            "seed": rng.randrange(10 ** 6),
            "pool": pool,
            "resume_at": rng.randrange(0, 12),
        }
        casts.update(cfg["team"])
        cover["fights"] += 1
        for kind, key, detail in one_fight(cfg, cap=args.cap, cover=cover):
            findings[(kind, key)].append((detail, cfg))
        if (i + 1) % 200 == 0:
            print(f"  ...{i+1}/{args.fights} fights, "
                  f"{len(findings)} distinct finding(s)", flush=True)

    print(f"\n{args.fights} fights, {len(findings)} distinct finding(s)")
    # Coverage, so that "no findings" can be read as evidence rather than as an absence of
    # evidence. A fuzzer that never reaches an interesting state finds nothing either.
    print(f"  exercised: {cover['attacks']} attacks "
          f"({cover['engine_path']} engine / {cover['legacy_path']} legacy builder), "
          f"{cover['deaths']} deaths, {cover['status_rows']} status rows, "
          f"{cover['wave_advances']} wave advances, "
          f"{cover['resumes']} save/restore cycles, {len(casts)} distinct casts\n")
    for (kind, key), hits in sorted(findings.items(), key=lambda kv: -len(kv[1])):
        detail, cfg = hits[0]
        print(f"[{kind}] {key}  -- {len(hits)} hit(s)")
        print(f"    {detail[:200]}")
        print(f"    repro: kind={cfg['kind']} pool={cfg['pool']} lv={cfg['level']} "
              f"chooser={cfg['chooser']} seed={cfg['seed']}")
        print(f"           team={cfg['team']}"
              + (f" stage={cfg['stage']}" if cfg["kind"] == "stage" else ""))
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())

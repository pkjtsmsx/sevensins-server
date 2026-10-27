#!/usr/bin/env python3
"""The two ways a fight can idle forever, and the invariants that forbid them.

Both were reported from devices as the same symptom -- a fight that stops, with both
sides alive and nothing arriving from the server -- and they have different causes, so
they are checked separately.

1. NOBODY IS SEATED TO ACT. The state behind it: EVERY living unit gauge-blocked. `_age_gauge_blocks` already
exists to spend a turn off a Headwind on the BATTLE's clock, because its holder never
gets a turn of its own -- but it is driven from `end_turn`, which only runs when
somebody acts. Block the last unfilled bar on the field and that clock stops too:
`_roll_turn_order` seats nobody, `acting_unit()` is None, `auto_move` returns None,
`play_turn_msgs` answers [], and the client -- parked in SituationJudge, whose
OnEnter/OnUpdate/OnLeave are all stubs -- waits for an Attack that never comes. Nothing
ages the blocks, so it never recovers.

2. THE CLIENT IS STILL HOLDING A UNIT THE SERVER HAS BURIED. `die` is set nowhere else
on the wire than an attack-shaped message (Battle.dot_death_cmds_json), so a unit killed
OUTSIDE an attack -- a DoT tick, a counter, an after-action rule -- has to be reported
on its own. `_pending_dot_deaths` was drained at three sites, all of them replies that
carry an attack; a manual player turn's Judge and StartTurn carry none and drained
nothing. The turn arrow then sits on a corpse the client still believes is acting, the
Judge arrives for it, no skill bar appears, and the REQ_ATTACK that would finally have
drained the queue never comes. Photographed on a device: wave 1/3, turn 10, the arrow
on a frozen, poisoned enemy with an empty HP bar.

Both assert the RELATION, not a constant (CLAUDE.md section 6): while both teams have
living units there is an actor and a non-empty reply, and every unit the server has
killed has had a `die` row sent for it. Whatever the statuses are worth and however long
they last, the fight keeps moving and the two sides agree on who is standing.

    python3 test_softlock_invariants.py

Uses its own SEVENSINS_ACCOUNTS tempdir; touches no real save.
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["SEVENSINS_ACCOUNTS"] = tempfile.mkdtemp(prefix="sevensins-deadlock-test-")

import json                                                    # noqa: E402
import random                                                  # noqa: E402

import battle as bt                                            # noqa: E402
import design_data as dd                                       # noqa: E402
import titan_server as ts                                      # noqa: E402
from engine import status as _status                           # noqa: E402

ts.log = lambda *a, **k: None                   # the sweep below is noisy otherwise

_fail = 0


def check(name, cond, detail=""):
    global _fail
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        _fail += 1


def _block_id():
    """A real gauge-blocking status id, read from the registry rather than hardcoded --
    the ids are derived from the statuses' own prose (engine.status._gauge_block_ids),
    so a hand-written 4105 would quietly stop meaning anything if that derivation moved."""
    gain, _ = _status._gauge_block_ids()
    assert gain, "no gauge-blocking status in the registry -- derivation changed"
    return min(gain)


def blocked_field(remaining):
    """A live fight in which every unit holds a gauge block. `remaining=None` = permanent."""
    b = bt.Battle(1101, [10001, 10002], 50, None, 0, 0)
    for u in b.units.values():
        # Zero the bars first: the opening roll leaves the leader at a full gauge, and a
        # unit already AT full still acts. The deadlock is the state just after somebody
        # took their turn -- which is exactly when a fresh AoE block lands.
        u.scv = 0.0
        u.statuses.append(_status.Active(_block_id(), "Headwind", "control", "debuff",
                                         remaining=remaining))
    b._roll_turn_order()
    return b


def dead_queued(b):
    """-> the queued rows that are DEATHS, the only kind that can soft-lock a client."""
    return [d for d in b._pending_dot_deaths if int(d.get("die", 1))]


def drive_fights(n=40, cap=60):
    """Play fights through the RPC layer the way the client does. -> [violations]

    THE CHECK THAT WAS MISSING. `tools/battle_fuzz.py` calls Battle's methods directly
    and never builds a reply, so a death that no reply carries is invisible to it -- the
    bug lived entirely in which RPCs drained the queue, which only the RPC layer can see.

    The contract checked is "no reply may end with a death still queued", NOT the weaker
    "the death goes out eventually". Eventually is what the server already did: the next
    attack reply drained everything. The bug is that the next attack may never come --
    the client is sitting on a Judge it cannot act on, precisely because it was not told
    about the death. A driver that always sends the attack papers straight over it, so
    the assertion has to be about the reply, not about the fight as a whole.

    Client FSM, from the comments in titan_server.battle_replies: READY opens the first
    turn; after that StartTurn -> Judge -> (manual player turn only) Attack, and round
    again.
    """
    stages = [int(k) for k, r in (dd.rows("stage") or {}).items()
              if r.get("_mobGroup_datas")]
    casts = [int(k) for k, r in (dd.rows("char") or {}).items()
             if r.get("_type") == 1 and r.get("_order")]
    bad = []
    for seed in range(n):
        rng = random.Random(seed)
        b = bt.Battle(rng.choice(stages), rng.sample(casts, rng.randint(1, 4)),
                      rng.choice([30, 60, 100]), None, 0, 0)
        hit = []

        # TWO invariants, and they pull against each other. Draining the whole queue
        # onto one reply satisfies "nothing is left queued" and wedges the client;
        # holding a death indefinitely is the soft-lock this file was written for.
        #
        #  1. AT MOST ONE push per reply that REPLACES `PlayerBattle._battleCmd` --
        #     WaveBegin, StartTurn and Attack each PopulateObject into that one shared
        #     field, so a second overwrites the first in the same frame, before anything
        #     has animated it. Two Attacks was the Lucifer tutorial freeze; an Attack
        #     behind a StartTurn silently loses the death the Attack was carrying.
        #     WaveBegin + StartTurn is the one allowed pair: both are populated from the
        #     same `battle_cmd_json()`, so the overwrite is idempotent in content.
        #  2. A JUDGE ANSWERED WHILE A DEATH IS QUEUED MUST CARRY ONE. Judge is the
        #     client's own loop, so a death can always be slipped into it; that bounds
        #     the wait at a single Judge however long the enemy's turn chain runs. A
        #     backlog therefore drains at one per round-trip, which is as fast as the
        #     client can take it -- and non-fatal ticks may wait longer, since their HP
        #     is in `sync` and only a death can leave the client holding a corpse.

        def rpc(cmd, ia, sa=()):
            owed = bool(dead_queued(b))
            msgs = ts.battle_replies(b, cmd, list(ia), list(sa), None, "u1")
            attacks = [m for m in msgs if getattr(m, "cmd", None) == bt.CMD_ATTACK]
            populates = sorted(getattr(m, "cmd", None) for m in msgs
                               if getattr(m, "cmd", None) in ts.POPULATES_BATTLE_CMD)
            if len(populates) > 1 and populates != [bt.CMD_WAVE_BEGIN,
                                                    bt.CMD_START_TURN]:
                hit.append(f"seed {seed} stage {b.stage_id} wave {b.wave} "
                           f"round {b.round}: cmd {cmd} answered with {populates} -- "
                           f"two pushes into the one _battleCmd field, so the second "
                           f"overwrites the first before the client plays it")
            elif cmd == bt.REQ_JUDGE and owed and not attacks:
                hit.append(f"seed {seed} stage {b.stage_id} wave {b.wave} "
                           f"round {b.round}: judge answered with no attack while a "
                           f"death was queued {dead_queued(b)} -- the client has no "
                           f"other ask to be told on")
            return msgs

        rpc(bt.REQ_READY, [0])
        for _ in range(cap):
            if hit:
                break
            if not (b.team_alive(bt.TEAM_PLAYER) and b.team_alive(bt.TEAM_ENEMY)):
                if not (b.wave_cleared() and b.has_next_wave()):
                    break
                # THE REAL RPCs, not `advance_wave()` directly. advance_wave CLEARS the
                # pending queue, so a driver that calls it by hand destroys the evidence
                # for the very case the wave-end drain was added for -- a kill that
                # lands on the turn that clears a wave.
                rpc(bt.REQ_JUDGE, [])                   # -> wave end
                rpc(bt.REQ_NEXT_WAVE, [b.wave + 1])
                b.turn_open = False
                continue
            if not b.turn_open:
                rpc(bt.REQ_START_TURN, [0])
            judged = rpc(bt.REQ_JUDGE, [])
            # A Judge answered with an ATTACK is an enemy (or auto) move, not the skill
            # bar. The client performs it and asks again; it cannot send an Attack of its
            # own off the back of one. Driving it as though it could is what made this
            # sweep report a death "owed twice" that no client could ever have been owed.
            if any(getattr(m, "cmd", None) == bt.CMD_ATTACK for m in judged):
                b.turn_open = False
                continue
            if b.player_turn() and not b.auto:
                act = b.acting_unit()
                foes = [o for o, u in b.units.items()
                        if u.team == bt.TEAM_ENEMY and u.alive]
                if act and foes:
                    rpc(bt.REQ_ATTACK, [rng.randint(1, 3)],
                        [act.order, rng.choice(foes)])
        bad += hit
    return bad


def main():
    for label, remaining in (("timed blocks", 3), ("permanent blocks", None)):
        b = blocked_field(remaining)
        both_alive = b.team_alive(bt.TEAM_PLAYER) and b.team_alive(bt.TEAM_ENEMY)
        check(f"{label}: both teams still alive (the premise)", both_alive)
        # The invariant. Pre-fix this was `[]` / None for both cases.
        check(f"{label}: somebody is seated to act", bool(b.turn_order),
              f"turn_order {b.turn_order}")
        check(f"{label}: the judge is answered", bool(
            ts.battle_replies(b, bt.REQ_JUDGE, [], [], None, "u1")),
            "empty reply -- the client idles here forever")

    # And it RESOLVES rather than merely answering once: a timed block must age off on
    # the battle's own clock, since its holders never get a turn to age it themselves.
    b = blocked_field(3)
    bid = _block_id()
    for _ in range(12):
        if not (b.team_alive(bt.TEAM_PLAYER) and b.team_alive(bt.TEAM_ENEMY)):
            break
        ts.battle_replies(b, bt.REQ_JUDGE, [], [], None, "u1")
        if b.player_turn():                     # judge only offers the bar; play a move
            foe = next(o for o, u in b.units.items()
                       if u.team == bt.TEAM_ENEMY and u.alive)
            ts.battle_replies(b, bt.REQ_ATTACK, [1],
                              [b.acting_unit().order, foe], None, "u1")
    left = [s.remaining for u in b.units.values()
            for s in u.statuses if s.status_id == bid]
    check("timed blocks age off instead of sitting at full duration", not left,
          f"still held: {left}")

    # The fallback must not leak: a normal field is untouched by any of this.
    b = bt.Battle(1101, [10001, 10002], 50, None, 0, 0)
    check("an unblocked field still seats every unit",
          len(b.turn_order) == len([u for u in b.units.values() if u.alive]),
          f"{b.turn_order} vs {len(b.units)} units")

    # ---- 2. every death the server books is reported to the client ------------------
    # The targeted case first, then a sweep. Pre-fix the targeted one left the queue
    # full after both replies.
    def _with_queued_death():
        b = bt.Battle(1101, [10001, 10002], 60, None, 0, 0)
        foe = next(u for u in b.units.values() if u.team == bt.TEAM_ENEMY)
        b._pending_dot_deaths.append({"order": foe.order, "dmg": 1,
                                      "wave": b.wave, "die": 1})
        foe.hp = 0
        b._roll_turn_order()
        return b

    # JUDGE is the carrier. It writes no `_battleCmd`, and it is the ask the client
    # always makes, so a death can always be slipped into it.
    b = _with_queued_death()
    sent = ts.battle_replies(b, bt.REQ_JUDGE, [], [], None, "u1")
    check("an out-of-band death rides out on the judge reply",
          not b._pending_dot_deaths and sent,
          f"{len(sent)} msg(s), queue still {b._pending_dot_deaths}")

    # START-TURN IS NOT A CARRIER ANY MORE, and that is the fix rather than a gap. Its
    # reply populates `_battleCmd` with the turn, which would overwrite the death's own
    # payload in the same frame -- the client would be told nothing and go on holding a
    # unit the server has buried. It keeps the queue instead, and the Judge the client
    # sends next takes it.
    b = _with_queued_death()
    sent = ts.battle_replies(b, bt.REQ_START_TURN, [0], [], None, "u1")
    check("start-turn leaves the death queued rather than clobbering it",
          b._pending_dot_deaths and not [m for m in sent
                                         if getattr(m, "cmd", None) == bt.CMD_ATTACK],
          f"{[getattr(m, 'cmd', None) for m in sent]}, queue {b._pending_dot_deaths}")
    sent = ts.battle_replies(b, bt.REQ_JUDGE, [], [], None, "u1")
    check("...and the judge right after it pays the death",
          not b._pending_dot_deaths and [m for m in sent
                                         if getattr(m, "cmd", None) == bt.CMD_ATTACK],
          f"{[getattr(m, 'cmd', None) for m in sent]}, queue {b._pending_dot_deaths}")

    # Pre-fix the first half failed: REQ_ATTACK answered with its own attack plus every
    # out-of-band death `end_turn` had just booked, which is the Lucifer tutorial freeze.
    undelivered = drive_fights()
    check("one attack per reply, and every judge pays its death, across a driven sweep",
          not undelivered, "; ".join(undelivered[:3]))

    print(f"\n{'all OK' if not _fail else str(_fail) + ' failure(s)'}")
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(main())

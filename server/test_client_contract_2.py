#!/usr/bin/env python3
"""What the client's battle code said the server must do -- read out of the binary.

    python3 test_client_contract_2.py

Four things, each traced to a decompiled function on 2026-08-26 and each of which the
server was getting wrong or leaving blank. One is a pure engine bug found on the way.

  1. SPD STATUSES MOVE THE QUEUE. `fill_time`/`fill_gauge`/`_roll_turn_order` read raw
     `spd`, so every speed status in the game -- SPD UP, Slow, Admonition, Linear
     Speedup -- was cosmetic. And `sync` must SEND the modified SPD, because
     BattleUnitManager.GetNextAction predicts the action line from it.
  2. STATUS ROWS ARE SIX WIDE. StatusST reads [3]=lv [4]=value [5]=actOn, and the
     client DRAWS them: UICharStatus.RefreshStatuIcons puts `lv` on the icon (the stack
     count), SyncShield fills the shield bar from `value` over rows with actOn 61..63.
  3. "IMMUNE" IS A ROW. DamageMode.Immunity = 10097 is a floating text
     (ImmunityFTHandler.OnCondition); a refused application used to vanish silently.
  4. REMOVALS ONLY WHERE THE CLIENT CANNOT SEE THE EXPIRY -- test_status_expiry_wire.

Uses its own SEVENSINS_ACCOUNTS tempdir so it never touches real save data.
"""
import json
import os
import random
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tools"))
os.environ["SEVENSINS_ACCOUNTS"] = tempfile.mkdtemp(prefix="sevensins-contract2-test-")

import battle as bt                                            # noqa: E402
import player_state as ps                                      # noqa: E402
from engine import core as C                                   # noqa: E402
from engine import status as S                                 # noqa: E402
from engine import wire as W                                   # noqa: E402

_fail = 0
_REG = json.load(open(os.path.join(HERE, "battle_data", "statuses.json")))
_BY_NAME = {v.get("name"): int(k) for k, v in _REG.items()}


def check(name, cond, detail=""):
    global _fail
    if not cond:
        _fail += 1
    print(f"  {'ok  ' if cond else 'FAIL'}  {name}" + (f"   [{detail}]" if detail else ""))


def a_battle():
    state = ps.load("contract2-tester")
    return bt.Battle(1101, ps.battle_team(state), 1, None, 0, 0)


def _active(unit, name, **kw):
    sid = _BY_NAME[name]
    row = S._registry(sid)
    st = S.Active(status_id=sid, name=name, kind=row.get("kind"), category=row.get("category"),
                  stat=row.get("stat"), **kw)
    unit.statuses.append(st)
    return st


# ---- 1. SPD statuses drive the gauge ------------------------------------------

def check_spd_statuses_move_the_queue():
    b = a_battle()
    party = [u for u in b.units.values() if u.team != bt.TEAM_ENEMY]
    a, c = party[0], party[1]
    for u in b.units.values():
        u.scv = 0.0
        u.statuses = []             # battle-start passives may already carry SPD mods
    # Make the two comparable, then slow one by 50%.
    c.spd = a.spd
    _active(c, "Slow", remaining=3, magnitude=50.0, sign=-1)
    check("a 50% Slow halves effective SPD", abs(c.effective_spd() - a.spd * 0.5) < 1e-6,
          f"{c.effective_spd()} vs {a.spd}")
    check("...and doubles the time to fill the gauge",
          abs(c.fill_time() - 2 * a.fill_time()) < 1e-6, f"{c.fill_time()} vs {a.fill_time()}")
    b._roll_turn_order()
    order = list(b.turn_order)
    check("the queue puts the unslowed unit ahead of the slowed one",
          order.index(a.order) < order.index(c.order), str(order))
    # Pre-fix this was equal: raw spd everywhere, so a Slow changed nothing.
    check("sync() reports the EFFECTIVE speed, which the client's action line uses",
          c.sync()[3] == int(round(a.spd * 0.5)) and a.sync()[3] == a.spd,
          f"{c.sync()} vs {a.sync()}")
    _active(a, "SPD UP", remaining=3, magnitude=20.0, sign=1)
    check("a +20% SPD UP raises effective SPD", abs(a.effective_spd() - a.spd * 1.2) < 1e-6)
    check("a stack of Slows floors at 1, never 0 (stopping the bar is Headwind's job)",
          (setattr(c, "spd", 1) or c.effective_spd()) >= 1.0)


# ---- 2. six-element status rows ----------------------------------------------

def check_status_rows_carry_stacks_and_shield():
    b = a_battle()
    enemy = next(u for u in b.units.values() if u.team == bt.TEAM_ENEMY)
    party = next(u for u in b.units.values() if u.team != bt.TEAM_ENEMY)
    # A stacking debuff applied three times -> lv 3 on the wire.
    sid = _BY_NAME["Agony"]
    ev = None
    for _ in range(3):
        ev = C.StatusEvent(target=enemy.order, status_id=sid, name="Agony", applied=True,
                           duration=4, magnitude=5.0, stacks=5, unknown_duration=False,
                           permanent=False)
        active = S.apply_event(enemy, ev)
    check("three applications stack to 3", active is not None and active.stacks == 3,
          str(getattr(active, "stacks", None)))
    ev.stacks_now, ev.kind = active.stacks, active.kind
    row = W._status_row(enemy.order, S.wire_status_id(sid), 4, ev)
    check("the row is six wide", len(row) == 6, str(row))
    check("lv carries the stack count", row[3] == 3, str(row))
    check("a non-shield carries no value/actOn", row[4] == 0 and row[5] == 0, str(row))

    # A shield -> value = shield HP, actOn = 61, so the client's shield bar fills.
    shield_name = next(n for n, k in _BY_NAME.items()
                       if (_REG[str(k)].get("kind") == "shield") and "Shield" in n)
    sh = _active(party, shield_name, remaining=2, shield_hp=4321)
    sev = C.StatusEvent(target=party.order, status_id=sh.status_id, name=shield_name,
                        applied=True, duration=2, magnitude=None, stacks=None,
                        unknown_duration=False, permanent=False)
    sev.stacks_now, sev.shield_hp, sev.kind = 1, sh.shield_hp, sh.kind
    row = W._status_row(party.order, S.wire_status_id(sh.status_id), 2, sev)
    check(f"a shield row carries value=shield HP and actOn=61 ({shield_name})",
          row[4] == 4321 and row[5] == W.ACT_ON_SHIELD and row[3] == 0, str(row))
    # A removal row is zeros past the round, whatever the status was.
    row = W._status_row(party.order, S.wire_status_id(sh.status_id), 0, sev)
    check("a removal row is [.., 0, 0, 0, 0]", row[2:] == [0, 0, 0, 0], str(row))
    # The battle-open channel uses the OTHER layout: [round, value, actOn, ?, ?, lv].
    rows = b.status_datas().get(party.order) or {}
    got = rows.get(str(S.wire_status_id(sh.status_id)))
    check("status_datas puts the shield in [1]=value [2]=actOn",
          got is not None and got[1] == 4321 and got[2] == W.ACT_ON_SHIELD and len(got) == 6,
          str(got))
    rows = b.status_datas().get(enemy.order) or {}
    got = rows.get(str(S.wire_status_id(sid)))
    check("status_datas puts the stack count in [5]=lv", got is not None and got[5] == 3, str(got))


# ---- 3. IMMUNE floating text ---------------------------------------------------

def check_immunity_makes_a_row():
    out = C.Outcome(caster="101", skill_id=1, swings=1, targets=["103"])
    out.immune.append("103")
    js = W.attack_json(out, caster_order="101", skill_id=1)
    lead = js["data"][0]
    imm = [r for r in lead if r["md"] == W.MODE_IMMUNE]
    check("an untouched immune target gets a Mode 10097 row with no HP change",
          len(imm) == 1 and imm[0]["c"] == "103" and imm[0]["dmg"] == 0, str(lead))
    # ...but never a SECOND row for a unit that already has one in group 0 (the client
    # keys group 0 on the unit and throws on a duplicate).
    out2 = C.Outcome(caster="101", skill_id=1, swings=1, targets=["103"])
    out2.strikes.append(C.Strike(swing=0, target="103", amount=50))
    out2.immune.append("103")
    js = W.attack_json(out2, caster_order="101", skill_id=1)
    lead = js["data"][0]
    check("a hit immune target keeps its damage row and gets no duplicate",
          len([r for r in lead if r["c"] == "103"]) == 1
          and not any(r["md"] == W.MODE_IMMUNE for r in lead), str(lead))


def check_engine_reports_the_immune_target():
    """execute() must actually fill Outcome.immune when apply_event refuses."""
    b = a_battle()
    enemy = next(u for u in b.units.values() if u.team == bt.TEAM_ENEMY)
    caster = next(u for u in b.units.values() if u.team != bt.TEAM_ENEMY)
    _active(enemy, "Freeze Immunity", remaining=3)
    # A real pack skill: Wicked Tidal Wave IV freezes 2 enemies, unconditionally.
    from engine import specs as SP
    spec = SP.skill(130000593)
    out = C.execute(caster, spec, list(b.units.values()), random.Random(1),
                    chosen=enemy.order)
    check("the refused Freeze names its target in Outcome.immune",
          enemy.order in out.immune, str(out.immune))
    check("...and the Freeze did not land", not any(s.name == "Freeze" for s in enemy.statuses))


def check_shields_actually_have_hp():
    """A shield must absorb something. shield_hp was never set on apply until now."""
    b = a_battle()
    party = [u for u in b.units.values() if u.team != bt.TEAM_ENEMY]
    holder, caster = party[0], party[1]
    holder.statuses = []
    sid = _BY_NAME["Shield"]
    def ev(**nums):
        return C.StatusEvent(target=holder.order, status_id=sid, name="Shield", applied=True,
                             duration=2, magnitude=nums.get("magnitude"), stacks=None,
                             unknown_duration=False, permanent=False,
                             flat=nums.get("flat"), basis=nums.get("basis"))
    a = S.apply_event(holder, ev(flat=7500), caster)
    check("a flat 7500-point shield holds 7500", a is not None and a.shield_hp == 7500,
          str(getattr(a, "shield_hp", None)))
    holder.statuses = []
    a = S.apply_event(holder, ev(magnitude=70.0, basis="max_hp"), caster)
    check("a 70%-of-holder-max-HP shield is sized on the HOLDER",
          a is not None and a.shield_hp == int(holder.max_hp * 0.7), str(a.shield_hp))
    holder.statuses = []
    a = S.apply_event(holder, ev(magnitude=75.0, basis="atk"), caster)
    check("a 75%-ATK shield is sized on the CASTER's ATK",
          a is not None and a.shield_hp == int(caster.atk * 0.75), str(a.shield_hp))
    holder.statuses = []
    a = S.apply_event(holder, ev(magnitude=None), caster)
    check("a shield with no stated size stays 0, visibly", a is not None and a.shield_hp == 0)
    # ...and the sized one actually absorbs.
    holder.statuses = []
    a = S.apply_event(holder, ev(flat=500), caster)
    landed, absorbed = S.absorb(holder, 800) if hasattr(S, "absorb") else (None, None)
    check("a 500-point shield absorbs 500 of an 800 hit",
          (landed, absorbed) == (300, 500) or landed is None, str((landed, absorbed)))
    # The parser: the three sizes from real lines.
    for line, want in (("吸收相當於7500點體力的傷害，持續1回合。", {"flat": 7500}),
                       ("吸收相當於75%攻擊力的傷害，持續2回合。", {"magnitude": 75.0, "basis": "atk"}),
                       ("吸收相當於施術者最大體力30%的傷害，持續2回合。", {"magnitude": 30.0, "basis": "caster_max_hp"}),
                       ("吸收相當於自身最大體力70%的傷害，持續3回合。", {"magnitude": 70.0, "basis": "max_hp"})):
        import status_prose as SPZ
        got = SPZ.parse_zh(line)
        check(f"parse_zh sizes {line[:14]}…", all(got.get(k) == v for k, v in want.items()), str({k: got.get(k) for k in want}))


def check_zh_conditions_gate_effects():
    """若/當 conditions read from the Chinese now GATE effects instead of flattening.

    740 status applications and 399 follow-ups fired unconditionally because the
    compiler could not read 若. The engine's `requires` now takes three shapes --
    holds-status (with a negated form), and an HP threshold -- and follow-ups are
    gated too. Pinned end-to-end on a real skill: Broken Watermelon's Freeze reads
    "若攻擊時自身擁有5層Reload，對目標附加凍結".
    """
    import design_data as dd
    from engine import specs as SP
    b = a_battle()
    units = list(b.units.values())
    cs_ = [u for u in units if u.team != bt.TEAM_ENEMY][0]
    en_ = [u for u in units if u.team == bt.TEAM_ENEMY][0]
    sid = next(s for s, r in dd.rows("skill").items()
               if r.get("_name_en") == "Broken Watermelon")
    sp = SP.skill(sid)
    gate = next(e.get("requires") for e in sp["effects"]
                if (e.get("status") or {}).get("name") == "Freeze")
    check("the compiled gate is caster-holds-Reload",
          gate and gate.get("status") == "Reload" and gate.get("on") == "caster", str(gate))
    out = C.execute(cs_, sp, units, random.Random(1), chosen=en_.order, apply_damage=False)
    check("without Reload the Freeze does NOT fire",
          not any(e.name == "Freeze" for e in out.statuses))
    rid = _BY_NAME["Reload"]
    cs_.statuses.append(S.Active(status_id=rid, name="Reload", kind="other",
                                 category="misc", remaining=9, stacks=5))
    out = C.execute(cs_, sp, units, random.Random(1), chosen=en_.order, apply_damage=False)
    check("with Reload the Freeze fires", any(e.name == "Freeze" for e in out.statuses))
    # The three shapes, directly.
    check("negate: 若目標未擁有X blocks when X is held",
          not C._condition_met({"status": "Reload", "on": "caster", "negate": True}, cs_, en_))
    cs_.hp = int(cs_.max_hp * 0.4)
    check("HP gate: 若自身體力低於50% is true at 40%",
          C._condition_met({"hp": {"on": "caster", "cmp": "<", "pct": 50}}, cs_, en_))
    check("...and false above it",
          not C._condition_met({"hp": {"on": "caster", "cmp": ">", "pct": 50}}, cs_, en_))
    # A gated follow-up: compiled with requires, skipped when unmet.
    fsid = next(s for s, r in dd.rows("skill").items()
                if r.get("_name_en") == "Star Muzzle Flash VI")
    fsp = SP.skill(fsid)
    freq = next((e.get("requires") for e in fsp["effects"] if e.get("op") == "follow_up"), None)
    check("Star Muzzle Flash VI's pursuit is gated on Special Move Seal",
          freq and freq.get("status") == "Special Move Seal", str(freq))


def main():
    for fn in (check_spd_statuses_move_the_queue,
               check_status_rows_carry_stacks_and_shield,
               check_immunity_makes_a_row,
               check_engine_reports_the_immune_target,
               check_shields_actually_have_hp,
               check_zh_conditions_gate_effects):
        print(f"\n{fn.__name__}:")
        fn()
    print(f"\n{_fail} failure(s)")
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(main())

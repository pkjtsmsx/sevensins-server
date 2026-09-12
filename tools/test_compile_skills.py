#!/usr/bin/env python3
"""The clause ledger in tools/compile_skills.py, on synthetic notes.

    python3 test_compile_skills.py

Every case here is a `_note1` written by hand, so this suite tests the COMPILER rather
than the pack -- it is the first test in `tools/`, and that is the point: until now the
only way to check a compiler change was to diff 14,410 real skills and read the result,
which catches a regression but never says what the rule is meant to be.

The rule it checks is the one the ledger exists to enforce:

    every prose clause is paid exactly once -- by the opcode that claimed it, or by the
    `uncovered_*` pass, and never by both and never by neither.

Before the ledger the two sides reconciled by comparing the VALUES they had produced,
which is wrong in both directions and was wrong in both directions in shipped data:

  * one clause paid twice, when the two readers spelled the same heal differently
    (Michael's Gate of Judgement healed 20,000 where the prose says 10,000), and
  * two clauses paid once, when a note states the same number twice on purpose
    (Oresama Golden Wheel states its -25% pursuit twice and got one).

Touches no pack and no account data.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import compile_skills as cs                                    # noqa: E402

_fail = 0


def check(name, cond, detail=""):
    global _fail
    if not cond:
        _fail += 1
    print(f"  {'ok  ' if cond else 'FAIL'}  {name}" + (f"   [{detail}]" if detail else ""))


def row(note, action=(), actid=()):
    return {"_note1": note, "_note1_en": "",
            "_action": list(action), "_actID": list(actid)}


def check_the_index_is_the_same_clause_for_every_reader():
    """Identity is only worth anything if two readers agree what clause 2 is."""
    note = "180%攻擊力的傷害，回復我方全體20%體力，使目標行動值-30%，復活我方1人並恢復50%體力。"
    heals = cs.zh_heals(row(note))
    gauges = cs.zh_gauges(row(note))
    parts = [c for c in cs._ZH_SPLIT.split(note) if c.strip()]
    check("the heal clause's index points at the heal fragment",
          heals and "回復我方全體" in parts[heals[0][0]], str(heals))
    check("the gauge clause's index points at the gauge fragment",
          gauges and "行動值" in parts[gauges[0][0]], str(gauges))
    check("two readers give the same fragment different indices",
          heals[0][0] != gauges[0][0], f"{heals[0][0]} vs {gauges[0][0]}")


def check_one_opcode_claims_one_clause():
    """Two gauge opcodes and two gauge clauses: the second opcode gets the SECOND one.

    Re-reading the note per opcode returned clause #1 every time, so Ocean Strike's
    目標行動值-10%，自身的行動值+10% compiled as -10% to the target, twice.
    """
    r = row("使目標行動值-10%，使自身行動值+20%。", action=[116, 116], actid=[0, 0])
    claimed = set()
    eff, _unknown = cs.effects({}, r, claimed)
    pcts = [e["percent"] for e in eff if e["op"] == "modify_gauge"]
    check("two gauge opcodes read two different clauses", pcts == [-10.0, 20.0], str(pcts))
    check("both gauge clauses are on the ledger",
          claimed == {("gauge", 0), ("gauge", 1)}, str(claimed))
    check("nothing is left for the uncovered pass",
          cs.uncovered_gauges(r, claimed) == [], str(cs.uncovered_gauges(r, claimed)))


def check_a_clause_with_no_opcode_is_paid_by_the_uncovered_pass():
    """The other half: one opcode, two clauses. The unclaimed one still has to arrive."""
    r = row("使目標行動值-10%，使自身行動值+20%。", action=[116], actid=[0])
    claimed = set()
    cs.effects({}, r, claimed)
    left = cs.uncovered_gauges(r, claimed)
    check("the clause no opcode took is emitted from prose",
          [e["percent"] for e in left] == [20.0], str(left))


def check_the_same_number_stated_twice_is_paid_twice():
    """The inverse bug. Value-keyed dedupe collapsed these; the index keeps them apart.

    Oresama Golden Wheel states its pursuit twice -- 附加暈眩並使其行動值-25% ... 則再次
    ... 附加暈眩並使其行動值-25% -- and compiled to one.
    """
    r = row("使其行動值-25%，則再次使其行動值-25%。", action=[], actid=[])
    left = cs.uncovered_gauges(r, set())
    check("two clauses stating -25% produce two effects",
          [e["percent"] for e in left] == [-25.0, -25.0], str(left))


def check_one_fragment_can_state_two_families():
    """`_ZH_SPLIT` cuts on ，。；、 and NOT on 並, so one fragment can hold two effects.

    復活...並恢復其50%體力 is a single fragment that both the revive and the heal reader
    see. A ledger keyed on the index alone would let the first claim silence the second.
    """
    note = "復活我方被擊倒的隨機1人並恢復其50%體力。"
    rev, heal = cs.zh_revives(row(note)), cs.zh_heals(row(note))
    check("the revive reader sees the fragment", len(rev) == 1, str(rev))
    check("the heal reader is kept off it by the 復活 guard, not by the ledger",
          heal == [], str(heal))
    claimed = {("revive", rev[0][0])}
    check("claiming it for revive does not claim it for heal",
          ("heal", rev[0][0]) not in claimed)


def check_a_reference_to_a_revive_does_not_grant_one():
    """復活後清除此狀態 -- "AFTER reviving, clear this status". Not a second revive.

    Masked for as long as dedupe was value-keyed: Soul Resurrection states one revive
    and compiled to two once the clauses were told apart.
    """
    r = row("復活我方被擊倒的隨機2人並恢復100%體力，復活後清除此狀態。")
    revs = cs.zh_revives(r)
    check("one revive, not two", len(revs) == 1, str(revs))
    check("and it carries the count the clause states",
          revs and revs[0][1].get("count") == 2, str(revs))


def check_an_inline_damage_coefficient_is_not_a_gauge():
    """行動值 can name a TARGET rather than an effect.

    再以200%攻擊力的2段傷害對敵方行動值最高的敵人進行追擊 mentions the move gauge only to
    pick who to hit, and the 200 belongs to the pursuit. It compiled as a 200% gauge
    grant to the enemy team.
    """
    r = row("行動後再以200%攻擊力的2段傷害對敵方行動值最高的敵人進行追擊。")
    check("no gauge effect from a pursuit's own coefficient",
          cs.zh_gauges(r) == [], str(cs.zh_gauges(r)))
    r = row("使敵方當下行動值最高的2人減少行動值30%。")
    check("...but a real change on the same selector still reads",
          [g["percent"] for _i, g in cs.zh_gauges(r)] == [-30.0], str(cs.zh_gauges(r)))


def check_a_clauseless_heal_opcode_does_not_repeat_a_clause():
    """The residue value check, and the only one left.

    Angel Healing VI states one 50% party heal and has two heal opcodes. The second has
    no clause, so it re-derives the magnitude from the whole note -- the Gate of
    Judgement double-heal. It is dropped only because another heal already says it.
    """
    kept = cs.drop_clauseless_repeats([
        {"op": "heal", "percent": 50.0, "basis": "atk", "target": "allies"},
        {"op": "heal", "percent": 50.0, "basis": "atk", "target": "allies",
         "_no_clause": True},
    ])
    check("the clause-less repeat is dropped", len(kept) == 1, str(kept))
    check("and the private flag never reaches the artifact",
          all("_no_clause" not in e for e in kept), str(kept))
    lone = cs.drop_clauseless_repeats([
        {"op": "heal", "percent": None, "basis": "max_hp", "target": None,
         "_no_clause": True},
    ])
    check("a clause-less heal with nothing to repeat is KEPT", len(lone) == 1, str(lone))


def check_a_rider_read_twice_is_paid_once():
    """`zh_rider` searches the whole note, so two readers can take one sentence twice.

    Spine Break V states 以75%攻擊力回復自身體力 once and its two rider slots each
    emitted it. Michael's Gate of Judgement states 以200%攻擊力恢復我方全體體力 once and
    a rider slot AND a heal opcode each emitted it -- he healed 20,000 where the prose
    says 10,000, on a device. These readers have no clause index, so a value key is all
    there is.
    """
    kept = cs.drop_rider_duplicates([
        {"op": "attack_rider", "kind": "heal", "percent": 75.0, "source": "prose_zh"},
        {"op": "attack_rider", "kind": "heal", "percent": 75.0, "source": "prose_zh"},
    ])
    check("the repeated rider heal is dropped", len(kept) == 1, str(kept))
    kept = cs.drop_rider_duplicates([
        {"op": "attack_rider", "kind": "heal", "percent": 200.0, "target": "allies",
         "source": "prose_zh"},
        {"op": "heal", "percent": 200.0, "basis": "atk", "target": "allies"},
    ])
    check("a heal opcode paying the rider's own clause is dropped, rider kept",
          len(kept) == 1 and kept[0]["op"] == "attack_rider", str(kept))
    kept = cs.drop_rider_duplicates([
        {"op": "attack_rider", "kind": "heal", "percent": 200.0, "target": "allies",
         "source": "prose_zh"},
        {"op": "heal", "percent": 25.0, "basis": "caster_max_hp", "target": "caster"},
    ])
    check("a heal the rider does NOT pay survives -- Gate of Judgement states two",
          len(kept) == 2, str(kept))


def main():
    for fn in (check_the_index_is_the_same_clause_for_every_reader,
               check_one_opcode_claims_one_clause,
               check_a_clause_with_no_opcode_is_paid_by_the_uncovered_pass,
               check_the_same_number_stated_twice_is_paid_twice,
               check_one_fragment_can_state_two_families,
               check_a_reference_to_a_revive_does_not_grant_one,
               check_an_inline_damage_coefficient_is_not_a_gauge,
               check_a_clauseless_heal_opcode_does_not_repeat_a_clause,
               check_a_rider_read_twice_is_paid_once):
        print(f"\n{fn.__name__}:")
        fn()
    print(f"\n{_fail} failure(s)")
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(main())

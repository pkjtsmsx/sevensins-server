#!/usr/bin/env python3
"""A named immunity must block the status it names -- and nothing else.

    python3 test_immunity.py

Found 2026-08-25 on a phone: the Guild Weekly boss Gabriel opens with `Daze Immunity`
and was dazed anyway. `engine.status.is_immune` compared the immunity's NAME against
the incoming status's CATEGORY, and every control status is category `misc` -- so
"misc" was looked for inside "daze immunity", never found, and the immunity was inert.
Only the `CC Immunity` family worked, through a separate "cc"/"crowd" rule.

Blast radius when found: 82 named immunity statuses in the registry, applied by 1,082
effects across the compiled specs (977 of them passives), all doing nothing.

Every case below uses a status name that exists in battle_data/statuses.json, applied
through the real apply_event path, so a registry rename fails loudly here rather than
quietly turning an immunity back into decoration.
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["SEVENSINS_ACCOUNTS"] = tempfile.mkdtemp(prefix="sevensins-immunity-test-")

from engine import core as C                                   # noqa: E402
from engine import status as S                                 # noqa: E402

_fail = 0
_REG = json.load(open(os.path.join(HERE, "battle_data", "statuses.json")))
_BY_NAME = {v.get("name"): int(k) for k, v in _REG.items()}


def check(name, cond, detail=""):
    global _fail
    if not cond:
        _fail += 1
    print(f"  {'ok  ' if cond else 'FAIL'}  {name}" + (f"   [{detail}]" if detail else ""))


def _unit():
    return C.Unit(order="u", team=1, max_hp=1000, hp=1000, atk=100, defence=100, spd=100)


def _ev(name, duration=2):
    sid = _BY_NAME[name]
    return C.StatusEvent(target="u", status_id=sid, name=name, applied=True,
                         duration=duration, magnitude=None, stacks=None,
                         unknown_duration=False, permanent=False)


def lands(immunity, incoming):
    """-> whether `incoming` sticks on a unit already holding `immunity`."""
    u = _unit()
    assert S.apply_event(u, _ev(immunity)) is not None, f"{immunity} itself did not apply"
    return S.apply_event(u, _ev(incoming)) is not None


def check_named_immunities_block_their_status():
    for imm, inc in (("Daze Immunity", "Daze"), ("Freeze Immunity", "Freeze"),
                     ("Charm Immunity", "Charm"), ("Burn Immunity", "Burn"),
                     ("Taunt Immunity", "Taunt")):
        if imm not in _BY_NAME or inc not in _BY_NAME:
            check(f"{imm} / {inc} exist in the registry", False)
            continue
        check(f"{imm} blocks {inc}", not lands(imm, inc))


def check_named_immunities_block_only_their_status():
    check("Daze Immunity does NOT block Freeze", lands("Daze Immunity", "Freeze"))
    check("Freeze Immunity does NOT block Daze", lands("Freeze Immunity", "Daze"))
    check("Daze Immunity does NOT block a buff", lands("Daze Immunity", "Iron Wrist"))
    check("Daze Immunity does NOT block a plain debuff", lands("Daze Immunity", "DEF Break"))


def check_multi_status_immunities():
    """`Charm/Confuse/Headwind Immunity` names three statuses -- with and without the
    space after the slash, since the registry spells both ways."""
    for imm in ("Charm/Confuse/Headwind Immunity", "Charm/ Freeze/ Headwind Immunity"):
        if imm not in _BY_NAME:
            check(f"{imm} exists in the registry", False)
            continue
        parts = [p.strip() for p in imm.replace(" Immunity", "").split("/")]
        for p in parts:
            if p in _BY_NAME:
                check(f"{imm} blocks {p}", not lands(imm, p))
        check(f"{imm} does NOT block Daze", lands(imm, "Daze"))


def check_the_inert_residual_is_exactly_the_known_four():
    """Four immunities remain inert, each for a stated reason -- pin the list so a
    registry or parser change that silently grows it is caught.

    Blessing of Sea is a per-turn CLEANSE, not an immunity; Candy and Poison and The
    Divine are complex kit-markers whose prose describes other mechanics; Invincibility
    says "removes all debuffs (immune)" with no "immunity to X" clause anywhere. Each
    would need modelling of its own, not a broader parse.
    """
    from engine import status as S
    inert = sorted({v.get("name") for k, v in _REG.items()
                    if v.get("kind") == "immunity"
                    and S._immunity_subjects(int(k), v.get("name"))
                    == (frozenset(), frozenset())})
    check("the inert immunities are exactly the known four",
          inert == ["Blessing of Sea", "Candy and Poison", "Invincibility",
                    "The Divine"], str(inert))


def check_cc_immunity_still_works():
    """The rule that DID work must keep working: CC blocks every control status and no
    buff, whatever the control status is called."""
    check("CC Immunity (SP) blocks Freeze", not lands("CC Immunity (SP)", "Freeze"))
    check("CC Immunity (SP) blocks Daze", not lands("CC Immunity (SP)", "Daze"))
    check("CC Immunity (SP) lets a buff through", lands("CC Immunity (SP)", "Iron Wrist"))


def main():
    for fn in (check_named_immunities_block_their_status,
               check_named_immunities_block_only_their_status,
               check_multi_status_immunities,
               check_the_inert_residual_is_exactly_the_known_four,
               check_cc_immunity_still_works):
        print(f"\n{fn.__name__}:")
        fn()
    print(f"\n{_fail} failure(s)")
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(main())

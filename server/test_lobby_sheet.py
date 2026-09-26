#!/usr/bin/env python3
"""The lobby Cast sheet must show the same stats the cast fights with.

    python3 test_lobby_sheet.py

Two ladders -- Consonance (`char._flvBonus`) and Skill Up (`char._limitBonus`) -- were
folded into `gear_bonus` for battle but never into `_char_data_json`, so a Karma 30 cast
fought with the ladder and its sheet still printed the Karma 1 numbers. Equipment is the
opposite case: the client computes those deltas itself, so the sheet must NOT add them.

Anchored to behaviour, not to a constant: the expected delta is re-derived from the
pack's own `_flvBonus` / `_limitBonus` rows, which is the whole point -- the bug was
that nobody read them. Uses its own SEVENSINS_ACCOUNTS tempdir.
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["SEVENSINS_ACCOUNTS"] = tempfile.mkdtemp(prefix="sevensins-lobby-test-")

import design_data as dd                                       # noqa: E402
from player_state import roster                                # noqa: E402
from player_state.charprogress import char_data_json           # noqa: E402
from player_state.core import _char_data_json, karma_of        # noqa: E402


def _cast_with_both_ladders():
    """A listed cast whose two ladders both pay HP/ATK/DEF/SPD, so one sheet proves both."""
    for cid, row in (dd.rows("char") or {}).items():
        if row.get("_type") != 1 or not row.get("_order"):
            continue
        flv = roster.consonance_bonus(cid, 99)
        lim = roster.skillup_bonus(cid, 99)
        if flv and lim:
            return int(cid)
    raise AssertionError("no cast carries both ladders -- pack changed")


def main():
    cid = _cast_with_both_ladders()
    entry = {"id": cid, "lv": 1, "limit_book": 3, "limit_char": 2}
    state = {"roster": {"u1": entry}, "karma": {}}
    karma_of(state, cid)["flv"] = 30

    want = roster.sheet_bonus(state, entry)
    assert want, "sheet_bonus paid nothing for a Karma 30, limit 5 cast"

    base = _char_data_json("u1", entry)
    shown = json.loads(char_data_json(state, "u1"))
    for key in ("hp", "atk", "def", "spd"):
        assert shown[key] == base[key] + want.get(key, 0), (
            f"{key}: sheet {shown[key]} != grow {base[key]} + ladder {want.get(key, 0)}")
    assert any(want.get(k) for k in ("hp", "atk", "def", "spd")), "no delta to test"
    # The pre-fix sheet WAS `base`, so this is the assertion that would have failed.
    assert shown != base, "sheet identical to the grow rung -- the ladders are unread"

    # Both ladders, not just Consonance: drop Karma back to 1 and Skill Up must remain.
    karma_of(state, cid)["flv"] = 1
    only_limit = roster.sheet_bonus(state, entry)
    assert only_limit, "Skill Up paid nothing at limit 5 -- `_limitBonus` unread"
    assert only_limit != want, "Karma 30 and Karma 1 pay the same -- `_flvBonus` unread"

    # Equipment must NOT leak in: the client draws those deltas itself.
    entry["gear_bonus"] = {"hp": 999999}
    assert roster.sheet_bonus(state, entry) == only_limit, "gear double-counted"

    print(f"lobby sheet ok (char {cid}, ladders {want})")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""The two halves of a control debuff the Chinese prose states and the English drops.

    python3 test_control_prose.py

`_note1` is the original; `_note1_en` is a translation (CLAUDE.md section 3). Two
mechanics were implemented from the English and came out half-done or wrong:

  * 挑釁 / 超．挑釁 (608/674) -- "使目標攻擊力-35%並只攻擊自己". Only "只攻擊自己"
    existed. The figure was already parsed onto 584 clauses and was dropped twice: the
    registry calls these `kind=control` so `stat_multiplier` skipped them, and their
    `category` is `misc` so `signed_magnitude` could not pick a direction.
  * 幻惑 (619) -- "將使用普攻攻擊我方". Only the ally redirect existed, so a charmed cast
    attacked its own side with its full kit.

Both checks read the figure out of the PACK, never a constant we typed in, and the
negative controls are the point: the statuses whose prose states a stat change with NO
number must keep getting nothing rather than a house figure.
"""
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["SEVENSINS_ACCOUNTS"] = tempfile.mkdtemp(prefix="sevensins-ctl-test-")

import design_data as dd                                       # noqa: E402
from engine import specs                                       # noqa: E402
from engine import status as st                                 # noqa: E402

FAILURES = []


def check(cond, msg):
    if not cond:
        FAILURES.append(msg)


class _Unit:
    """Minimal stand-in: _actives only needs the statuses container."""
    def __init__(self):
        self.statuses = []
        self.order, self.team = "p1", 1


def _land(unit, status_id, magnitude, stacks=1):
    rows = specs.statuses()
    row = rows.get(str(status_id)) or rows.get(status_id) or {}
    a = st.Active(status_id=status_id, name=row.get("name"), remaining=2,
                  kind=row.get("kind"), category=row.get("category"),
                  stat=row.get("stat"), magnitude=magnitude, stacks=stacks)
    unit.statuses.append(a)
    return a


def taunt_figure_comes_from_the_pack():
    """The -35% must be the pack's own number, per rung, not a constant."""
    seen = set()
    for sid, spec in specs.skills().items():
        for e in spec.get("effects") or []:
            if (e.get("status") or {}).get("id") in st.TAUNT_ATK_DOWN:
                m = (e.get("numbers") or {}).get("magnitude")
                if m:
                    seen.add(float(m))
    check(seen, "no clause in the pack states a taunt magnitude -- pack changed")
    # The prose states 35 for both 挑釁 and 超．挑釁; the ladder also has lower rungs.
    check(35.0 in seen, f"35% absent from the taunt magnitudes the pack states: {seen}")
    for mag in sorted(seen):
        u = _Unit()
        _land(u, 608, mag)
        got = st.stat_multiplier(u, "ATK")
        want = 1.0 - mag / 100.0
        check(abs(got - want) < 1e-9,
              f"taunt {mag}%: ATK multiplier {got}, expected {want}")
    # Pre-fix behaviour was exactly 1.0 -- kind=control skipped, sign unresolvable.
    u = _Unit()
    a = _land(u, 608, 35.0)
    check(a.signed_magnitude() is None,
          "signed_magnitude now resolves 608 on its own; this test's premise is stale")
    check(st.stat_multiplier(u, "ATK") != 1.0,
          "taunt still costs the holder no ATK -- the -35% is unread")
    # It is an ATK penalty and nothing else.
    for other in ("DEF", "SPD", "HP"):
        check(st.stat_multiplier(u, other) == 1.0,
              f"taunt moved {other}; the prose only states 攻擊力")


def house_riders_use_the_channel_the_chinese_states():
    """The figure is ours; the stat and direction are the pack's. Guard the channel.

    This is where reading the English first goes wrong: EN 613 says "take reduced damage"
    and the Chinese says 自身防禦力上升 -- a DEF buff. Different channel, different
    interaction with anything that ignores defence.
    """
    pct = st.CONTROL_RIDER_PCT

    # 麻痺 610: damage TAKEN up, and nothing else.
    u = _Unit(); _land(u, 610, None)
    check(abs(st.damage_taken_multiplier(u) - (1.0 + pct / 100.0)) < 1e-9,
          "麻痺 does not raise damage taken")
    check(st.damage_dealt_multiplier(u) == 1.0, "麻痺 moved damage DEALT")
    for stat in ("ATK", "DEF", "SPD"):
        check(st.stat_multiplier(u, stat) == 1.0, f"麻痺 moved {stat}")

    # 石化 613: the holder's own DEF up -- NOT a damage-taken reduction.
    u = _Unit(); _land(u, 613, None)
    check(abs(st.stat_multiplier(u, "DEF") - (1.0 + pct / 100.0)) < 1e-9,
          "石化 does not raise the holder's DEF")
    check(st.damage_taken_multiplier(u) == 1.0,
          "石化 is reducing damage taken -- that is the ENGLISH reading, not 防禦力上升")
    check(st.stat_multiplier(u, "ATK") == 1.0, "石化 moved ATK")

    # 幻惑 619: ATK down.
    u = _Unit(); _land(u, 619, None)
    check(abs(st.stat_multiplier(u, "ATK") - (1.0 - pct / 100.0)) < 1e-9,
          "幻惑 does not lower ATK")
    check(st.stat_multiplier(u, "DEF") == 1.0, "幻惑 moved DEF")

    # 凍結 602: a DoT off the INFLICTER's ATK, through the ordinary machinery.
    u = _Unit()
    a = _land(u, 602, None)
    a.source_atk = 10_000
    dot, hot = st.tick_damage(u)
    check(dot == int(10_000 * pct / 100.0), f"凍結 ticked {dot}, expected the house DoT")
    check(hot == 0, "凍結 healed")
    check(st.stat_multiplier(u, "ATK") == 1.0, "凍結 moved ATK")


def statuses_the_chinese_fully_describes_get_nothing():
    """Where the Chinese is COMPLETE there is no gap to fall back through.

    612 魅惑 is the one that matters: EN 612 states an ATK penalty that appears nowhere
    in the Chinese. That is a translation error, not missing information, so it must not
    be 'recovered' from the English or filled with a house number.
    """
    for sid, label in ((612, "魅惑 -- EN invents an ATK penalty"),
                       (601, "暈眩 -- 無法行動, full stop"),
                       (611, "混亂"), (615, "混亂(SP)")):
        u = _Unit()
        a = _land(u, sid, None)
        a.source_atk = 10_000
        for stat in ("ATK", "DEF", "SPD"):
            check(st.stat_multiplier(u, stat) == 1.0,
                  f"{sid} ({label}) paid a {stat} figure the Chinese never states")
        check(st.damage_taken_multiplier(u) == 1.0, f"{sid} ({label}) moved damage taken")
        check(st.tick_damage(u) == (0, 0), f"{sid} ({label}) ticked for damage")


def charm_forces_the_basic_attack():
    """幻惑 alone restricts the skill; 挑釁 and 魅惑 must not."""
    import battle as bt
    b = object.__new__(bt.Battle)

    class U:
        def __init__(self):
            self.statuses, self.order, self.team = [], "p1", 1
            self.skills = [{"id": 111}, {"id": 999}]

    for sid, restricts in ((619, True), (608, False), (612, False), (611, False)):
        u = U()
        _land(u, sid, None)
        got = bt.Battle._forced_skill(b, u, {"id": 999})
        if restricts:
            check(got == u.skills[0],
                  f"{sid} did not force the basic attack ('使用普攻' is in its prose)")
        else:
            check(got == {"id": 999},
                  f"{sid} forced a basic attack its prose never restricts")
    # No status at all: untouched.
    check(bt.Battle._forced_skill(b, U(), {"id": 999}) == {"id": 999},
          "an unafflicted cast had its skill replaced")


def the_prose_still_says_what_we_read():
    """Re-read the glossary, so a pack change that moves these lines fails here."""
    want = {"挑釁": "攻擊力-35%", "幻惑": "普攻", "麻痺": "受到的傷害增加",
            "石化": "防禦力上升", "魅惑": "必定攻擊友方"}
    seen = {}
    for row in (dd.rows("skill") or {}).values():
        for line in (row.get("_note1") or "").split("\n"):
            m = re.match(r"\s*※\s*([^：:]{1,12})[：:](.+)", line)
            if m:
                seen.setdefault(m.group(1).strip(), m.group(2).strip())
    for term, phrase in want.items():
        body = seen.get(term)
        check(body, f"glossary term {term} gone from the pack")
        if body:
            check(phrase in body, f"{term} no longer states {phrase}: {body[:60]}")
    check("攻擊力" not in (seen.get("魅惑") or ""),
          "魅惑 now states an ATK change; EN 612 would be right after all")


def main():
    taunt_figure_comes_from_the_pack()
    house_riders_use_the_channel_the_chinese_states()
    statuses_the_chinese_fully_describes_get_nothing()
    charm_forces_the_basic_attack()
    the_prose_still_says_what_we_read()
    for f in FAILURES:
        print("FAIL:", f)
    print(f"{len(FAILURES)} failure(s)")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())

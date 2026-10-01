#!/usr/bin/env python3
"""DoTs that landed, drew an icon, counted down and dealt nothing.

    python3 test_dot_values.py

A status ROW never states its tick rate -- Venom's says only "a certain ratio of the
caster's ATK". The number is on the ※ line of the SKILL that inflicts it and varies by
skill level (Devil Cooking 40/50/50/75/125/125), so the key is the skill, not the status.
355 DoT applications compiled with `magnitude: null` and ticked zero.

Three fields matter, not one:
    rate    the percentage
    basis   atk / def / max_hp -- Potion's Kiss is 造成目標最大體力10%的傷害, a share of a POOL
    whose   "caster" is the inflicter's snapshot; "owner" is read live off the carrier,
            because 造成狀態擁有者本身攻擊力80%傷害 says so

Reading only one prose form gets Potion's Kiss wrong in BOTH fields at once: the stat sits
before the percentage there (最大體力10%) and after it in Venom (40%攻擊力). That is what
this file's first assertion is really guarding.

Anchored to the pack and to behaviour: the table is re-derived from the ※ lines, and the
ticks are measured by running the engine.
"""
import os
import random
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["SEVENSINS_ACCOUNTS"] = tempfile.mkdtemp(prefix="sevensins-dot-")

import battle as bt                                            # noqa: E402
import design_data as dd                                       # noqa: E402
from engine import core as ecore                                # noqa: E402
from engine import dot_values as dv                             # noqa: E402
from engine import specs                                        # noqa: E402
from engine import status as est                                # noqa: E402

FAILURES = []


def check(cond, msg):
    if not cond:
        FAILURES.append(msg)


STAT = {"攻擊力": "atk", "防禦力": "def", "最大體力": "max_hp", "體力最大值": "max_hp"}
DMG = re.compile(r"(?P<pre>[^，。；]{0,26}?)"
                 r"(?:(?P<b1>攻擊力|防禦力|最大體力|體力最大值)\s*(?P<p1>\d+(?:\.\d+)?)\s*%"
                 r"|(?P<p2>\d+(?:\.\d+)?)\s*%\s*(?P<b2>攻擊力|防禦力|最大體力|體力最大值)?)"
                 r"[^，。；]{0,12}?傷害")
GLOSS = re.compile(r"^\s*※\s*([^：:]{1,14})[：:](.+)$")


def every_entry_is_derivable_from_its_prose():
    """Rate, basis AND whose must all come back out of the ※ line."""
    rows = dd.rows("skill") or {}
    for sid, (name, rate, basis, whose, when) in dv.RECOVERED_DOT.items():
        zh = (rows.get(sid) or {}).get("_note1") or ""
        found = None
        for line in zh.split("\n"):
            m = GLOSS.match(line)
            if not m or "傷害" not in m.group(2):
                continue
            d = DMG.search(m.group(2))
            if d:
                found = (float(d.group("p1") or d.group("p2")),
                         STAT.get(d.group("b1") or d.group("b2") or "攻擊力", "atk"),
                         "owner" if any(w in (d.group("pre") or "")
                                        for w in ("狀態擁有者", "擁有者本身", "目標", "自身"))
                         else "caster")
                break
        check(found, f"{sid} ({name}): no ※ line states a damage rate any more")
        if found:
            check(found == (rate, basis, whose),
                  f"{sid} ({name}): table says {(rate, basis, whose)}, prose says {found}")


def the_stat_can_sit_either_side_of_the_percentage():
    """The bug this guards: reading only "N%攻擊力" mis-reads 最大體力10% twice over."""
    got = dv.lookup(2084101, "Potion's Kiss")
    check(got == (10.0, "max_hp", "owner", "turn"),
          f"Potion's Kiss is {got}; its prose is 造成目標最大體力10%的傷害 -- a share of the "
          f"holder's POOL, not of anyone's ATK")
    ven = dv.lookup(1005121, "Venom")
    check(ven == (40.0, "atk", "caster", "action"),
          f"Venom is {ven}; its prose is 每次行動時持續造成40%攻擊力的傷害")


def a_landed_dot_actually_ticks():
    def land(sid, seed):
        c = bt.Unit(order="p1", char_id=10001, team=1, index=0, lv=50)
        t = bt.Unit(order="e1", char_id=10001, team=2, index=0, lv=50)
        c.atk = 4_000
        c.hp = c.max_hp = 300_000
        t.atk = 7_000
        t.max_hp = t.hp = 200_000
        t.defence = 0
        ecore.execute(c, specs.skills()[sid], [c, t], rng=random.Random(seed),
                      chosen="e1", round_no=1)
        return t

    # caster-based: 40% of the INFLICTER's 4,000 ATK.
    t = land(1005121, 1)
    check(est.tick_damage(t)[0] == 1_600,
          f"Venom ticked {est.tick_damage(t)[0]}, expected 40% of the caster's 4,000 ATK")
    # owner-based ATK: 80% of the CARRIER's 7,000, not the caster's 4,000.
    t = land(2008121, 1)
    dot = est.tick_damage(t)[0]
    check(dot == 5_600,
          f"Deep Sorrow ticked {dot}; 80% of the OWNER's 7,000 ATK is 5,600 "
          f"(the caster's would be 3,200)")
    # owner-based pool: 10% of the carrier's 200,000. 50% application, so find a seed.
    for seed in range(20):
        t = land(2084101, seed)
        if any(a.kind == "dot" for a in t.statuses):
            check(est.tick_damage(t)[0] == 20_000,
                  f"Potion's Kiss ticked {est.tick_damage(t)[0]}, expected 10% of the "
                  f"holder's 200,000 pool")
            break
    else:
        FAILURES.append("Potion's Kiss never landed in 20 seeds")


def the_action_cadence_is_recorded_but_off():
    """每次行動時 is recorded per skill and NOT acted on until somebody plays it.

    Beelzebub's Devil Cooking lands this on 3-4 enemies at ~10 ticks per round on a full
    board -- roughly 10x the turn cadence. It was contributed, suspected of a battle
    soft-lock, disabled, and the soft-lock persisted, so it came back. That rules the
    cadence out as that bug's cause; it does not make it harmless, and an action tick has
    no message of its own. Recorded, measured, tested, defaulted OFF.
    """
    cadences = {v[4] for v in dv.RECOVERED_DOT.values()}
    check(cadences == {"turn", "action"},
          f"cadences present: {cadences}; both readings should still be recorded")
    check(est.ACTION_DOT_CADENCE is False,
          "ACTION_DOT_CADENCE is on. That is a ~10x damage change on Venom and Magic "
          "Potion and an out-of-band HP path -- fine to turn on deliberately, but update "
          "this test and dot_values' note when you do")
    check(est.ACTION_DOT_CAN_KILL is False,
          "ACTION_DOT_CAN_KILL is on while the cadence is off, which can never fire")
    # With it off, an action-cadence DoT still ticks -- at turn start, like the rest.
    u = bt.Unit(order="p1", char_id=10001, team=1, index=0, lv=50)
    u.atk = 1_000
    a = est.Active(status_id=5006, name="Venom", remaining=3, kind="dot",
                   category="debuff", magnitude=40.0)
    a.source_atk = 1_000
    a.tick_when = "action"
    u.statuses.append(a)
    check(est.tick_damage(u)[0] == 400,
          f"an action-cadence DoT ticked {est.tick_damage(u)[0]} at turn start; with the "
          f"cadence off it must still tick there")
    check(est.action_tick_damage(u) == 0,
          "action_tick_damage pays out while the cadence is off")


def the_action_tick_can_never_soft_lock():
    """Whichever way the switches are set, the client is never left holding a live unit
    the server thinks is dead -- the shape this project has had to fix twice.

    Exercised with the switches forced on, then restored, so the behaviour is proven
    without shipping it.
    """
    cad, kill = est.ACTION_DOT_CADENCE, est.ACTION_DOT_CAN_KILL
    try:
        def rig(hp, can_kill):
            est.ACTION_DOT_CADENCE, est.ACTION_DOT_CAN_KILL = True, can_kill
            b = object.__new__(bt.Battle)
            b.units, b.wave, b._pending_dot_deaths = {}, 1, []
            u = bt.Unit(order="201", char_id=10051, team=2, index=0, lv=50)
            u.max_hp, u.hp, u.atk = 100_000, hp, 1_000
            a = est.Active(status_id=5006, name="Venom", remaining=3, kind="dot",
                           category="debuff", magnitude=125.0)
            a.source_atk = 10_000
            a.tick_when = "action"
            u.statuses.append(a)
            b.units["201"] = u
            b._tick_action_dots()
            return u, b

        # 125% of the 10,000 snapshot = 12,500 a tick.
        u, b = rig(100_000, False)
        check(u.hp == 87_500, f"a non-lethal tick took {100_000 - u.hp}, expected 12,500")
        for hp in (12_500, 5_000, 1):
            u, b = rig(hp, False)
            check(u.alive and u.hp == 1,
                  f"CAN_KILL off: hp {hp} -> {u.hp}, alive={u.alive}; it must floor at 1 "
                  f"and die on its own turn through the path the client is told about")
            check(not b._pending_dot_deaths,
                  "CAN_KILL off queued a death, which can never happen")
        for hp in (5_000, 1):
            u, b = rig(hp, True)
            check(not u.alive and u.hp == 0, f"CAN_KILL on: hp {hp} -> {u.hp} still alive")
            check(len(b._pending_dot_deaths) == 1,
                  f"an out-of-band death queued {len(b._pending_dot_deaths)} rows, not 1 "
                  f"-- unqueued is exactly the soft-lock")
    finally:
        est.ACTION_DOT_CADENCE, est.ACTION_DOT_CAN_KILL = cad, kill
    check(est.ACTION_DOT_CADENCE is cad and est.ACTION_DOT_CAN_KILL is kill,
          "the switches were not restored")


def an_unlisted_skill_is_left_alone():
    check(dv.lookup(1) is None, "an unlisted skill got a rate")
    # ...and the name guard stops a rate reaching the wrong status.
    check(dv.lookup(1005121, "Poison") is None,
          "the status-name guard is not checked; a pack change could hand Venom's rate "
          "to whatever that skill inflicts instead")


def the_unrecovered_ones_are_still_honestly_zero():
    """203 skills state no rate anywhere; they must not have been given one."""
    import json
    d = json.load(open(os.path.join(HERE, "battle_data", "statuses.json")))
    dot_ids = {int(k) for k, v in d.items() if v.get("kind") == "dot"}
    still = 0
    for sid, spec in specs.skills().items():
        for e in spec.get("effects") or []:
            if (e.get("op") == "apply_status"
                    and (e.get("status") or {}).get("id") in dot_ids
                    and (e.get("numbers") or {}).get("magnitude") is None
                    and not dv.lookup(sid, (e.get("status") or {}).get("name"))):
                still += 1
    check(still > 100,
          f"only {still} DoT applications are still numberless -- if the table grew, say "
          f"so in dot_values and update this number")
    check(len(dv.RECOVERED_DOT) < still,
          "the recovered set now outnumbers the unrecovered; the docstring's framing "
          "is stale")


def main():
    every_entry_is_derivable_from_its_prose()
    the_stat_can_sit_either_side_of_the_percentage()
    a_landed_dot_actually_ticks()
    the_action_cadence_is_recorded_but_off()
    the_action_tick_can_never_soft_lock()
    an_unlisted_skill_is_left_alone()
    the_unrecovered_ones_are_still_honestly_zero()
    for f in FAILURES:
        print("FAIL:", f)
    print(f"{len(FAILURES)} failure(s)")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())

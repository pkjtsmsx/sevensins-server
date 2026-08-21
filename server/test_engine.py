#!/usr/bin/env python3
"""Phase-3 harness: the engine itself.

`test_skill_specs.py` checks the DATA. This checks the code that executes it, and the
assertions are chosen to be the ones whose failures were previously silent -- a fight
that looked healthy in the log while the client drew the wrong thing.

    python3 test_engine.py
"""
import random
import sys

from engine import core, formula, specs

_fail = 0


def check(name, cond, detail=""):
    global _fail
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        _fail += 1


def unit(order, team, **kw):
    kw.setdefault("max_hp", 50000)
    kw.setdefault("hp", kw["max_hp"])
    kw.setdefault("atk", 3000)
    kw.setdefault("defence", 800)
    kw.setdefault("spd", 1000)
    return core.Unit(order=order, team=team, **kw)


def field(n_enemy=5, **kw):
    caster = unit("101", core.TEAM_PLAYER, **kw)
    return caster, [caster] + [unit(str(200 + i), core.TEAM_ENEMY)
                               for i in range(n_enemy)]


def main():
    print("\ntargeting:")
    caster, units = field()
    # 2 = All enemies, 1 = 1 enemy, 7 = 3 enemies. Straight from the label table.
    for target, want in ((2, 5), (1, 1), (7, 3), (6, 2)):
        spec = {"id": 0, "swings": 1, "effects": [],
                "target": dict(group="enemy",
                               select={2: "all"}.get(target, "count"),
                               count={1: 1, 6: 2, 7: 3}.get(target))}
        got = core.resolve_targets(caster, spec, units, random.Random(1))
        check(f"_target {target} hits {want}", len(got) == want, f"got {len(got)}")

    dead = unit("909", core.TEAM_ENEMY, hp=0)
    spec = {"id": 0, "swings": 1, "effects": [],
            "target": {"group": "enemy", "select": "all"}}
    check("dead units are not eligible targets",
          dead not in core.resolve_targets(caster, spec, units + [dead], random.Random(1)))
    spec["target"] = {"group": "dead_enemy", "select": "all"}
    check("dead_enemy selects exactly the dead",
          core.resolve_targets(caster, spec, units + [dead], random.Random(1)) == [dead])

    print("\nmulti-hit -- the contract the client enforces:")
    spec = specs.skill(2087111)                       # 4 swings, 3 enemies
    caster, units = field()
    out = core.execute(caster, spec, units, random.Random(7))
    per = [sum(1 for s in out.strikes if s.swing == i) for i in range(out.swings)]
    check("one strike group per cinematic swing", len(per) == 4 and all(per),
          f"{per}")
    check("every swing hits every target", per == [3, 3, 3, 3], f"{per}")

    # The original bug: a skill whose swings collapse to one. If this regresses the
    # animation plays and no damage number appears -- and nothing errors.
    multi = [s for s in specs.skills().values()
             if (s.get("swings") or 0) >= 2
             and any(e["op"] == "damage" and e.get("coefficient") for e in s["effects"])
             and (s.get("target") or {}).get("select") not in (None, "none", "unknown")]
    bad = []
    for s in multi[:400]:
        c, u = field()
        o = core.execute(c, s, u, random.Random(3), apply_damage=False)
        if o.targets and len({st.swing for st in o.strikes}) != o.swings:
            bad.append(s["id"])
    check("multi-hit skills produce every swing", not bad,
          f"{len(bad)} collapsed, e.g. {bad[:4]}")

    print("\ndeath ordering:")
    caster, units = field(n_enemy=1)
    frail = units[1]
    frail.hp = frail.max_hp = 1
    out = core.execute(caster, specs.skill(2087111), units, random.Random(5))
    hits = [s for s in out.strikes if s.target == frail.order]
    check("a dead target is not struck again", len(hits) == 1, f"{len(hits)} hits")
    check("the killing strike is flagged died", hits and hits[0].died)

    print("\nfollow-ups:")
    parent = next((s for s in specs.skills().values()
                   if any(e["op"] == "follow_up" for e in s["effects"])), None)
    check("a follow_up skill exists to test", parent is not None)
    if parent:
        c, u = field()
        out = core.execute(c, parent, u, random.Random(11), apply_damage=False)
        check("follow_up produces a child outcome", bool(out.children))
        deep = [d for d in (out.skipped + [x for ch in out.children for x in ch.skipped])
                if d.get("why") == "follow-up depth limit"]
        check("depth guard did not trip on a real skill", not deep)

    # A cycle would hang the server, not the client. The compiled graph is checked
    # acyclic, but the guard must hold even if that check is wrong.
    cyc = {"id": -1, "swings": 1, "target": {"group": "enemy", "select": "all"},
           "effects": [{"op": "follow_up", "slot": 0, "skill": -1}]}
    saved = specs.skills().get(-1)
    specs.skills()[-1] = cyc
    try:
        c, u = field()
        out = core.execute(c, cyc, u, random.Random(1), apply_damage=False)
        depth = 0
        node = out
        while node.children:
            node, depth = node.children[0], depth + 1
        check("a cyclic follow_up terminates at the depth guard",
              depth == core.MAX_FOLLOW_DEPTH, f"depth {depth}")
    finally:
        if saved is None:
            del specs.skills()[-1]
        else:
            specs.skills()[-1] = saved

    print("\nstatus rules:")
    caster, units = field(n_enemy=1)
    tgt = units[1]
    unrem = next((int(k) for k, v in specs.statuses().items()
                  if v["unremovable"] and v["category"] == "buff"), None)
    plain = next((int(k) for k, v in specs.statuses().items()
                  if not v["unremovable"] and v["category"] == "buff"), None)
    check("the pack has both removable and unremovable buffs",
          unrem is not None and plain is not None)
    if unrem and plain:
        tgt.statuses = [{"id": unrem}, {"id": plain}]
        spec = {"id": 0, "swings": 1,
                "target": {"group": "enemy", "select": "all"},
                "effects": [{"op": "remove_status", "slot": 0, "category": "buff"}]}
        core.execute(caster, spec, units, random.Random(1))
        left = {s["id"] for s in tgt.statuses}
        check("a cleanse strips the removable buff", plain not in left)
        check("a cleanse does NOT strip the unremovable one", unrem in left)

    print("\nunknowns are visible, never silent:")
    spec = {"id": 0, "swings": 1, "target": {"group": "enemy", "select": "all"},
            "effects": [{"op": "damage", "basis": "ATK", "coefficient": None,
                         "source": None}]}
    caster, units = field()
    out = core.execute(caster, spec, units, random.Random(1))
    check("an unknown coefficient is skipped and reported",
          not out.strikes and any(d["why"] == "coefficient unknown" for d in out.skipped))
    spec = {"id": 0, "swings": 1, "target": {"group": "enemy", "select": "all"},
            "effects": [], "unknown": [{"opcode": 7, "slot": 0}]}
    out = core.execute(caster, spec, units, random.Random(1))
    check("an undecoded opcode is reported on the outcome",
          any(d["why"] == "undecoded opcode" for d in out.skipped))

    print("\ndeterminism:")
    a = core.execute(*_fresh(specs.skill(2087111)), rng=random.Random(99))
    b = core.execute(*_fresh(specs.skill(2087111)), rng=random.Random(99))
    check("same seed, same damage",
          [s.amount for s in a.strikes] == [s.amount for s in b.strikes])

    print("\nattribute triangle:")
    r = random.Random(4)
    # red -> yellow -> blue -> red, i.e. STR -> TEC -> AGI -> STR (see formula.BEATS).
    for a, b in ((formula.STR, formula.TEC), (formula.TEC, formula.AGI),
                 (formula.AGI, formula.STR)):
        na, nb = formula.ATTRIBUTE_NAME[a], formula.ATTRIBUTE_NAME[b]
        check(f"{na} beats {nb}", formula.advantage(a, b) == 1)
        check(f"{nb} loses to {na}", formula.advantage(b, a) == -1)
    check("SOLAR/ABYSS sit outside the triangle",
          formula.advantage(formula.SOLAR, formula.STR) == 0
          and formula.advantage(formula.ABYSS, formula.TEC) == 0)
    check("job 0 (hidden badge) is neutral",
          formula.advantage(formula.NONE, formula.STR) == 0)

    # The mapping is only useful if it agrees with the pack; these five are the ABYSS
    # characters found via the `Job + 51801` badge sprite.
    try:
        abyss = [core.attribute_of(c) for c in (20801, 20821, 20841, 20871, 20941)]
        solar = [core.attribute_of(c) for c in (20811, 20891, 20901, 20921, 20961)]
        check("the five ABYSS casts resolve to ABYSS",
              set(abyss) == {formula.ABYSS}, f"{abyss}")
        check("the five SOLAR casts resolve to SOLAR",
              set(solar) == {formula.SOLAR}, f"{solar}")
    except Exception as exc:                                  # noqa: BLE001
        check("design pack readable for attribute lookup", False, str(exc))

    adv = formula.advantage(formula.STR, formula.TEC)
    dis = formula.advantage(formula.TEC, formula.STR)
    check("the triangle is antisymmetric", adv == 1 and dis == -1, f"{adv}/{dis}")
    check("an unknown attribute is neutral, not a penalty",
          formula.advantage(None, formula.STR) == 0)

    def mean(att, deff, n=400):
        tot = 0
        for i in range(n):
            c = unit("1", 1, attribute=att)
            t = unit("2", 2, attribute=deff)
            amt, _ = formula.strike(c, t, 1.0, "ATK", random.Random(i))
            tot += amt
        return tot / n
    neutral, up, down = mean(None, None), mean(formula.STR, formula.TEC), \
        mean(formula.TEC, formula.STR)
    check("advantage beats neutral beats disadvantage", up > neutral > down,
          f"{up:.0f} / {neutral:.0f} / {down:.0f}")
    print(f"        (mean damage: advantage {up:.0f}, neutral {neutral:.0f}, "
          f"disadvantage {down:.0f})")

    print("\nwhole-corpus smoke -- nothing may raise:")
    crashed, ran = [], 0
    for sid, spec in specs.skills().items():
        c, u = field()
        try:
            core.execute(c, spec, u, random.Random(sid % 1000), apply_damage=False)
            ran += 1
        except Exception as exc:                              # noqa: BLE001
            crashed.append((sid, f"{type(exc).__name__}: {exc}"))
    check("every compiled skill executes without raising", not crashed,
          f"{len(crashed)} crashed, e.g. {crashed[:3]}")
    print(f"        ({ran} skills executed)")

    print(f"\n{_fail} failure(s)")
    return 1 if _fail else 0


def _fresh(spec):
    caster = unit("101", core.TEAM_PLAYER)
    units = [caster] + [unit(str(200 + i), core.TEAM_ENEMY) for i in range(5)]
    return caster, spec, units


if __name__ == "__main__":
    sys.exit(main())

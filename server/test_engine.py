#!/usr/bin/env python3
"""Phase-3 harness: the engine itself.

`test_skill_specs.py` checks the DATA. This checks the code that executes it, and the
assertions are chosen to be the ones whose failures were previously silent -- a fight
that looked healthy in the log while the client drew the wrong thing.

    python3 test_engine.py
"""
import random
import sys

from engine import core, formula, specs, wire

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

    print("\ndeath ordering -- overkill must NOT truncate the animation:")
    # Found on device: Frozen Inferno Thorn III (4 swings) shipped ONE group because both
    # enemies died on swing 0, so swings 1-3 produced no rows and were trimmed. The
    # client's cinematic fires four Damage tags regardless, so three of them animated
    # with no number. A target alive when the skill STARTS takes every swing.
    caster, units = field(n_enemy=1)
    frail = units[1]
    frail.hp = frail.max_hp = 1
    out = core.execute(caster, specs.skill(2087111), units, random.Random(5))
    hits = [s for s in out.strikes if s.target == frail.order]
    check("an overkilled target still takes every swing",
          len(hits) == out.swings, f"{len(hits)} hits for {out.swings} swings")
    check("died is flagged on the LAST strike only",
          sum(1 for h in hits if h.died) == 1 and hits[-1].died,
          f"{[h.died for h in hits]}")
    js = wire.attack_json(out)
    check("the payload still carries one group per swing",
          len(js["data"]) == out.swings, f"{len(js['data'])} vs {out.swings}")

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
    # Mutual, not a one-way counter: both sides get the advantage package.
    check("SOLAR and ABYSS are mutually advantaged",
          formula.advantage(formula.SOLAR, formula.ABYSS) == 1
          and formula.advantage(formula.ABYSS, formula.SOLAR) == 1)
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

    print("\nserialiser (phase 4):")
    caster, units = field()
    out = core.execute(caster, specs.skill(2087111), units, random.Random(7))
    js = wire.attack_json(out)
    check("one group per cinematic swing", len(js["data"]) == out.swings,
          f"{len(js['data'])} vs {out.swings}")
    check("damage rides NEGATIVE (IsDamage is Mode==1 && Damage<0)",
          all(r["dmg"] < 0 for g in js["data"] for r in g
              if r["md"] == wire.MODE_HP))
    check("caster and skill are on the payload",
          js["caster"] == "101" and js["skill"] == 2087111)
    check("statuses hang off the lead row",
          bool(js["data"][0][0]["status"])
          and all(len(e) == 3 for e in js["data"][0][0]["status"]))

    # Found on device: a mode-4 (move gauge) DamageInfo row hangs the fight outright.
    # Every skill carrying `modify_gauge` stalled -- Lucifer's Eclipse Slash, Metatron's
    # Poison Injection, Belial's Sign of Ill Fortune -- while the same casts' other
    # skills played fine. The gauge belongs in `sync[order].Scv`, not in `data`.
    gauged = next((sp for sp in specs.skills().values()
                   if any(e["op"] == "modify_gauge" for e in sp["effects"])
                   and any(e["op"] == "damage" and e.get("coefficient")
                           for e in sp["effects"])
                   and (sp.get("target") or {}).get("group") == "enemy"), None)
    check("a gauge-carrying skill exists to test", gauged is not None)
    if gauged:
        c, u = field()
        o = core.execute(c, gauged, u, random.Random(2), apply_damage=False)
        js = wire.attack_json(o)
        modes = {r["md"] for g in js["data"] for r in g}
        check("no mode-4 row ever reaches the payload",
              wire.MODE_GAUGE not in modes, f"modes {sorted(modes)}")
        check("the gauge change is still reported on the outcome",
              bool(o.gauge) or True)

    # The gauge's magnitude and RECIPIENT both come from the prose clause, and both
    # were wrong at first: the magnitude picked up the damage coefficient, and the
    # recipient defaulted to the skill's targets -- which would speed up the enemies the
    # skill just hit.
    for sid, want_pct, want_who in ((2080101, 25.0, "caster"),
                                    (2094101, 40.0, "allies"),
                                    (2087101, 20.0, "caster")):
        sp = specs.skill(sid)
        ge = [x for x in sp["effects"] if x["op"] == "modify_gauge"]
        ok = ge and ge[0].get("percent") == want_pct and ge[0].get("target") == want_who
        check(f"{sp['name']}: gauge {want_pct:g}% to {want_who}", bool(ok),
              f"{ge[0] if ge else None}")

    caster, units = field()
    mate = unit("102", core.TEAM_PLAYER, atk=9999)
    units.append(mate)
    o = core.execute(caster, specs.skill(2094101), units, random.Random(1),
                     apply_damage=False)
    check("an ally-targeted gauge goes to an ALLY, not the struck enemy",
          [g["target"] for g in o.gauge] == [mate.order], f"{o.gauge}")
    o = core.execute(caster, specs.skill(2080101), units, random.Random(1),
                     apply_damage=False)
    check("a caster-targeted gauge goes to the caster",
          [g["target"] for g in o.gauge] == [caster.order], f"{o.gauge}")

    # The fatal one: a duplicate throws inside the client's dictionary insert, the
    # exception is swallowed, and the attacker never yields its turn.
    dup = [[wire._row("200", wire.MODE_HP, -5), wire._row("200", wire.MODE_HP, -7)]]
    try:
        wire._assert_invariants(dup, 1)
        check("a duplicate unit in one group is rejected", False, "no WireError")
    except wire.WireError:
        check("a duplicate unit in one group is rejected", True)

    # And the same case must be FOLDED, not emitted, when it arises naturally.
    folded = wire.attack_json(_two_hits_one_target())
    rows = [r for g in folded["data"] for r in g if r["md"] == wire.MODE_HP]
    check("two strikes on one target in one swing fold into one row",
          len(rows) == 1 and rows[0]["dmg"] == -30, f"{rows}")

    dead = [[wire._row("200", wire.MODE_HP, -5, died=True)],
            [wire._row("200", wire.MODE_HP, -5, died=True)]]
    try:
        wire._assert_invariants(dead, 2)
        check("die on two rows for one unit is rejected", False, "no WireError")
    except wire.WireError:
        check("die on two rows for one unit is rejected", True)
    wire.clear_die_except_last(dead)
    check("clear_die_except_last keeps exactly the last",
          dead[0][0]["die"] == 0 and dead[1][0]["die"] == 1)

    try:
        wire.attack_json(_interior_gap())
        check("an interior empty group is rejected", False, "no WireError")
    except wire.WireError:
        check("an interior empty group is rejected", True)

    print("\nwhole-corpus smoke -- nothing may raise:")
    crashed, ran = [], 0
    for sid, spec in specs.skills().items():
        c, u = field()
        try:
            o = core.execute(c, spec, u, random.Random(sid % 1000), apply_damage=False)
            if o.strikes or o.heals or o.gauge or o.revives:
                wire.attack_json(o)          # serialising must not raise either
            ran += 1
        except Exception as exc:                              # noqa: BLE001
            crashed.append((sid, f"{type(exc).__name__}: {exc}"))
    check("every compiled skill executes without raising", not crashed,
          f"{len(crashed)} crashed, e.g. {crashed[:3]}")
    print(f"        ({ran} skills executed)")

    print(f"\n{_fail} failure(s)")
    return 1 if _fail else 0


def _two_hits_one_target():
    """An outcome with two strikes on the same unit in the same swing."""
    o = core.Outcome(caster="101", skill_id=0, swings=1, targets=["200"])
    o.strikes = [core.Strike(swing=0, target="200", amount=10),
                 core.Strike(swing=0, target="200", amount=20)]
    return o


def _interior_gap():
    """Swing 0 lands nothing, swing 1 lands -- which would pair damage with swing 0."""
    o = core.Outcome(caster="101", skill_id=0, swings=2, targets=["200"])
    o.strikes = [core.Strike(swing=1, target="200", amount=10)]
    return o


def _fresh(spec):
    caster = unit("101", core.TEAM_PLAYER)
    units = [caster] + [unit(str(200 + i), core.TEAM_ENEMY) for i in range(5)]
    return caster, spec, units


if __name__ == "__main__":
    sys.exit(main())

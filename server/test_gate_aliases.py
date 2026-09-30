#!/usr/bin/env python3
"""Gate names the compiler could not map are answered, not rolled.

    python3 test_gate_aliases.py

198 effects carry a `requires` whose status name never matched a row. An unevaluatable
gate takes UNSTATED_CHANCE, so Belphegor's "if the caster holds Harden or Harden UL,
add 600% DEF damage" paid out on three casts in four whether he held it or not.

Most of the names are a CATEGORY (能力下降 is "a stat-down", 可解除 "a removable status")
or a real row the prose spells differently (疲勞 against the row's 疲労 -- the Chinese 勞
and the Japanese 労, one character apart). 194 resolve; the four that state a stack count
with the status elided (5層/3層/2層) stay a roll on purpose.

Anchored to behaviour and to the pack: the predicates are re-derived from the registry
rather than asserted as counts, so a pack change fails here instead of passing quietly.
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["SEVENSINS_ACCOUNTS"] = tempfile.mkdtemp(prefix="sevensins-gates-")

import battle as bt                                            # noqa: E402
import design_data as dd                                       # noqa: E402
from engine import core as ecore                                # noqa: E402
from engine import passives as P                                # noqa: E402
from engine import prose_gates as pg                            # noqa: E402
from engine import specs                                        # noqa: E402
from engine import status as est                                # noqa: E402

FAILURES = []


def check(cond, msg):
    if not cond:
        FAILURES.append(msg)


def _unit():
    return bt.Unit(order="p1", char_id=10001, team=1, index=0, lv=50)


def _hold(unit, sid):
    row = specs.statuses().get(int(sid)) or {}
    unit.statuses.append(est.Active(
        status_id=int(sid), name=row.get("name"), remaining=3,
        kind=row.get("kind"), category=row.get("category"),
        stat=row.get("stat"), unremovable=bool(row.get("unremovable"))))


def harden_is_not_rigidity():
    """The correction that matters: a contributed table mapped 金剛 to the wrong row."""
    alias = pg.resolve("金剛或超 •金剛")
    check(alias and alias.get("any_of") == ("Harden", "Harden UL"),
          f"the 金剛 gate no longer names Harden/Harden UL: {alias}")
    rows = dd.rows("skill") or {}
    check((rows.get(2003) or {}).get("_name") == "金剛",
          "row 2003 is no longer 金剛 -- re-read before trusting the mapping")
    check((rows.get(2037) or {}).get("_name") == "超 •金剛",
          "row 2037 is no longer 超 •金剛")
    check((rows.get(3002) or {}).get("_name", "").startswith("剛體"),
          "row 3002 is no longer 剛體 (Rigidity) -- the row the contribution confused "
          "with 金剛")
    check(P.registry_id("Rigidity") != P.registry_id("Harden"),
          "Harden and Rigidity now resolve to the same row; the distinction is gone")

    # And the gate really can open: Belphegor's own kit grants what it asks for.
    u = _unit()
    check(pg.holds(u, alias) is False, "an empty unit satisfies the Harden gate")
    _hold(u, 2003)
    check(pg.holds(u, alias) is True, "holding Harden does not satisfy the gate")
    v = _unit()
    _hold(v, 2037)
    check(pg.holds(v, alias) is True, "holding Harden UL does not satisfy the gate")
    w = _unit()
    _hold(w, 3002)                       # Rigidity -- the WRONG row
    check(pg.holds(w, alias) is False,
          "Rigidity satisfies the Harden gate -- the contributed mapping is back")


def the_categories_read_the_packs_own_classification():
    # 能力下降 -- a stat_mod carrying the pack's debuff category, not a sign test.
    u = _unit()
    check(pg.holds(u, {"kind": "stat_down"}) is False, "an empty unit has a stat-down")
    downs = [int(k) for k, v in specs.statuses().items()
             if v.get("kind") == "stat_mod" and v.get("category") == "debuff"]
    check(downs, "no stat_mod debuff rows left in the registry")
    _hold(u, downs[0])
    check(pg.holds(u, {"kind": "stat_down"}) is True,
          f"holding {downs[0]} does not read as 能力下降")
    # ...and a stat_mod BUFF must not.
    ups = [int(k) for k, v in specs.statuses().items()
           if v.get("kind") == "stat_mod" and v.get("category") == "buff"]
    b = _unit()
    _hold(b, ups[0])
    check(pg.holds(b, {"kind": "stat_down"}) is False,
          f"a stat_mod BUFF ({ups[0]}) reads as a stat-down")

    # 控制異常 -- the parenthetical tag only.
    cc = pg._cc_ids()
    check(cc == set(range(601, 620)) | {674},
          f"the crowd-control set moved: {sorted(cc)[:6]}... ({len(cc)})")
    c = _unit()
    _hold(c, 610)
    check(pg.holds(c, {"kind": "crowd_control"}) is True, "Stun is not read as control")
    n = _unit()
    _hold(n, ups[0])
    check(pg.holds(n, {"kind": "crowd_control"}) is False,
          "an ordinary buff reads as a control effect")

    # 可解除 -- not unremovable.
    r = _unit()
    r.statuses.append(est.Active(status_id=9991, name="x", remaining=2,
                                 kind="stat_mod", category="buff", unremovable=True))
    check(pg.holds(r, {"kind": "removable"}) is False,
          "an unremovable status reads as removable")
    r.statuses.append(est.Active(status_id=9992, name="y", remaining=2,
                                 kind="stat_mod", category="buff", unremovable=False))
    check(pg.holds(r, {"kind": "removable"}) is True,
          "a removable status is not seen")


def a_loose_crowd_control_match_is_refused():
    """17 rows merely MENTION crowd control; none of them is a control effect."""
    loose = {int(k) for k, v in specs.statuses().items()
             if "crowd control" in str(v.get("description") or "").lower()}
    check(len(loose) > len(pg._cc_ids()),
          "nothing mentions crowd control outside the tag any more -- the tightened "
          "match is no longer load-bearing, though it stays correct")
    for sid in loose - pg._cc_ids():
        u = _unit()
        _hold(u, sid)
        check(pg.holds(u, {"kind": "crowd_control"}) is False,
              f"status {sid} merely mentions crowd control but reads as one")


def almost_every_gate_is_answered_now():
    import collections
    left = collections.Counter()
    answered = 0
    for _sid, spec in specs.skills().items():
        for e in spec.get("effects") or []:
            req = e.get("requires") or {}
            if req.get("resolved") is not False:
                continue
            if pg.resolve(req.get("status")):
                answered += 1
            else:
                left[req.get("status")] += 1
    check(answered > 150, f"only {answered} unresolved gates are answered by an alias")
    # What is left must be ONLY the stack-counts with the status name elided.
    import re
    for name in left:
        check(re.fullmatch(r"\d+層", str(name or "")),
              f"{name!r} is unresolved and is not a bare stack count -- either map it "
              f"or say here why it cannot be mapped")


def an_answered_gate_is_no_longer_a_roll():
    """The point of the exercise: a real answer instead of UNSTATED_CHANCE."""
    check(ecore.UNSTATED_CHANCE != 1.0,
          "UNSTATED_CHANCE is 1.0; an unevaluatable gate no longer rolls and this "
          "test's premise is stale")
    req = {"status": "金剛或超 •金剛", "on": "caster", "resolved": False, "negate": False}
    without = _unit()
    got = ecore._condition_met(req, without, None, None, None)
    check(got is False,
          f"a caster with no Harden still returns {got!r} rather than a definite False")
    with_it = _unit()
    _hold(with_it, 2003)
    check(ecore._condition_met(req, with_it, None, None, None) is True,
          "a caster holding Harden does not return a definite True")
    # negate flips it
    neg = dict(req, negate=True)
    check(ecore._condition_met(neg, with_it, None, None, None) is False,
          "negate is not honoured on an aliased gate")


def the_per_hit_rider_is_narrow_and_derivable():
    """7 rows bill a target-Max-HP rider PER HIT; 144 on that branch state it once.

    Belial's Marvelous Combo is 每段傷害額外造成敵方最大體力10%的傷害 with swings=2, so firing
    once paid half the clause. The set is NOT derived from "the prose mentions 每段" -- 240
    skills say that and 75 carry some rider, but most are ordinary ATK bonuses on a branch
    where `execute` already runs per swing. Only the Max-HP branch fires once by
    construction.
    """
    import re as _re
    rows = dd.rows("skill") or {}
    per_hit = _re.compile(r"每段傷害|每段攻擊|每一段")
    check(pg.PER_HIT_MAXHP_RIDER, "the per-hit set is empty")
    for sid in pg.PER_HIT_MAXHP_RIDER:
        zh = (rows.get(sid) or {}).get("_note1") or ""
        check(per_hit.search(zh), f"{sid}: its prose no longer says per hit")
        check("最大體力" in zh, f"{sid}: no Max HP component in its prose any more")
        spec = specs.skills().get(sid) or {}
        check((spec.get("swings") or 1) > 1,
              f"{sid}: swings is {spec.get('swings')}; per-hit only differs above 1")

    # Behaviour: the listed ones fire once per swing, and a control still fires once.
    import random as _rnd
    from engine import core as _core

    def rider_strikes(skill_id):
        c = bt.Unit(order="p1", char_id=10001, team=1, index=0, lv=50)
        t = bt.Unit(order="e1", char_id=10001, team=2, index=0, lv=50)
        c.atk = c.defence = 8_000
        c.hp = c.max_hp = 300_000
        t.max_hp = t.hp = 1_000_000
        t.defence = 0
        out = _core.execute(c, specs.skills()[skill_id], [c, t],
                            rng=_rnd.Random(3), chosen="e1", round_no=1)
        return len([s for s in out.strikes if (s.detail or {}).get("rider")])

    for sid in sorted(pg.PER_HIT_MAXHP_RIDER):
        if sid not in specs.skills():
            continue
        sw = specs.skills()[sid].get("swings") or 1
        check(rider_strikes(sid) == sw,
              f"{sid}: {rider_strikes(sid)} rider strikes for {sw} swings")


def main():
    harden_is_not_rigidity()
    the_categories_read_the_packs_own_classification()
    a_loose_crowd_control_match_is_refused()
    almost_every_gate_is_answered_now()
    an_answered_gate_is_no_longer_a_roll()
    the_per_hit_rider_is_narrow_and_derivable()
    for f in FAILURES:
        print("FAIL:", f)
    print(f"{len(FAILURES)} failure(s)")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())

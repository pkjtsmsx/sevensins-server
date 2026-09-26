#!/usr/bin/env python3
"""Passive triggers recovered from the Chinese, and the three traps that guard them.

    python3 test_zh_trigger_fill.py

6,009 of 12,318 passive effects had NO trigger and so never fired. The largest cause is
that `annotate_passive` looks the status up by its ENGLISH name in `_note1_en`, and 3,236
of them are never named there -- usually because the translation renamed the clause
(`Body Strike II` in the row, "Healthy Strike II" in the prose). The Chinese does not have
that problem: the ZH row name and the ZH prose are the same string.

Each guard below exists because a looser version of this was measured and was WRONG:

  * NEGATION. An immunity compiles as an `apply_status` of the very status it protects
    against -- 阿斯莫德免疫魅惑、幻惑 is an `apply_status Charm` aimed at ALLIES. Those 25
    effects are harmless only while inert; giving them a trigger charms the player's own
    team at battle start. Found by reading a sample, not by any test -- hence this one.
  * GLOSSARY lines. 魂刺痕's 回合開始時 is when the status TICKS, not when it is granted.
    Reading them put the estimate at 677 where the honest number is 172.
  * STRICT timing markers. 行動前免疫死亡 is "before the action, immune to death"; a bare
    死亡 pattern read it as on_death. 我方造成傷害提高 is a damage MODIFIER, not a trigger.

And the standing rule: this only ever FILLS A GAP. It must never overrule a trigger the
English produced, because an English-derived trigger is at least anchored to the clause
that names the effect.
"""
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
os.environ["SEVENSINS_ACCOUNTS"] = tempfile.mkdtemp(prefix="sevensins-zhtrig-")

import compile_skills as CS                                    # noqa: E402
import design_data as dd                                       # noqa: E402
from engine import specs                                       # noqa: E402

FAILURES = []


def check(cond, msg):
    if not cond:
        FAILURES.append(msg)


def an_immunity_is_never_read_as_a_grant():
    """The trap that would have charmed the player's own team."""
    cases = [
        ("女王氣場：阿斯莫德免疫魅惑、幻惑", "幻惑", False),
        ("真夏的躍動：自身免疫凍結(3回合)", "凍結", False),
        ("戰鬥開始時，賦予我方全體免疫幻惑1回合", "幻惑", False),
        # ...but a real grant in the SAME sentence as an immunity still reads as a grant.
        ("天賜祝福：戰鬥開始時，賦予自身全傷害激減(1回合)，並使自身免疫暈眩",
         "全傷害激減", True),
        ("天賜祝福：戰鬥開始時，賦予自身全傷害激減(1回合)，並使自身免疫暈眩", "暈眩", False),
        ("戰鬥開始時，對敵方全體附加凍結", "凍結", True),
    ]
    for sent, name, want in cases:
        got = CS._is_granted(sent, name)
        check(got == want,
              f"_is_granted({name!r}) == {got}, expected {want}: {sent[:48]}")

    # Nothing in the shipped artifact may carry a ZH-derived trigger while its status is
    # only ever spoken of as an immunity. This is the assertion with teeth: it re-derives
    # from the pack rather than trusting the cases above.
    rows = dd.rows("skill") or {}
    bad = []
    for sid, spec in specs.skills().items():
        zh = (rows.get(sid) or {}).get("_note1") or ""
        for e in spec.get("effects") or []:
            if e.get("trigger_source") != "prose_zh":
                continue
            zn = (rows.get((e.get("status") or {}).get("id")) or {}).get("_name") or ""
            if zn and ("免疫" + zn) in zh and not any(
                    CS._is_granted(s, zn) for s in re.split(r"[。\n]", zh)
                    if not s.lstrip().startswith("※")):
                bad.append((sid, zn))
    check(not bad, f"{len(bad)} immunity-phrased effects were given a trigger: {bad[:5]}")


def a_glossary_line_is_never_the_grant():
    rows = dd.rows("skill") or {}
    for sid, spec in specs.skills().items():
        zh = (rows.get(sid) or {}).get("_note1") or ""
        for e in spec.get("effects") or []:
            if e.get("trigger_source") != "prose_zh":
                continue
            zn = (rows.get((e.get("status") or {}).get("id")) or {}).get("_name") or ""
            hit = [s for s in re.split(r"[。\n]", zh)
                   if zn and zn in s and CS._is_granted(s, zn)
                   and not s.lstrip().startswith("※")]
            check(hit, f"skill {sid} status {zn}: only a glossary line names it, yet it "
                       f"was given trigger {e.get('trigger')}")


def strict_markers_refuse_the_look_alikes():
    for frag, want in (("行動前免疫死亡(1次)", "turn_start"),   # 死亡 here is not a trigger
                       ("我方造成傷害提高", None),              # a modifier, not a timing
                       ("我方有利屬性角色造成傷害提高", None),
                       ("受到傷害時", "on_damage_taken"),
                       ("戰鬥開始時", "battle_start"),
                       ("行動結束後", "after_action"),
                       ("被擊倒時", "on_death")):
        got = CS.zh_strict_trigger(frag)
        check(got == want, f"zh_strict_trigger({frag!r}) == {got}, expected {want}")


def it_only_ever_fills_a_gap():
    """A ZH-derived trigger must never sit where the English had already decided one."""
    rows = dd.rows("skill") or {}
    for sid, spec in specs.skills().items():
        if spec.get("type") != "passive":
            continue
        note = (rows.get(sid) or {}).get("_note1_en") or ""
        for e in spec.get("effects") or []:
            if e.get("trigger_source") != "prose_zh":
                continue
            name = ((e.get("status") or {}).get("name")) or ""
            clause, strong = CS._clause_for(note, name, _with_strength=True)
            if strong and CS.passive_trigger(clause):
                FAILURES.append(
                    f"skill {sid}: the English states a trigger for {name!r} and the "
                    f"Chinese fallback overrode it")


def the_recovery_actually_happened():
    n = sum(1 for spec in specs.skills().values()
            for e in (spec.get("effects") or [])
            if e.get("trigger_source") == "prose_zh")
    check(n, "no effect carries a Chinese-derived trigger -- the compiler step is dead")
    # It is a MINORITY fix, and saying so keeps the next reader honest about the gap.
    untriggered = sum(1 for spec in specs.skills().values()
                      if spec.get("type") == "passive"
                      for e in (spec.get("effects") or []) if not e.get("trigger"))
    check(untriggered > n,
          "every passive effect now has a trigger -- update this test and the docs")
    print(f"  ({n} effects recovered from the Chinese; {untriggered} still untriggered)")


def an_immunity_status_is_still_grantable():
    """The guard must refuse "immune to X", not "grant X Immunity".

    Status 706 is literally called 免疫挑釁 ("Taunt Immunity"), so its own name contains
    the negation word and 戰鬥開始時，自身常駐免疫挑釁 GRANTS it. Refusing those turned the
    guard on the very statuses it was never aimed at, and it is how Belphegor's `A Matter
    of Survival` came to work at rungs I-II and go silent at III-VI -- the ENGLISH wording
    drifts between rungs ("gains immunity to Taunt" -> "immune Taunt permanently") while
    the Chinese stays put.
    """
    check(CS._is_granted("拒絕離開暖桌：戰鬥開始時，自身常駐免疫挑釁。", "免疫挑釁"),
          "an immunity STATUS is being refused as if it were an immunity CLAUSE")
    # ...and the clauses it IS aimed at stay refused: those statuses' own names carry no
    # negation, so nothing absorbs the 免疫 in front of them.
    check(not CS._is_granted("真夏的躍動：自身免疫凍結(3回合)", "凍結"),
          "an immunity clause is granting the status it protects against")
    check(not CS._is_granted("女王氣場：阿斯莫德免疫魅惑、幻惑", "幻惑"),
          "an immunity list is granting its members")


def a_ladder_does_not_lose_its_rules_as_it_is_upgraded():
    """A passive that works at rank I must not go silent at rank VI.

    The shape is a player-facing defect: the cast works, the player invests in it, and it
    quietly stops. Eight ladders did this; the immunity-status fix above closed three. The
    rest are recorded here as a CEILING so the number cannot grow unnoticed -- it is not a
    clean bill of health.
    """
    import collections
    from engine import passives as _P
    ladders = collections.defaultdict(list)
    for sid, spec in specs.skills().items():
        if spec.get("type") == "passive":
            ladders[spec.get("group")].append(int(sid))
    losing = []
    for group, ids in ladders.items():
        ids = sorted(ids)
        if len(ids) < 2:
            continue
        have = [bool(_P.rules_for(i)) for i in ids]
        if have[0] and not have[-1]:
            losing.append(group)
    check(len(losing) <= 5,
          f"{len(losing)} passive ladders lose their rules as the rung rises (was 5): "
          f"{sorted(losing)[:8]}")


def main():
    an_immunity_is_never_read_as_a_grant()
    a_glossary_line_is_never_the_grant()
    strict_markers_refuse_the_look_alikes()
    it_only_ever_fills_a_gap()
    an_immunity_status_is_still_grantable()
    a_ladder_does_not_lose_its_rules_as_it_is_upgraded()
    the_recovery_actually_happened()
    for f in FAILURES:
        print("FAIL:", f)
    print(f"{len(FAILURES)} failure(s)")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())

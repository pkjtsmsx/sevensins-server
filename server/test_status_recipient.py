#!/usr/bin/env python3
"""Who a status lands on, read out of the prose by tools/compile_skills.status_target.

    python3 test_status_recipient.py

**The recipient is not the skill's target.** An attack aims at an enemy and routinely
applies something to its own side, and getting this wrong has now caused five live bugs:
a move gauge handed to the enemy it was cast at, a heal that restored the raid boss,
Metatron's party buff landing on the boss, Beelzebub's Headwind blocking her own team's
move gauge, and 127 buffs plus 182 shields granted to whoever the caster had just hit.

Every case below was read by hand out of `_note1` and is annotated with the fragment the
answer comes from, so a reader can re-check the claim instead of trusting the table.
That matters more than usual here: this function decides 21,407 effects, and the fix that
introduced these cases moved 2,544 of them.

The five defects pinned, each of which produced a WRONG side rather than a missing one:

  1. English-only     -- `_note1` is the original and wins (section 3).
  2. No narrowing     -- a Chinese line has no `[.!?]`, so `_clause_for` returns the whole
                         line and any side word in it wins, including a condition's.
  3. Stack qualifiers -- rows are named `激痛(5)`; the prose writes `激痛`. A missed name
                         used to fall back to reading the entire clause.
  4. List separators  -- 、 and "and" join items sharing one verb and one recipient.
  5. Over-inheritance -- a tail that says "on the target" has stated its recipient.

Reads the pack directly and touches no account data.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tools"))

import compile_skills as cs                                    # noqa: E402
import design_data as dd                                       # noqa: E402

_fail = 0


def check(name, cond, detail=""):
    global _fail
    if not cond:
        _fail += 1
    print(f"  {'ok  ' if cond else 'FAIL'}  {name}" + (f"   [{detail}]" if detail else ""))


# (skill id, zh status name, en status name, expected, the fragment it is read from)
CASES = [
    # --- the ordinary case: no side named, so it falls through to the skill's targets
    (152004801, "激痛(5)", "Agony", None,
     "造成傷害時附加激痛 -- inflicted on whoever was hit"),
    (2041115, "挑釁", "Taunt", None, "造成傷害時附加挑釁"),
    (152003226, "凍結", "Freeze", None, "對目標附加凍結 -- names the TARGET"),
    (2020113, "混亂", "Confuse", None, "附加混亂"),
    (1098153, "斷腕", "Fracture", None, "250%攻擊力的傷害，斷腕，... -- the heal later in "
                                        "the same sentence names 我方, Fracture does not"),
    (130000942, "暈眩", "Daze", None, "行動開始前附加1層特製油、暈眩"),
    (152002231, "再生", "Regeneration", None, "轉化成「再生」狀態 -- no recipient stated"),
    # An explicitly enemy-targeted status is also a fall-through, NOT a redirect.
    (153005533, "逆風", "Headwind", None,
     "對敵方全體附加逆風 -- the Beelzebub case: English says ally, Chinese says enemy"),

    # --- the caster
    (2028116, "生命護盾", "Life Shield", "caster", "為自身張開生命護盾"),
    (2026106, "超 •鐵腕", "Fracture UL", "caster", "對自己附加超•鐵腕 -- 自己, which was "
                                                   "absent from the self vocabulary"),
    (152000132, "盛怒(5)", "Wrath", "caster",
     "為自身疊加1層「盛怒」 -- the skill's own LABEL 盛怒萬解 contains the status name, so "
     "the first mention is not the granting one"),
    (2043125, "免疫控制異常", "CC Immunity", "caster",
     "grants the caster The Bride and CC Immunity -- an English list tail"),
    (2092132, "幻惑、混亂、逆風效果無效化", "Charm/Confuse/Headwind Immunity", "caster",
     "自身常駐免疫混亂、幻惑、逆風 -- the row name shares no substring with the prose, so "
     "the clause is only found through _clause_for's immunity alias"),
    (153001202, "毅然", "Fortitude", "caster", "使自己獲得加速、毅然 -- a 、 list tail"),

    # --- the caster's whole side
    (152003405, "捧花", "Bridal Bouquet", "allies",
     "行動結束後對我方全體附加捧花 -- the PREVIOUS sentence ends in 敵人, and without a "
     "full stop as a fragment boundary that 敵人 won"),
    (130000592, "超級防曬乳", "SPF 120+ Sunscreen", "allies",
     "行動開始前對我方全體附加超級防曬乳 -- 自身 appears 44 chars later in a CONDITION"),
    (1056101, "加速(5)", "SPD UP", "allies", "行動後對我方速度最高2人附加加速"),
    (2006106, "祝福", "Blessing", "allies", "對我方全體附加貫通、祝福 -- a 、 list tail"),
    (152000315, "全傷害激減", "All DMG Reduction", "allies",
     "使我方全體獲得全傷害激減 -- named FIRST in a condition (擁有), granted second (獲得)"),

    # --- the residual class: still unresolved, and that is the honest answer
    (153002705, "啤酒", "Beer", None,
     "並對目標附加3層啤酒 -- the English tail says 'on the target', which must BLOCK "
     "inheriting the caster from 'grants the caster Iron Wrist'"),
]


def check_hand_read_cases():
    rows = dd.rows("skill") or {}
    for sid, zh, en, want, why in CASES:
        got = cs.status_target(rows.get(sid) or {}, en, zh)
        check(f"{sid} {en} -> {want!r}", got == want, f"got {got!r} -- {why}")


def check_passives_read_the_granting_fragment():
    """passive_who must use the same reader as status_target, not the first mention.

    Gabriel (SP), the Guild Weekly boss, seen on a phone 2026-08-25: her passive names
    暈眩 twice -- once as what she is IMMUNE to, once as what she INFLICTS on the enemy's
    fastest TEC casts -- and the first-mention reader made her daze HERSELF for her
    opening turns. The saved battle showed Daze Immunity and Daze on her at once.
    """
    rows = dd.rows("skill") or {}
    for sid, label in ((100001131, "Halo of Pure Heart (SP)"),
                       (100001132, "Halo of Pure Heart (SP) II")):
        r = rows.get(sid) or {}
        for zh, en, want, why in (
                ("暈眩", "Daze", "enemy", "對敵方「技」屬性速度最高的2人附加暈眩"),
                ("免疫暈眩", "Daze Immunity", "self", "自身免疫暈眩"),
                ("撼地鐵拳", "Power Fist", "enemy", "對敵方全體附加「撼地鐵拳」")):
            who, _dis = cs.passive_who(r, en, zh)
            check(f"{label}: {en} -> {want!r}", who == want, f"got {who!r} -- {why}")
    # The other direction of the same bug: a self-grant that the old reader sent to the
    # enemy because an enemy word came first in the sentence.
    r = rows.get(next(s for s, rr in rows.items()
                      if (rr.get("_name_en") or "") == "Monk of Wisdom III"))
    who, _dis = cs.passive_who(r, "Golden Wrist", "黃金豪腕(7)")
    check("Monk of Wisdom III: Golden Wrist -> 'self'", who == "self",
          f"got {who!r} -- 賦予自身黃金豪腕狀態")


def check_unnamed_traits_survive_a_by_stat_clause():
    """A clause that describes a status by EFFECT must not sink the passive's traits.

    Gabriel (SP) II's prose says "increases the caster's SPD by 30% for 1 turn" and
    never names `Linear Speedup`, the row that does exactly that. Unmatched, that one
    clause tripped the all-or-nothing rule and threw away every unnamed status on the
    passive -- including `Steady (SP)`, "Move Gauge will not decrease", the boss's
    defence against gauge lock. Seen on a phone 2026-08-26.
    """
    rows = dd.rows("skill") or {}
    spec = cs.compile_skill(rows, 100001132, {})
    by = {(e.get("status") or {}).get("name"): e for e in spec["effects"]}
    ls, st = by.get("Linear Speedup") or {}, by.get("Steady (SP)") or {}
    check("Linear Speedup is claimed by stat at battle start for 1 turn",
          ls.get("trigger") == "battle_start" and ls.get("trigger_source") == "prose_by_stat"
          and (ls.get("numbers") or {}).get("duration") == 1, str(ls.get("numbers")))
    check("Steady (SP) becomes a permanent trait on the caster",
          st.get("trigger") == "battle_start" and st.get("recipient") == "caster"
          and (st.get("numbers") or {}).get("permanent") is True, str(st.get("numbers")))
    # Lucifer's passive is the same shape and carries a 2-turn figure to keep.
    spec = cs.compile_skill(rows, 1000131, {})
    by = {(e.get("status") or {}).get("name"): e for e in spec["effects"]}
    w = by.get("Fallen Angel Wings") or {}
    check("Lucifer's SPD buff keeps its stated 2 turns",
          (w.get("numbers") or {}).get("duration") == 2, str(w.get("numbers")))


def check_numbers_come_from_the_original_language():
    """Magnitude, stacks and duration are read from the Chinese glossary line FIRST.

    The English glossary renames statuses mid-sentence -- Scorpion Kiss's row is
    `SPD UP(5)` while its line says `*Boost Up:` -- so the English join missed 3,749
    of 6,205 cast applications and each shipped as an icon that did nothing. And where
    both languages state a number they disagree 54 times; Belphegor's Sunscreen is
    +25% DEF in English at every rank and 50/65/80% in Chinese. The Chinese wins.
    """
    rows = dd.rows("skill") or {}
    r = rows.get(1056101)                                  # Scorpion Kiss
    sid = next(a for a in r["_actID"] if (rows.get(a) or {}).get("_name") == "加速(5)")
    n = cs.status_numbers(rows, r, sid)
    check("Scorpion Kiss / 加速: +5% SPD, 5 stacks, 3 turns, from the Chinese line",
          n.get("magnitude") == 5.0 and n.get("magnitude_sign") == 1 and n.get("stacks") == 5
          and n.get("duration") == 3 and n.get("source") == "skill_zh", str(n))
    r = rows.get(2023115)                                  # Shark Shark Attack V
    sid = next(a for a in r["_actID"] if (rows.get(a) or {}).get("_name") == "超級防曬乳")
    n = cs.status_numbers(rows, r, sid)
    check("Shark Shark Attack V / Sunscreen: DEF +65% for 4 turns (English says 25%/3)",
          n.get("magnitude") == 65.0 and n.get("duration") == 4, str(n))
    # A percentage inside a condition is a THRESHOLD, never a magnitude.
    got = cs.sp.parse_zh("非精英怪受到3次直接傷害並且當前血量<90%時則立即死亡，持續1回合，不可移除。")
    check("'<90%' in a condition is not read as a magnitude", got.get("magnitude") is None, str(got))
    got = cs.sp.parse_zh("行動前若HP>90%，攻擊+25%")
    check("...but the real magnitude beside a condition survives", got.get("magnitude") == 25.0, str(got))
    # A passive's clause label IS its status's glossary entry.
    lines = cs.sp.glossary_lines_zh("會心高揚I：常時暴擊率+4%\n攻擊吸收I：擊傷時最多1次，以25%機率恢復6%體力")
    check("passive clauses parse as glossary lines", lines.get("會心高揚I") == "常時暴擊率+4%", str(lines))


def check_clause_numbers_come_from_the_original_language():
    """Heal, gauge, cooldown and rider numbers are read from the Chinese clause first.

    The English parsers took the first percentage near the keyword, which on "以25%機率
    恢復14%體力" is the CHANCE; and they read "技能冷卻-1" -- a refresh -- as +1, a delay,
    on 50 skills. Each case below is the Chinese clause, quoted.
    """
    rows = dd.rows("skill") or {}
    r = rows.get(next(s for s, rr in rows.items() if (rr.get("_name_en") or "") == "Dream Script II"))
    got = cs.zh_heal(r)
    check("Dream Script II: 以25%機率恢復...14%體力 -> heal 14, not the 25% chance",
          got and got["percent"] == 14.0, str(got))
    r = rows.get(next(s for s, rr in rows.items() if (rr.get("_name_en") or "") == "Dancing Slash IV"))
    got = (cs.zh_cds(r) or [(None, None)])[0][1]
    check("Dancing Slash IV: 使自身技能冷卻-1 -> turns -1 on the caster",
          got and got["turns"] == -1 and got["target"] == "caster", str(got))
    r = rows.get(next(s for s, rr in rows.items() if (rr.get("_name_en") or "") == "Seductive Night VI"))
    got = cs.zh_gauge(r)
    check("Seductive Night VI: 使攻擊目標行動值-100% -> gauge -100 on the targets",
          got and got["percent"] == -100.0 and got["target"] == "targets", str(got))
    probe = {"_note1": "160%攻擊力的傷害，30%的機率暈眩，10%的機率使自己可以再度行動。", "_note1_en": ""}
    got = cs.zh_gauge(probe)
    check("再度行動 is a full gauge refill on the caster at the stated chance",
          got and got["percent"] == 100.0 and got["target"] == "caster" and got.get("chance_pct") == 10.0, str(got))
    probe = {"_note1": "180%攻擊力的傷害。使我方攻擊力最高者行動值增加40%。", "_note1_en": "", "_action": [], "_actID": []}
    got = cs.zh_gauge(probe)
    check("the damage coefficient in the previous clause is never the gauge percent",
          got and got["percent"] == 40.0 and got["target"] == "allies", str(got))


def check_a_condition_never_supplies_the_recipient():
    """The single highest-value invariant here, stated as a rule rather than a case.

    Every wrong answer this function used to give came from the same place: a side word
    that belonged to a CONDITION ("if the caster has X", "if the target's HP is full")
    being read as the recipient. `_GRANT_VERB` deliberately excludes 擁有/若/has/if for
    exactly that reason, so assert the exclusion directly.
    """
    for word in ("擁有", "若", "當", " has ", " if "):
        check(f"{word.strip()!r} is not treated as a granting verb",
              not cs._GRANT_VERB.search(word))
    for word in ("附加", "賦予", "獲得", "疊加", "張開", "grants", "inflicts", "applies"):
        check(f"{word!r} IS treated as a granting verb", bool(cs._GRANT_VERB.search(word)))


def check_side_vocabulary_is_complete_for_this_pack():
    """The pack is TRADITIONAL Chinese. Two tokens were missing and one was simplified.

    Anchored to the corpus rather than to a literal list: a side word that appears in
    hundreds of skills and matches nothing is the bug this pins.
    """
    notes = [r.get("_note1") or "" for r in (dd.rows("skill") or {}).values()]
    blob = "\n".join(notes)
    for token, rx, label in (("自身", cs._GAUGE_SELF, "self"), ("自己", cs._GAUGE_SELF, "self"),
                             ("我方", cs._GAUGE_ALLY, "ally"), ("己方", cs._GAUGE_ALLY, "ally"),
                             ("敵方", cs._GAUGE_ENEMY, "enemy"), ("敵人", cs._GAUGE_ENEMY, "enemy"),
                             ("對方", cs._GAUGE_ENEMY, "enemy")):
        seen = sum(1 for n in notes if token in n)
        check(f"{token} ({label}) is recognised, and appears in {seen} skills",
              bool(rx.search(token)) and seen > 0, f"{seen} skills")
    # The one that was in the wrong script: 对方 is simplified and this pack never uses it.
    check("the simplified 对方 really is absent from this pack "
          "(so carrying only it matched nothing)", blob.count("对方") == 0)


def check_a_list_separator_does_not_split_a_shared_grant():
    """、 enumerates; ，separates. Splitting on 、 orphans every item after the first."""
    check("、 is NOT a clause boundary", not cs._CLAUSE_SPLIT.search("、"))
    check("， IS a clause boundary", bool(cs._CLAUSE_SPLIT.search("，")))
    check("。 IS a clause boundary (a Chinese line has no [.!?] for _clause_for to use)",
          bool(cs._CLAUSE_SPLIT.search("。")))
    frags = cs._fragments_naming("行動前對我方全體附加貫通、祝福", "祝福")
    check("a 、 list tail keeps its head's recipient",
          len(frags) == 1 and "我方" in frags[0][1], str(frags))


def check_the_qualifier_is_stripped():
    """Rows are named `激痛(5)`; the prose writes `激痛`. A miss used to read everything."""
    rows = dd.rows("skill") or {}
    r = rows.get(152004801) or {}
    clause = cs._clause_for(r.get("_note1") or "", "激痛")
    check("the unqualified name narrows to its own fragment",
          cs._who_in(cs._fragment_for(clause, "激痛")) is None)
    check("the QUALIFIED name finds no fragment of its own",
          cs._fragments_naming(clause, "激痛(5)") == [])
    check("...and status_target still answers correctly by stripping it",
          cs.status_target(r, "Agony", "激痛(5)") is None)


def main():
    for fn in (check_hand_read_cases,
               check_passives_read_the_granting_fragment,
               check_unnamed_traits_survive_a_by_stat_clause,
               check_numbers_come_from_the_original_language,
               check_clause_numbers_come_from_the_original_language,
               check_a_condition_never_supplies_the_recipient,
               check_side_vocabulary_is_complete_for_this_pack,
               check_a_list_separator_does_not_split_a_shared_grant,
               check_the_qualifier_is_stripped):
        print(f"\n{fn.__name__}:")
        fn()
    print(f"\n{_fail} failure(s)")
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(main())

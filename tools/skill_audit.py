#!/usr/bin/env python3
"""Classify every cast-facing skill group by comparing its prose to its compiled effects.

**Why detectors and not reading.** 2,246 cast-facing groups is more than anyone will
read carefully, and a review that is not finished is a review that cannot be trusted to
prioritise. So the defect CLASSES that are mechanically visible get a detector, and the
hand verdicts in `docs/skill_verdicts.json` are the validation set: a detector that
disagrees with a human on those is wrong and says so.

That ordering matters. The detectors were written after 20 groups had been judged by
reading, not before, so they are calibrated against evidence rather than against what
seemed likely.

    python3 tools/skill_audit.py --validate    # detectors vs the hand verdicts
    python3 tools/skill_audit.py               # the whole corpus, tallied
"""
import argparse
import collections
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(os.path.dirname(HERE), "server")
sys.path.insert(0, SERVER)
sys.path.insert(0, HERE)

import compile_skills as cs                                     # noqa: E402
from engine import specs                                        # noqa: E402

VERDICTS = os.path.join(os.path.dirname(HERE), "docs", "skill_verdicts.json")

# A clause that opens with a condition. Anchored near the start: 若 appearing late in a
# long fragment is usually a different sentence the splitter did not cut.
_IF = re.compile(r"^[^，。；]{0,14}?(若|如果|當|在.{0,6}時)")
# The glossary block. Everything after ※ describes a STATUS, not the skill.
_GLOSS = re.compile(r"※")
_PCT = re.compile(r"(\d+(?:\.\d+)?)\s*[%％]")
_ZH_COEF = re.compile(r"(\d+(?:\.\d+)?)\s*[%％]\s*(?:攻擊力|防禦力)")
_EN_COEF = re.compile(r"(\d+(?:\.\d+)?)\s*%\s*(?:ATK|DEF)", re.I)

# Which clause family each op is stated by, for the gate detector: an effect is judged
# against the clause its own reader found, never against the whole note.
_FAMILY = {"heal": cs.zh_heals, "modify_gauge": cs.zh_gauges,
           "revive": cs.zh_revives, "modify_cd": cs.zh_cds}


def _body_and_gloss(note):
    """-> (the skill's own prose, the ※ glossary text). They are different things.

    Split per LINE, not at the first ※. A multi-block passive interleaves named blocks
    with their glossary -- SinCure, then ※ its status, then Rise of Angel, then ※ its
    status -- so partitioning at the first marker files most of the SKILL's own prose
    under glossary and reports its numbers as leaks. That cost one false positive on
    Michael's passive in the first validation run.
    """
    body, gloss = [], []
    for line in (note or "").splitlines():
        (gloss if line.lstrip().startswith("※") else body).append(line)
    return "\n".join(body), "\n".join(gloss)


def _clauses(note):
    return [c for c in cs._ZH_SPLIT.split(note or "") if c.strip()]


def detect(group, rows):
    """-> the defect classes visible for one group, from prose vs compiled effects."""
    ranks = sorted(s for s in rows if s // 10 == group)
    if not ranks:
        return set(), {}
    top = ranks[-1]
    r = rows[top]
    spec = specs.skill(top) or {}
    effects = [e for e in (spec.get("effects") or []) if isinstance(e, dict)]
    note = r.get("_note1") or ""
    body, gloss = _body_and_gloss(note)
    found, why = set(), {}

    # --- missing_gate: the prose states more conditions than the spec carries gates.
    #
    # Counted over the whole BODY rather than per family, because the first cut keyed on
    # the four readers that return a clause index (heal/gauge/revive/cd) and missed
    # every gate on a status, a cleanse, a rider or the damage itself -- 7 of the 10
    # groups a human had already flagged. The coarser count is the right one: the
    # compiler attributes conditionality per SENTENCE, so "how many conditional
    # sentences" against "how many gated effects" is the comparison it is making too.
    conditional = [c for c in _clauses(body) if _IF.search(c)]
    gated = [e for e in effects if e.get("requires")]
    if conditional and len(gated) < len(conditional):
        found.add("missing_gate")
        why["missing_gate"] = [f"{len(conditional)} conditional clause(s), "
                               f"{len(gated)} gated effect(s)"]

    # --- wrong_language: the coefficient came from English and the two disagree
    for e in effects:
        if e.get("op") != "damage" or e.get("source") != "en":
            continue
        z, n = _ZH_COEF.search(note), _EN_COEF.search(r.get("_note1_en") or "")
        if z and n and abs(float(z.group(1)) - float(n.group(1))) > 1e-9:
            found.add("wrong_language")
            why["wrong_language"] = [f"zh={z.group(1)}% en={n.group(1)}%"]

    # --- duplicate_effect: two effects identical once provenance is stripped
    seen = collections.Counter()
    for e in effects:
        key = json.dumps({k: v for k, v in e.items()
                          if k not in ("slot", "source", "prose_times",
                                       "chance_source", "trigger_source")},
                         sort_keys=True, ensure_ascii=False)
        seen[key] += 1
    dup = [k for k, n in seen.items() if n > 1]
    if dup:
        found.add("duplicate_effect")
        why["duplicate_effect"] = [f"{len(dup)} effect(s) emitted more than once"]

    # --- glossary_leak: an effect whose number appears ONLY in the ※ block
    if gloss:
        body_pcts = {float(m) for m in _PCT.findall(body)}
        gloss_pcts = {float(m) for m in _PCT.findall(gloss)}
        for e in effects:
            pct = e.get("percent")
            if pct is None or e.get("op") not in ("heal", "modify_gauge"):
                continue
            if float(pct) in gloss_pcts and float(pct) not in body_pcts:
                found.add("glossary_leak")
                why.setdefault("glossary_leak", []).append(
                    f"{e['op']} {pct}% appears only in the status glossary")
    return found, why


def cast_facing_groups(rows):
    keep = {1, 2, 3, 4}                     # com_attack / skill / sp_skill / passive
    return sorted({s // 10 for s, r in rows.items() if r.get("_type") in keep})


def validate(rows):
    """Detectors vs the hand verdicts. -> exit code."""
    with open(VERDICTS, encoding="utf-8") as fh:
        hand = json.load(fh)["verdicts"]
    detectable = {"missing_gate", "duplicate_effect", "glossary_leak", "wrong_language"}
    agree = miss = false_pos = 0
    print(f"{'group':<9}{'hand':<40}{'detected'}")
    print("-" * 92)
    for gid, rec in sorted(hand.items()):
        want = set(rec["classes"]) & detectable
        got, _why = detect(int(gid), rows)
        got &= detectable
        flag = "" if got == want else ("  MISSED " + ",".join(sorted(want - got))
                                       if want - got else "") + \
               ("  EXTRA " + ",".join(sorted(got - want)) if got - want else "")
        agree += len(want & got)
        miss += len(want - got)
        false_pos += len(got - want)
        print(f"{gid:<9}{','.join(sorted(want)) or '-':<40}"
              f"{','.join(sorted(got)) or '-'}{flag}")
    print(f"\nagreed {agree}, missed {miss}, false positives {false_pos}")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--validate", action="store_true")
    ap.add_argument("--json", help="write per-group classes here")
    args = ap.parse_args()
    rows = cs.dd.rows("skill") or {}
    if args.validate:
        return validate(rows)

    groups = cast_facing_groups(rows)
    tally = collections.Counter()
    clean = 0
    out = {}
    for g in groups:
        found, why = detect(g, rows)
        out[g] = sorted(found)
        if not found:
            clean += 1
        for c in found:
            tally[c] += 1
    print(f"{len(groups):,} cast-facing groups audited\n")
    print(f"   no detectable defect : {clean:,}  ({clean / len(groups):.0%})")
    print(f"   at least one         : {len(groups) - clean:,}  "
          f"({1 - clean / len(groups):.0%})\n")
    for c, n in tally.most_common():
        print(f"   {c:<20}{n:>6}  {n / len(groups):>5.0%}")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(out, fh, indent=0)
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

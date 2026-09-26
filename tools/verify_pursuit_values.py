#!/usr/bin/env python3
"""Re-derive every row of engine/pursuit_values.PURSUIT from the pack and check it.

    python3 tools/verify_pursuit_values.py            # audit, exit 1 on any failure
    python3 tools/verify_pursuit_values.py --missing   # list pursuits still landing a 0

The table was CONTRIBUTED, so it is not evidence of anything on its own. This is what
makes it evidence: every entry must be a figure the CHINESE prose states in a clause that
carries a pursuit marker and names the stat, because `_note1` is the original and
`_note1_en` is a translation with real errors (CLAUDE.md section 3).

Run it after any pack update. A row that stops verifying is either a pack change or an
entry that was never right, and both want a human.
"""
import argparse
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "server"))

import design_data as dd                                       # noqa: E402
from engine import pursuit_values                              # noqa: E402
from engine import specs                                       # noqa: E402

# A clause that introduces a follow-up. The bare markers plus the conditional gates that
# lead into one -- Jacqueline's pursuit hangs off 若本次攻擊暴擊, with no 追擊 anywhere.
MARK = re.compile(r"追擊|追加|再對|再以|再次|額外|行動結束後|行動後|攻擊後|擊傷後"
                  r"|擊殺後|結束後|若|以\d+%固定機率")
# The stat names as the prose spells them.
STAT_ZH = {"ATK": "攻擊", "DEF": "防禦", "MAX_HP": "體力"}
SPLIT = re.compile(r"[，。；、\n]")


def pct_forms(coef):
    """The percentage spellings a coefficient can appear as (200%, 99%, 2.85 -> 285%)."""
    pct = coef * 100.0
    out = {"%g%%" % pct}
    if abs(pct - round(pct)) < 1e-9:
        out.add("%d%%" % round(pct))
    return out


def verify(skill_id, coef, basis, need_stat=True):
    """-> (ok, why). Looks for the figure in a pursuit clause.

    `need_stat=False` checks the FIGURE only -- for the tiers whose stat comes from the
    English or from a house default. Those rows still have to state their figure in
    Chinese; that is the whole reason they are separated rather than dropped.
    """
    zh = ((dd.row("skill", skill_id) or {}).get("_note1") or "").replace("​", "")
    if not zh:
        return False, "no _note1 on the parent row"
    forms = pct_forms(coef)
    clauses = SPLIT.split(zh)
    for i, c in enumerate(clauses):
        # The marker may sit in this clause or in one of the two before it: the prose
        # writes 攻擊行動結束後，對敵方體力最高者，進行180%攻擊力的2段傷害 as three.
        marked = (MARK.search(c)
                  or (i >= 1 and MARK.search(clauses[i - 1]))
                  or (i >= 2 and MARK.search(clauses[i - 2])))
        if not marked or not any(p in c for p in forms):
            continue
        if not need_stat or STAT_ZH[basis] in c:
            return True, c.strip()[:70]
        return False, "figure found but the clause names no stat: %s" % c.strip()[:50]
    if any(p in zh for p in forms):
        return False, "figure present but never in a pursuit clause"
    return False, "figure absent from the Chinese entirely"


def coefficientless_subskills(skill_id):
    """-> the sub-skills this parent pursues via that state no coefficient of their own.

    More than one means a single parent-keyed figure cannot be right for all of them.
    """
    sk = specs.skills()
    spec = sk.get(skill_id) or {}
    out = []
    for e in spec.get("effects") or []:
        if e.get("op") != "follow_up" or e.get("coefficient") is not None:
            continue
        sub = sk.get(e.get("skill")) or {}
        coefs = [x.get("coefficient") for x in (sub.get("effects") or [])
                 if x.get("op") == "damage"]
        if coefs and all(c is None for c in coefs) and e.get("skill") not in out:
            out.append(e.get("skill"))
    return out


def missing():
    """Parent skills whose pursuit still lands a 0 -- no figure in either place."""
    sk = specs.skills()
    out = []
    for sid, spec in sk.items():
        for e in spec.get("effects") or []:
            if e.get("op") != "follow_up" or e.get("coefficient") is not None:
                continue
            if pursuit_values.lookup(sid):
                continue
            sub = sk.get(e.get("skill")) or {}
            coefs = [x.get("coefficient") for x in (sub.get("effects") or [])
                     if x.get("op") == "damage"]
            if coefs and all(c is None for c in coefs):
                out.append((sid, spec.get("name"), e.get("skill")))
                break
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--missing", action="store_true",
                    help="list pursuits with no figure anywhere instead of auditing")
    args = ap.parse_args()

    if args.missing:
        rows = missing()
        for sid, name, sub in sorted(rows):
            print(f"{sid:>10}  {(name or '?')[:34]:36} -> {sub}")
        print(f"\n{len(rows)} parent skill(s) still land a zero-damage pursuit")
        return 0

    bad = []
    for sid, (coef, basis) in sorted(pursuit_values.PURSUIT.items()):
        ok, why = verify(sid, coef, basis)
        if not ok:
            bad.append((sid, coef, basis, why))
    for (sid, sub), (coef, basis) in sorted(pursuit_values.PURSUIT_BY_SUB.items()):
        ok, why = verify(sid, coef, basis)
        if not ok:
            bad.append((f"{sid}/{sub}", coef, basis, why))

    # Lower tiers: the FIGURE must still be Chinese. Only the stat came from elsewhere,
    # so the stat half of the check is dropped and nothing else is.
    for label, table in (("EN-stat", pursuit_values.PURSUIT_STAT_FROM_EN),
                         ("house-stat", pursuit_values.PURSUIT_STAT_INVENTED)):
        for sid, (coef, basis) in sorted(table.items()):
            ok, why = verify(sid, coef, basis, need_stat=False)
            if not ok:
                bad.append((f"{sid} [{label}]", coef, basis, why))
            elif STAT_ZH[basis] in (why or ""):
                bad.append((f"{sid} [{label}]", coef, basis,
                            "the Chinese DOES name this stat -- promote it into PURSUIT"))

    # The named-skill tier states no percentage at all; what it must state is the SKILL.
    for sid, (coef, basis) in sorted(pursuit_values.PURSUIT_NAMED_SKILL.items()):
        zh = ((dd.row("skill", sid) or {}).get("_note1") or "")
        if "普攻" not in zh:
            bad.append((f"{sid} [named-skill]", coef, basis,
                        "Chinese no longer names a basic attack for this pursuit"))

    # THE TRAP THIS MISSED ONCE. Verifying that a figure appears in a pursuit clause does
    # NOT verify it is the figure for THIS pursuit: Judge's Mercy states 35% and 90% in
    # one clause chain, and a parent-keyed 90 verified clean while paying the 35% pursuit
    # 2.57x. The check is STRUCTURAL rather than another regex, because a prose scan
    # cannot tell a second pursuit from a neighbouring heal -- 再追擊40%防禦力... 追擊後
    # 使我方力屬角色回復20%體力 states two percentages and only one is damage.
    for sid in sorted(pursuit_values.PURSUIT):
        subs = coefficientless_subskills(sid)
        if len(subs) > 1:
            bad.append((sid, pursuit_values.PURSUIT[sid][0], "-",
                        f"drives {len(subs)} distinct coefficient-less pursuits {subs} "
                        f"but carries ONE figure -- key it by sub-skill (PURSUIT_BY_SUB)"))

    for sid, coef, basis, why in bad:
        print(f"FAIL {sid} ({coef} {basis}): {why}")
    total = (len(pursuit_values.PURSUIT) + len(pursuit_values.PURSUIT_BY_SUB)
             + len(pursuit_values.PURSUIT_STAT_FROM_EN)
             + len(pursuit_values.PURSUIT_STAT_INVENTED)
             + len(pursuit_values.PURSUIT_NAMED_SKILL))
    print(f"{total} entries across 5 tiers "
          f"({len(pursuit_values.PURSUIT)} zh, {len(pursuit_values.PURSUIT_BY_SUB)} zh/sub, "
          f"{len(pursuit_values.PURSUIT_STAT_FROM_EN)} en-stat, "
          f"{len(pursuit_values.PURSUIT_STAT_INVENTED)} house-stat, "
          f"{len(pursuit_values.PURSUIT_NAMED_SKILL)} named-skill), {len(bad)} unverified")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

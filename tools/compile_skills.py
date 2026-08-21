#!/usr/bin/env python3
"""Compile the design pack's skill rows into a checked-in, diffable spec.

The point of compiling offline: a wrong skill becomes a visible one-line data change in
git, not a mystery at turn 7 of a fight. Nothing here runs at battle time.

Sources, in order of confidence (see docs/BATTLE_CLIENT_CONTRACT.md):

  targeting breadth   `_target` -> the client's own `23000 + _target` label table  EXACT
  swings              cinematic BscTagKind.Damage tag count, else `hit`            EXACT
  cd / charge / type  design columns                                               EXACT
  effects             the opcode slots `_action[]` / `_actID[]`                     EXACT
  damage coefficient  prose (`Deals 108% ATK as damage`) -- nowhere else            INFERRED

Only the damage coefficient is prose-derived. Damage is genuinely absent from the opcode
script: pure-damage skills often carry no opcodes at all.

    tools/compile_skills.py [--skill ID ...] [--out FILE] [--stats]
"""
import argparse
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "server")
SWINGS_FILE = os.path.join(SERVER, "battle_data/cinematic_swings.json")
OUT_DEFAULT = os.path.join(SERVER, "battle_data/skills.json")

sys.path.insert(0, SERVER)
os.chdir(SERVER)
import battle as bt  # noqa: E402

SKILL_TYPE = {1: "com_attack", 2: "skill", 3: "sp_skill", 4: "passive",
              5: "support", 6: "status", 7: "sub_skill", 10: "god_item",
              101: "collection1", 102: "collection2", 103: "collection3"}

TARGET_GROUP = {0: "enemy", 1: "ally", 2: "dead_enemy", 3: "dead_ally"}

# Status id blocks -> (category, stackable). From op 114's category operands, each
# independently confirmed by prose. See contract doc 6.1.
STATUS_BLOCK = {
    1000: ("heal_over_time", False),
    2000: ("buff", False),
    3000: ("buff", True),
    4000: ("shield", False),
    5000: ("damage_over_time", False),
    6000: ("debuff", False),
    7000: ("debuff", True),
    8000: ("passive_grant", False),
    9000: ("stat_up", False),
}

# Opcodes that take an operand. Everything else is a trigger or an operandless effect --
# op 115 (skill-CD change) proves "no operand" does NOT imply "condition".
OP_APPLY, OP_APPLY_CHANCE, OP_REMOVE, OP_FOLLOW_UP = 112, 113, 114, 117
OP_MODIFY_CD = 115

_COEF = re.compile(r"(\d+(?:\.\d+)?)\s*%\s*(ATK|DEF|Max HP|HP)", re.I)


def _text(tid):
    row = (bt.dd.rows("text") or {}).get(tid) or (bt.dd.rows("text") or {}).get(str(tid))
    if not row:
        return None
    return row.get("_text_en") or row.get("_text") or None


def breadth(target):
    """-> a structured targeting spec, from the client's own label table.

    `_target` packs group*100 + range, and the UI label is text `23000 + _target`. We
    parse that label because it is the client's own words for the rule, which beats
    re-deriving the range semantics ourselves.
    """
    t = int(target or 0)
    if t == 0:
        return {"group": None, "select": "none", "count": 0}
    spec = {"group": TARGET_GROUP.get(t // 100, str(t // 100))}
    label = _text(23000 + t)
    if not label:
        return dict(spec, select="unknown", count=None, raw=t)
    low = label.lower()
    spec["label"] = label
    if low.startswith("player"):
        # "Player", "Player+1 ally", "Player+2 allies"
        n = re.search(r"\+(\d+)", low)
        spec.update(select="self_plus", count=1 + (int(n.group(1)) if n else 0))
    elif "all " in low or low.startswith("all"):
        spec.update(select="all", count=None)
    elif "random" in low:
        n = re.match(r"(\d+)", low)
        spec.update(select="random", count=int(n.group(1)) if n else 1)
    elif "highest" in low or "lowest" in low:
        stat = re.search(r"(hp|atk|def|spd)", low)
        spec.update(select=("highest" if "highest" in low else "lowest"),
                    stat=(stat.group(1).upper() if stat else None), count=1)
    elif re.match(r"^(str|agi|tec)\b", low):
        spec.update(select="by_attribute", attribute=low.split()[0].upper(), count=1)
    elif "except player" in low:
        spec.update(select="all_except_self", count=None)
    else:
        n = re.match(r"(\d+)", low)
        spec.update(select="count", count=int(n.group(1)) if n else 1)
    return spec


def status_meta(rows, sid):
    """-> {id, name, category, stackable, stack_cap} for a status row id."""
    r = rows.get(sid) or {}
    name = (r.get("_name_en") or r.get("_name") or "").strip()
    cat, stackable = STATUS_BLOCK.get(sid // 1000 * 1000 if sid < 100000 else -1,
                                      ("other", False))
    cap = None
    m = re.search(r"\((\d+)\)\s*$", name)
    if m:
        cap = int(m.group(1))
        stackable = True
    return {"id": sid, "name": re.sub(r"\s*\(\d+\)\s*$", "", name) or None,
            "category": cat, "stackable": stackable, "stack_cap": cap}


def effects(rows, r):
    """-> the effect list, read straight off the opcode slots."""
    acts, ids = r.get("_action") or [], r.get("_actID") or []
    out = []
    for i, op in enumerate(acts):
        if not op:
            continue
        aid = ids[i] if i < len(ids) else 0
        if op in (OP_APPLY, OP_APPLY_CHANCE) and aid in rows:
            out.append({"op": "apply_status", "slot": i,
                        "chance": op == OP_APPLY_CHANCE,
                        "status": status_meta(rows, aid)})
        elif op == OP_REMOVE:
            if aid in rows:
                out.append({"op": "remove_status", "slot": i,
                            "status": status_meta(rows, aid)})
            elif aid:
                cat, stackable = STATUS_BLOCK.get(aid, ("unknown", None))
                out.append({"op": "remove_status", "slot": i,
                            "category": cat, "stackable": stackable, "raw": aid})
        elif op == OP_FOLLOW_UP and aid:
            out.append({"op": "follow_up", "slot": i, "skill": aid,
                        "name": (rows.get(aid) or {}).get("_name_en")
                                or (rows.get(aid) or {}).get("_name")})
        elif op == OP_MODIFY_CD:
            out.append({"op": "modify_cd", "slot": i})
        else:
            # Trigger conditions and the rarer operandless effects. Kept verbatim rather
            # than guessed at -- see contract doc 6.3/6.5.
            out.append({"op": f"raw_{op}", "slot": i,
                        **({"operand": aid} if aid else {})})
    return out


def damage(r):
    """-> the damage entry, from prose. The ONE inferred field."""
    note = r.get("_note1_en") or ""
    m = _COEF.search(note)
    if not m:
        return None
    times = None
    for word, n in (("twice", 2), ("three times", 3), ("four times", 4),
                    ("five times", 5)):
        if word in note.lower():
            times = n
            break
    return {"op": "damage", "basis": m.group(2).upper().replace(" ", "_"),
            "coefficient": round(float(m.group(1)) / 100.0, 4),
            "prose_times": times, "source": "prose"}


def compile_skill(rows, sid, swings_by_act):
    r = rows.get(sid)
    if not r:
        return None
    act = (r.get("_actName") or "").strip()
    typ = SKILL_TYPE.get(r.get("_type"), str(r.get("_type")))
    declared = int(r.get("_count") or 0)
    cine = swings_by_act.get(act)
    # A PASSIVE/STATUS row can carry a vestigial _actName it never renders (see contract
    # doc: 136 of the 193 apparent mismatches were exactly this), so ignore it there.
    if typ in ("passive", "status") or not act:
        swings, src = declared, "hit"
    elif cine:
        swings, src = cine, "cinematic"
    else:
        # Tagless cinematic -> DoAllDamage flattens the list; grouping is free.
        swings, src = declared, "hit(flatten)"
    spec = {
        "id": sid, "name": r.get("_name_en") or r.get("_name"),
        "group": r.get("_group"), "lv": r.get("_lv"), "type": typ,
        "target": breadth(r.get("_target")),
        "swings": swings, "swings_from": src,
        "cd": r.get("_cdTurn"), "charge": r.get("_charge"),
        "cinematic": act or None,
        "effects": [],
    }
    d = damage(r)
    if d:
        spec["effects"].append(d)
    spec["effects"].extend(effects(rows, r))
    return spec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skill", type=int, action="append", help="compile just these ids")
    ap.add_argument("--out", default=OUT_DEFAULT)
    ap.add_argument("--stats", action="store_true")
    args = ap.parse_args()

    rows = bt.dd.rows("skill") or {}
    swings_by_act = {}
    if os.path.isfile(SWINGS_FILE):
        with open(SWINGS_FILE) as f:
            swings_by_act = json.load(f)
    else:
        print(f"warning: {SWINGS_FILE} missing -- run tools/skill_cinematics.py --json",
              file=sys.stderr)

    if args.skill:
        for sid in args.skill:
            print(json.dumps(compile_skill(rows, sid, swings_by_act),
                             indent=2, ensure_ascii=False))
        return

    out = {}
    for sid in rows:
        spec = compile_skill(rows, sid, swings_by_act)
        if spec:
            out[str(sid)] = spec
    with open(args.out, "w") as f:
        json.dump(out, f, indent=1, ensure_ascii=False, sort_keys=True)
    print(f"wrote {args.out}: {len(out)} skills")

    if args.stats:
        import collections
        ops = collections.Counter()
        unknown = collections.Counter()
        dmg = 0
        for s in out.values():
            for e in s["effects"]:
                ops[e["op"]] += 1
                if e["op"].startswith("raw_"):
                    unknown[e["op"]] += 1
            dmg += any(e["op"] == "damage" for e in s["effects"])
        print("  effect ops:", dict(ops.most_common(8)))
        print("  skills with a damage entry:", dmg)
        print("  still-raw opcodes:", dict(unknown.most_common(8)))


if __name__ == "__main__":
    main()

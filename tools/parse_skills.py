#!/usr/bin/env python3
"""Parse skill descriptions into ordered effect lists for the battle engine.

Step 2 of the effect-system revamp (step 1 = tools/extract_status_catalog.py). The battle
is server-authoritative and the skill mechanics live ONLY in the description text (the
client never reads `_action`/`_actID`; those are opaque type ids) -- so this turns each
skill's prose into a structured, ordered list of effect primitives the runtime can execute,
referencing the status catalog for what each applied status does.

Grammar, observed across the starter casts (Lucifer 10001 / Leviathan 10011, the
validation set a new account actually fields):

  <clause> = [trigger] [chance] <op> [target] [duration]
  trigger  = on_use(default) | before_action | after_attack | after_action |
             battle_start | on_counter | conditional("If ...")
  op       = damage(pct,times) | apply_status(name) | cleanse(names) | heal(pct) |
             skill_cd(delta) | move_gauge(pct) | immunity(name) | stat_buff | shield |
             extend_status
  target   = self | enemy_target | all_allies | all_enemies | highest_<stat>_enemy ...

Passives carry NAMED sub-ability blocks ("Fear Nothing: ...", "Jealousy Counter: ..."),
each its own trigger+effects; those are split first. Anything unparsed is kept verbatim in
`unparsed` so nothing is silently dropped and coverage is measurable.

Usage:
  tools/parse_skills.py            # validate on the starter casts (verbose)
  tools/parse_skills.py --all      # parse every skill, report coverage
"""
import json, os, re, sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "server")
CATALOG = os.path.join(SERVER, "battle_data", "status_catalog.json")
COLOR_RE = re.compile(r"\[[0-9A-Fa-f]{6}\]|\[-\]")
WORDNUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}

# --- target phrases -> canonical token -------------------------------------
TARGET_RE = [
    (re.compile(r"the enemy with the highest (\w+)", re.I),
     lambda m: f"highest_{m.group(1).lower()}_enemy"),
    (re.compile(r"the enemy target with the highest (\w+)", re.I),
     lambda m: f"highest_{m.group(1).lower()}_enemy"),
    (re.compile(r"all allies|all all(?:y|ies)", re.I), lambda m: "all_allies"),
    (re.compile(r"all enemies|all enemy targets", re.I), lambda m: "all_enemies"),
    (re.compile(r"the caster|itself|self", re.I), lambda m: "self"),
    (re.compile(r"the target|the enemy", re.I), lambda m: "enemy_target"),
]


def target_of(text):
    for regex, fn in TARGET_RE:
        m = regex.search(text)
        if m:
            return fn(m)
    return None


# Verb forms of a status ("stuns" = apply Stun). Extend as encountered.
VERB_STATUS = {"stun": "Stun", "freeze": "Freeze", "silence": "Silence",
               "poison": "Poison", "burn": "Burn", "seal": "Seal", "daze": "Daze"}


def clean(text):
    return COLOR_RE.sub("", text or "").replace("\n", " ").strip()


def strip_status_defs(text):
    """Remove the inline '* Name: definition' glosses (already in the catalog).
    Returns (effect_prose, [defined status names])."""
    names = []
    def grab(m):
        names.append(m.group(1).strip())
        return " "
    # A def runs from '*' up to the NEXT '*', a two-word block header
    # ("Jealousy Counter:"), or end -- otherwise a trailing named block that follows the
    # last '* Slow: ...' gloss gets swallowed with the definition.
    prose = re.sub(
        r"[*★]\s*([A-Z][A-Za-z ]{1,22}?)\s*:\s*"
        r".*?(?=\s*[*★]|\s+[A-Z][a-z]+(?: [A-Z][a-z]+)+:|\Z)", grab, text)
    return re.sub(r"\s+", " ", prose).strip(), names


NAMED_BLOCK_RE = re.compile(r"([A-Z][A-Za-z][A-Za-z' ]{2,24}?):\s")


def split_named_blocks(prose):
    """Passives read as 'Ability One: ... Ability Two: ...'. -> [(name, body)];
    a single unnamed block returns [(None, prose)]."""
    hits = list(NAMED_BLOCK_RE.finditer(prose))
    # Only treat as named blocks when a header sits at the very start (passives).
    if not hits or hits[0].start() > 2:
        return [(None, prose)]
    blocks = []
    for i, m in enumerate(hits):
        end = hits[i + 1].start() if i + 1 < len(hits) else len(prose)
        blocks.append((m.group(1).strip(), prose[m.end():end].strip()))
    return blocks


TRIGGERS = [
    (re.compile(r"^before the action[,:]?\s*", re.I), "before_action"),
    (re.compile(r"^after (?:an? )?attack[,:]?\s*", re.I), "after_attack"),
    (re.compile(r"^after (?:the action|dealing damage)[,:]?\s*", re.I), "after_action"),
    (re.compile(r"^when a battle starts[,:]?\s*", re.I), "battle_start"),
    (re.compile(r"^when taking (?:enemy )?counterattack[,:]?\s*", re.I), "on_counter"),
]


def split_clauses(body):
    """Sentence/segment split that keeps a trigger attached to what it governs."""
    parts = re.split(r"(?<=[.;])\s+", body)
    out = []
    for p in parts:
        p = p.strip()
        if p:
            out.append(p)
    return out


# --- op matchers: (regex, builder(match, clause_text) -> effect dict) --------
DUR_RE = re.compile(r"for (\d+|" + "|".join(WORDNUM) + r") turns?", re.I)
CHANCE_RE = re.compile(r"(\d+)% (?:fixed )?chance to", re.I)


def _dur(text):
    m = DUR_RE.search(text)
    if not m:
        return None
    t = m.group(1).lower()
    return WORDNUM.get(t, int(t) if t.isdigit() else t)


# A segment boundary (', ' or ' and ') only starts a NEW effect when what follows opens
# with one of these -- an effect verb or a subject. A bare 'and Fragile' / 'and Listless
# effects' (a status list) has no such opener, so it stays joined to its op.
EFFECT_START = re.compile(
    r"(?:deals?|inflicts?|grants?|restores?|reduces?|decreases?|increases?|removes?|"
    r"gains?|opens?|extends?|stuns?|freezes?|silences?|the caster|the target|"
    r"has an?|open an?|\d+%\s+(?:fixed\s+)?chance)\b", re.I)


def split_segments(text, catalog):
    """Split a sentence into effect segments so each op resolves its OWN target/chance,
    without breaking multi-status lists. We only cut at a ', '/' and ' that is followed
    by a new effect opener (EFFECT_START); a status-list 'and' is not."""
    segs, last = [], 0
    for m in re.finditer(r"(?:,\s*|\s+and\s+)", text):
        if EFFECT_START.match(text, m.end()):
            seg = text[last:m.start()].strip()
            if seg:
                segs.append(seg)
            last = m.end()
    tail = text[last:].strip()
    if tail:
        segs.append(tail)
    return segs


def parse_clause(clause, catalog):
    """-> list of effect dicts for one clause. Trigger is clause-level; target and
    chance are resolved per SEGMENT so co-located ops don't steal each other's."""
    trigger = "on_use"
    text = clause
    for regex, name in TRIGGERS:
        if regex.search(text):
            trigger = name
            text = regex.sub("", text)
            break
    if re.match(r"^if\b", text, re.I):
        trigger = "conditional"

    effects = []
    for seg in split_segments(text, catalog):
        effects.extend(parse_segment(seg, trigger, catalog))
    seen, uniq = set(), []
    for e in effects:
        k = json.dumps(e, sort_keys=True)
        if k not in seen:
            seen.add(k); uniq.append(e)
    return uniq


def parse_segment(text, trigger, catalog):
    """Parse ONE effect segment (local target/chance scope)."""
    effects = []
    base = {"trigger": trigger}
    ch = CHANCE_RE.search(text)
    if ch:
        base["chance"] = int(ch.group(1))

    # damage, two word orders:
    #  "deals X% ATK as damage [N times]"
    #  "deals damage [on <t>] by X% ATK [N times]"
    for m in re.finditer(r"deals?\s+(\d+)%\s+ATK\s+as\s+damage(?:\s+(\d+|two|three)\s+times)?",
                         text, re.I):
        times = m.group(2)
        effects.append({**base, "op": "damage", "pct_atk": int(m.group(1)),
                        "times": WORDNUM.get((times or "").lower(),
                                             int(times) if times and times.isdigit() else 1),
                        "target": target_of(text) or "enemy_target"})
    for m in re.finditer(r"deals?\s+damage\s+(?:on\s+(.+?)\s+)?by\s+(\d+)%\s+ATK"
                         r"(?:\s+(\d+|two|three)\s+times)?", text, re.I):
        times = m.group(3)
        effects.append({**base, "op": "damage", "pct_atk": int(m.group(2)),
                        "times": WORDNUM.get((times or "").lower(),
                                             int(times) if times and times.isdigit() else 1),
                        "target": target_of(m.group(1) or text) or "enemy_target"})
    # heal: "restores X% of Max HP"
    m = re.search(r"restores?\s+(\d+)%\s+of\s+Max\s+HP", text, re.I)
    if m:
        effects.append({**base, "op": "heal", "pct_maxhp": int(m.group(1)),
                        "target": target_of(text) or "self"})
    # skill CD: "increases the Skill CD of <t> by N" / "reduce ... Skill CD ... by N"
    m = re.search(r"(increase|reduce|decrease)s?\s+the\s+Skill\s+CD\s+of\s+(.+?)\s+by\s+(\d+)",
                  text, re.I)
    if m:
        sign = 1 if m.group(1).lower() == "increase" else -1
        effects.append({**base, "op": "skill_cd", "delta": sign * int(m.group(3)),
                        "target": target_of(m.group(2)) or "enemy_target"})
    # move gauge: "Move Gauge+30%" or "reduce the target's Move Gauge by 35%"
    m = re.search(r"Move\s+Gauge\s*([+\-])\s*(\d+)%", text, re.I)
    if m:
        effects.append({**base, "op": "move_gauge",
                        "pct": (1 if m.group(1) == "+" else -1) * int(m.group(2)),
                        "target": target_of(text) or "self"})
    m = re.search(r"reduce\s+(.+?)\s+Move\s+Gauge\s+by\s+(\d+)%", text, re.I)
    if m:
        effects.append({**base, "op": "move_gauge", "pct": -int(m.group(2)),
                        "target": target_of(m.group(1)) or "enemy_target"})
    # immunity: "gains immunity to <status> for N turns"
    m = re.search(r"immunity to\s+(\w+)\s+for\s+(\d+)\s+turns?", text, re.I)
    if m:
        effects.append({**base, "op": "immunity", "status": m.group(1),
                        "duration": int(m.group(2)), "target": target_of(text) or "self"})
    # stat buff/debuff by verb: "increase the caster's SPD by 30% for 2 turns"
    for m in re.finditer(r"(increase|reduce|decrease)s?\s+(.+?)'s\s+(ATK|DEF|SPD)\s+by\s+(\d+)%",
                         text, re.I):
        sign = 1 if m.group(1).lower() == "increase" else -1
        effects.append({**base, "op": "stat_mod", "stat": m.group(3).upper(),
                        "pct": sign * int(m.group(4)), "duration": _dur(text),
                        "target": target_of(m.group(2)) or "self"})
    # shield: "open a 7000 Shield (2 turns)"
    m = re.search(r"open a\s+(\d+)\s+Shield\s*\((\d+)\s*turns?\)", text, re.I)
    if m:
        effects.append({**base, "op": "shield", "amount": int(m.group(1)),
                        "duration": int(m.group(2)), "target": target_of(text) or "self"})
    # extend: "extends <status>'s effect to N turns"
    m = re.search(r"extends?\s+(.+?)'s\s+effect\s+to\s+(\d+)\s+turns?", text, re.I)
    if m:
        effects.append({**base, "op": "extend_status", "status": m.group(1).strip(),
                        "duration": int(m.group(2))})
    # cleanse: "removes X and Y effects from all allies"
    m = re.search(r"removes?\s+(.+?)\s+effects?\s+from\s+(.+)", text, re.I)
    if m:
        statuses = re.split(r"\s+and\s+|,\s*", m.group(1))
        effects.append({**base, "op": "cleanse",
                        "statuses": [s.strip() for s in statuses if s.strip()],
                        "target": target_of(m.group(2)) or "all_allies"})
    # verb-status: "stuns the enemy ..." -> apply Stun. Only the ACTIVE verb form
    # ("stuns"/"stun the"), never a noun mention ("immunity to Freeze", "* Freeze:").
    for verb, status in VERB_STATUS.items():
        if re.search(rf"\bimmunity to {verb}", text, re.I):
            continue
        vm = re.search(rf"\b{verb}s\b|\b{verb} the\b", text, re.I)
        if vm:
            # target sits right after the verb ("Freeze the target"), not earlier in
            # the segment ("the caster has a chance to Freeze the target").
            effects.append({**base, "op": "apply_status", "status": status,
                            "target": target_of(text[vm.end():]) or "enemy_target",
                            "duration": _dur(text)})
            break
    # apply status: "inflicts/grants <A> [and <B>] on/to <target> [for N turns]"
    m = re.search(r"(?:inflicts?|grants?|inflict|grant)\s+(?:the\s+\w+\s+|all\s+\w+\s+)?"
                  r"(.+?)(?:\s+(?:on|to)\s+(.+?))?(?:\s+for\s+\d+\s+turns?)?[.]?$", text, re.I)
    if m:
        # split "Freeze and Fragile" / "Gale on all allies and Slow on all enemies"
        chunk = m.group(1)
        # handle the "A on X and B on Y" form within the segment
        pairs = re.findall(r"([A-Z][A-Za-z ]+?)\s+on\s+(all allies|all enemies|the target|the enemy[\w ]*)",
                           text)
        if pairs:
            for names, tgt in pairs:
                # "Freeze and Fragile on X" -> both to X; "A on X and B on Y" -> pairs.
                for nm in re.split(r"\s+and\s+|,\s*", names):
                    nm = nm.strip()
                    if nm in catalog:
                        effects.append({**base, "op": "apply_status", "status": nm,
                                        "target": target_of(tgt) or "enemy_target",
                                        "duration": _dur(text)})
        else:
            for nm in re.split(r"\s+and\s+|,\s*", chunk):
                nm = nm.strip()
                if nm in catalog:
                    effects.append({**base, "op": "apply_status", "status": nm,
                                    "target": target_of(m.group(2) or text) or "enemy_target",
                                    "duration": _dur(text)})
    # de-dup identical effects
    seen, uniq = set(), []
    for e in effects:
        k = json.dumps(e, sort_keys=True)
        if k not in seen:
            seen.add(k); uniq.append(e)
    return uniq


def parse_skill(row, catalog):
    prose, defined = strip_status_defs(clean(row.get("_note1_en")))
    blocks = split_named_blocks(prose)
    out_blocks, unparsed = [], []
    for name, body in blocks:
        effects = []
        for clause in split_clauses(body):
            got = parse_clause(clause, catalog)
            if got:
                effects.extend(got)
            elif clause.strip():
                unparsed.append(clause.strip())
        out_blocks.append({"name": name, "effects": effects})
    return {"id": row.get("_id"), "type": row.get("_type"),
            "target": row.get("_target"), "hits": row.get("_count"),
            "defined_statuses": defined, "blocks": out_blocks, "unparsed": unparsed}


def main():
    sys.path.insert(0, SERVER)
    import design_data as dd                                         # noqa: E402
    catalog = json.load(open(CATALOG, encoding="utf-8"))
    sk = dd.rows("skill")

    if "--all" not in sys.argv:
        # validation set: the starter casts
        for cid in (10001, 10011):
            crow = dd.row("char", cid) or {}
            print(f"===== char {cid} =====")
            for sid in (crow.get("_skills") or []):
                r = sk.get(sid)
                if not r:
                    continue
                res = parse_skill(r, catalog)
                print(f"[{sid}] type={res['type']} target={res['target']} hits={res['hits']}")
                for b in res["blocks"]:
                    if b["name"]:
                        print(f"   ({b['name']})")
                    for e in b["effects"]:
                        print("     ", json.dumps(e, ensure_ascii=False))
                for u in res["unparsed"]:
                    print(f"   UNPARSED: {u}")
        return

    total = clauses = parsed_clauses = complete = 0
    out = {}
    for sid, r in sk.items():
        n1 = clean(r.get("_note1_en"))
        if not n1 or n1 in ("無", "-", "Unuseful"):
            continue
        total += 1
        res = parse_skill(r, catalog)
        got = sum(len(b["effects"]) for b in res["blocks"])
        clauses += got + len(res["unparsed"])
        parsed_clauses += got
        # "complete" = every clause turned into an effect (nothing left unparsed). The
        # engine should trust these; the rest need parser work before they are safe.
        res["complete"] = not res["unparsed"] and got > 0
        if res["complete"]:
            complete += 1
        out[str(sid)] = res
    print(f"skills with English text: {total}")
    print(f"clauses: {clauses}, parsed into effects: {parsed_clauses} "
          f"({100 * parsed_clauses // max(clauses, 1)}%)")
    print(f"fully-understood skills (no unparsed clauses): {complete} "
          f"({100 * complete // max(total, 1)}%)")

    if "--write" in sys.argv:
        path = os.path.join(SERVER, "battle_data", "skill_effects.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1, sort_keys=True)
        print(f"wrote {path}")


if __name__ == "__main__":
    main()

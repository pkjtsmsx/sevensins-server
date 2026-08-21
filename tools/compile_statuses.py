#!/usr/bin/env python3
"""Phase 2: compile the status registry from the design pack.

**Structured data first.** Everything that can be read from a column is read from a
column; prose is used only where the pack genuinely has no field for it. The split, as
measured (see docs/BATTLE_CLIENT_CONTRACT.md 5.1):

  FROM COLUMNS -- which statuses a skill applies (`action`/`act_id`), the category and
  stackability (the id block), the stack cap (the `(N)` name suffix), the icon
  (`statusID`/`spriteID`), whether the client hides it (`hide`), the status's own
  upgrade chain (`group`/`lv`), and -- for the 510 status rows that have one -- the
  status's OWN opcode script, i.e. statuses that apply further statuses.

  FROM PROSE ONLY -- magnitude and duration. A type-6 row has no field for either. This
  is not an oversight in the pack: the values belong to the (skill, status) PAIR, not to
  the status. Frozen Inferno Thorn keeps `act_id [6050, 2005, 4283, 4101, 0]` unchanged
  from lv1 to lv6 while its Gash goes from "+35%, two turns" to "+40%, three turns".
  So magnitude/duration are emitted per applying skill by compile_skills.py, NOT here.

What this file therefore owns is the status's IDENTITY and SEMANTIC KIND -- the part that
really is invariant across every skill that applies it.

    python3 compile_statuses.py [--out DIR] [--report]
"""
import argparse
import collections
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "server")
sys.path.insert(0, SERVER)

# design_data is the design-PACK reader (253 lines, no battle imports). Deliberately not
# reached via `import battle`: nothing in the new engine should touch the old one, and
# going through it would let the old module's assumptions in through the back door.
import design_data as dd  # noqa: E402

TYPE_STATUS = 6
OP_APPLY, OP_APPLY_CHANCE, OP_REMOVE = 112, 113, 114

# The id space is organised in thousands-blocks; see contract doc 6.1. This is the
# taxonomy, and it comes from a column, not from reading English.
STATUS_BLOCK = {
    0:    ("misc", False),
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

# Semantic kinds, decided from prose ONLY because no column encodes them.
#
# Order is load-bearing -- first match wins -- and the overlaps are real, not sloppy:
# "cannot be Healed" is literally an immunity but functionally a heal block, and Charm
# both lowers ATK and redirects attacks. The specific reading has to come first.
#
# Note these are PREFIX patterns with no trailing `\b`: the pack writes "immobilized",
# "immobolized" (sic) and "immobilizes", and a trailing boundary silently matches none
# of them -- which is exactly how Stun and Charm first came out as "other".
KIND_PATTERNS = [
    ("block_heal", r"cannot be healed|block(s|ed)? heal|healing received|"
                   r"healing.{0,12}(reduced|lowered|0)"),
    ("immunity",   r"\bimmun|nullifies|prevents? being|"
                   r"cannot be (afflicted|inflicted|affected)"),
    ("control",    r"crowd control|immobiliz|immobol|\bstun|cannot distinguish|"
                   r"(cannot|unable to) (act|use|cast|attack)|"
                   r"skips? action|skip.{0,24}turn|"
                   r"attacks? (both )?(on )?(the )?allies|only attack"),
    ("gauge",      r"move gauge"),
    ("shield",     r"\bshield|absorb"),
    # Plague and Tinder are DoTs written as a trigger rather than a noun -- "When a turn
    # starts, deal 20% of the effect owner's ATK as damage" -- so matching only the
    # phrase "damage over time" misses the two most-applied DoTs in the game.
    ("dot",        r"damage over time|\bdot\b|damage (each|per) turn|"
                   r"(when|at the start of|each|every).{0,30}turn.{0,50}\bdamage\b"),
    ("heal",       r"\b(restore|recover|heal)"),
    ("damage_mod", r"damage (dealt|output|taken)|final damage|\bdmg\b"),
    ("stat_mod",   r"\b(atk|def|spd|hp|cri|crit)\s*[+\-]|"
                   r"(increases?|reduces?|lowers?|raises?).{0,25}\b(atk|def|spd|hp)\b"),
]

# 20 status rows end their note with an explicit `(Crowd Control Debuff)`. That is a
# category stated by the data rather than inferred from wording, so it overrides the
# pattern scan outright.
CC_TAG_RE = re.compile(r"\(\s*crowd control[^)]*\)", re.I)

STAT_RE = re.compile(r"\b(ATK|DEF|SPD|HP|CRI|CRIT)\b", re.I)

# 500 status rows declare that a cleanse cannot touch them, always as a parenthetical.
# This matters more than it looks: op 114 removes by CATEGORY BLOCK ("removes the
# caster's removable buffs"), so without this flag a cleanse would wrongly strip half of
# them. No column carries it, so prose is the only source -- but it is a fixed marker,
# not free text, which is why it is safe to read.
UNREMOVABLE_RE = re.compile(
    r"\(\s*(un\s*-?\s*removable|cannot be cleansed[^)]*|"
    r"cannot be removed[^)]*)\s*\)", re.I)


def norm_name(s):
    """Join key for a status name.

    Strips the `(N)` stack cap and the `(SP)` variant marker, lowercases, and squashes
    punctuation -- the pack spells the same status `Lunge(SP)`, `Injured (SP)` and
    `Cease (SP)` with inconsistent spacing.
    """
    s = re.sub(r"\s*\(\s*SP\s*\)\s*$", "", s or "", flags=re.I)
    s = re.sub(r"\s*\(\d+\)\s*$", "", s)
    s = re.sub(r"[^a-z0-9]+", " ", s.lower())
    return " ".join(s.split())


def display_name(s):
    """The name without the stack-cap suffix, but keeping (SP) -- it IS a distinct row."""
    return re.sub(r"\s*\(\d+\)\s*$", "", (s or "").strip())


def stack_cap(s):
    m = re.search(r"\((\d+)\)\s*$", s or "")
    return int(m.group(1)) if m else None


def glossary_lines(note):
    """-> {name: body} for the `* Name: body` lines of one note1_en."""
    out = {}
    if not note or "*" not in note:
        return out
    for part in re.split(r"(?m)^\s*\*\s*", note)[1:]:
        flat = " ".join(part.split())
        m = re.match(r"^(.{1,60}?)\s*[:：]\s*(.+)$", flat)
        if m:
            out[m.group(1).strip()] = m.group(2).strip()
    return out


def _kind_of(body):
    low = (body or "").lower()
    if CC_TAG_RE.search(low):
        return "control"
    for kind, pat in KIND_PATTERNS:
        if re.search(pat, low):
            return kind
    return "other"


def classify(own_note, bodies):
    """-> (kind, stat, confidence, source).

    Two prose sources, and they are NOT equal:

      * the status row's OWN `note1_en` -- one authoritative sentence written about the
        status itself, present on 1,431 of the 1,685 type-6 rows;
      * the `* Name: body` glossary lines on every skill that applies it -- present for
        only 376 statuses, but up to 473 of them for a single status.

    The row's own note wins when it exists: it describes the status, whereas a glossary
    line describes the status *as used by that skill* and carries that skill's magnitude.
    The glossary is then used to corroborate -- confidence is the share of glossary
    bodies that reach the same kind, which is what makes a misclassification visible
    instead of silent. With no glossary to check against, confidence is reported as 1.0
    only when the row note itself classified; a status with neither source gets None.
    """
    glossary_kinds = [_kind_of(b) for b in bodies]
    stats = collections.Counter()
    for b in ([own_note] if own_note else []) + list(bodies):
        m = STAT_RE.search(b)
        if m:
            stats[m.group(1).upper().replace("CRIT", "CRI")] += 1
    stat = stats.most_common(1)[0][0] if stats else None

    if own_note:
        kind = _kind_of(own_note)
        if glossary_kinds:
            agree = sum(1 for k in glossary_kinds if k == kind) / len(glossary_kinds)
            return kind, stat, agree, "row_note+glossary"
        return kind, stat, 1.0, "row_note"
    if glossary_kinds:
        c = collections.Counter(glossary_kinds)
        kind, n = c.most_common(1)[0]
        return kind, stat, n / len(glossary_kinds), "glossary"
    return None, stat, 0.0, None


def nested_effects(rows, row):
    """A status row's OWN opcode script -- statuses that apply or remove statuses.

    510 of the 1,685 type-6 rows carry one. Ignoring these would silently drop a whole
    layer of the effect graph.
    """
    acts, ids = row.get("_action") or [], row.get("_actID") or []
    out = []
    for i, op in enumerate(acts):
        if not op:
            continue
        aid = ids[i] if i < len(ids) else 0
        if op in (OP_APPLY, OP_APPLY_CHANCE) and aid in rows:
            out.append({"op": "apply_status", "slot": i,
                        "chance": op == OP_APPLY_CHANCE, "status": aid})
        elif op == OP_REMOVE and aid:
            if aid in rows:
                out.append({"op": "remove_status", "slot": i, "status": aid})
            else:
                cat, _ = STATUS_BLOCK.get(aid, ("unknown", None))
                out.append({"op": "remove_category", "slot": i, "category": cat,
                            "raw": aid})
    return out


def build():
    rows = dd.rows("skill") or {}

    # -- prose bodies, gathered per NORMALISED NAME across the whole corpus ------------
    bodies = collections.defaultdict(list)
    for r in rows.values():
        for name, body in glossary_lines(r.get("_note1_en")).items():
            bodies[norm_name(name)].append(body)

    # -- how often each status is actually applied, and by what ----------------------
    applied_by = collections.Counter()
    for sid, r in rows.items():
        acts, ids = r.get("_action") or [], r.get("_actID") or []
        for i, op in enumerate(acts):
            if op in (OP_APPLY, OP_APPLY_CHANCE):
                aid = ids[i] if i < len(ids) else 0
                if aid in rows:
                    applied_by[aid] += 1

    out = {}
    for sid, r in rows.items():
        if r.get("_type") != TYPE_STATUS and sid not in applied_by:
            continue
        raw_name = r.get("_name_en") or r.get("_name") or ""
        key = norm_name(raw_name)
        # The thousands-block taxonomy only governs the low id space. Above 100000 the
        # ids are allocated per CONTENT -- arena field effects (7000000), carried stage
        # items (6170000), per-character mechanics, `[Event]` buffs -- so forcing them
        # into a category block would invent a fact. They are marked honestly and lean
        # on `kind` instead.
        if sid < 100000:
            block = sid // 1000 * 1000
            cat, stackable = STATUS_BLOCK.get(block, ("other", False))
        else:
            block, (cat, stackable) = -1, ("content", False)
        cap = stack_cap(raw_name)
        seen = bodies.get(key, [])
        own = (r.get("_note1_en") or "").strip()
        kind, stat, conf, source = classify(own, seen)
        out[sid] = {
            # ---- from columns -------------------------------------------------------
            "id": sid,
            "name": display_name(raw_name) or None,
            "name_key": key or None,
            "category": cat,
            "stackable": bool(stackable or cap),
            "stack_cap": cap,
            "hidden": bool(r.get("_hide")),
            "icon_status_id": r.get("_statusID") or None,
            "sprite_id": r.get("_spriteID") or None,
            "group": r.get("_group"),
            "lv": r.get("_lv"),
            "target": r.get("_target") or None,
            "nested": nested_effects(rows, r),
            "applied_by_skills": applied_by.get(sid, 0),
            "is_status_row": r.get("_type") == TYPE_STATUS,
            # ---- from prose, because no column carries it ---------------------------
            "description": own or None,
            "kind": kind,
            "stat": stat,
            "kind_confidence": round(conf, 3),
            "kind_source": source,
            "unremovable": bool(UNREMOVABLE_RE.search(own)),
            "prose_definitions": len(seen),
        }
    return out, rows


def report(reg, rows):
    n = len(reg)
    print(f"\nstatus registry: {n} rows "
          f"({sum(1 for s in reg.values() if s['is_status_row'])} type-6, "
          f"{sum(1 for s in reg.values() if not s['is_status_row'])} applied but not type-6)")

    print("\nby category (from the id block -- a COLUMN, not prose):")
    cats = collections.Counter(s["category"] for s in reg.values())
    for c, k in cats.most_common():
        print(f"   {c:18s} {k:5d}")

    print("\nsemantic kind (prose-voted; only source available):")
    kinds = collections.Counter(s["kind"] or "-- no prose --" for s in reg.values())
    for c, k in kinds.most_common():
        print(f"   {c:18s} {k:5d}")

    print("\nwhere the kind came from:")
    for c, k in collections.Counter(
            s["kind_source"] or "-- none --" for s in reg.values()).most_common():
        print(f"   {c:18s} {k:5d}")

    classified = [s for s in reg.values() if s["kind"]]
    weighted = sum(s["applied_by_skills"] for s in reg.values())
    print(f"\nclassified: {len(classified)}/{n} statuses")
    cov = sum(s["applied_by_skills"] for s in classified)
    print(f"   weighted by apply sites: {cov}/{weighted} ({cov/weighted:.0%})")
    unrem = [s for s in reg.values() if s["unremovable"]]
    print(f"\nunremovable (a cleanse must NOT strip these): {len(unrem)}")

    # Only the corroborated ones can disagree; a lone row note has nothing to check it.
    low = [s for s in classified
           if s["prose_definitions"] and s["kind_confidence"] < 0.6]
    print(f"\nlow-confidence classifications (<60% majority): {len(low)}")
    for s in sorted(low, key=lambda s: -s["applied_by_skills"])[:8]:
        print(f"   {s['id']:>7} {str(s['name'])[:26]:26s} kind={s['kind']:11s} "
              f"conf={s['kind_confidence']:.0%} over {s['prose_definitions']} bodies")

    nested = [s for s in reg.values() if s["nested"]]
    print(f"\nstatuses carrying their own opcode script: {len(nested)}")
    for s in sorted(nested, key=lambda s: -s["applied_by_skills"])[:5]:
        ops = ", ".join(e["op"] for e in s["nested"])
        print(f"   {s['id']:>7} {str(s['name'])[:26]:26s} -> {ops}")

    orphan = [s for s in reg.values() if not s["name"]]
    if orphan:
        print(f"\nWARNING: {len(orphan)} statuses have no name at all")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(SERVER, "battle_data"))
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args()

    reg, rows = build()
    os.makedirs(a.out, exist_ok=True)
    path = os.path.join(a.out, "statuses.json")
    with open(path, "w") as f:
        json.dump({str(k): v for k, v in sorted(reg.items())}, f,
                  indent=1, sort_keys=False, ensure_ascii=False)
    print(f"wrote {path}  ({len(reg)} statuses)")
    if a.report:
        report(reg, rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())

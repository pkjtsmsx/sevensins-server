#!/usr/bin/env python3
"""Extract the buff/debuff (status) catalog from skill descriptions.

Foundational data-mining pass for the battle-engine effect-system revamp. Established
empirically (see memory sevensins-battle): the battle is server-authoritative and the
client holds NO mechanics -- it never even reads the skill `_action`/`_actID` arrays, and
those codes encode an OPAQUE effect TYPE, not the magnitude. The ACTUAL mechanics live in
the human-readable skill descriptions, which additionally DEFINE each named status inline,
e.g.:

    Fracture: ATK-35%. Lasts for 2 turns.
    Fatigue:  SPD-6%, stacks up to 5 times. Lasts for 3 turns.
    Stun:     Immobilizes the target and increases its damage taken. Lasts for 1 turn.

This reads every "Name: definition" line, KEEPS ONLY GENUINE STATUSES (a definition is a
status iff its name is actually inflicted/granted somewhere -- "inflicts X", "grants X" --
OR its own definition parses to a mechanical record; the "Unlock Special Move" sections
otherwise flood the list with ~800 skill-name lines), picks the canonical (most-frequent)
definition per status, and parses it into a structured record the runtime effect system
can apply. Anything the parser can't yet structure is kept as raw text, never dropped.

Output: server/battle_data/status_catalog.json
Usage:  tools/extract_status_catalog.py
"""
import json, os, re, sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "server")
OUT_DIR = os.path.join(SERVER, "battle_data")
OUT = os.path.join(OUT_DIR, "status_catalog.json")

COLOR_RE = re.compile(r"\[[0-9A-Fa-f]{6}\]|\[-\]")
DEF_RE = re.compile(r"^[\s*★●・\-]*([A-Z][A-Za-z][A-Za-z ]{1,22}?)\s*[:：]\s*(\S.*)$")

# Statuses that are actually applied read as "inflicts/grants/gains/applies/removes/
# extends/dispels [the caster's/all allies'/an] <Name>". Used as the noise filter: a
# "Name: def" line whose name never appears here (and does not parse mechanically) is
# almost always a skill/special-move gloss, not a status.
APPLY_RE = re.compile(
    r"\b(?:inflicts?|grants?|gains?|applies|removes?|extends?|dispels?|immune to|"
    r"immunity to|affected by)\s+"
    r"(?:the\s+\w+'?s?\s+|all\s+\w+'?s?\s*|an?\s+|its?\s+|this\s+)?"
    r"([A-Z][A-Za-z][A-Za-z ]{1,20}?)"
    r"(?=\s+(?:on|to|for|and|effect|status|from|by|before|after|\.|,)|'|$)")

# ---- definition parser ------------------------------------------------------
STAT_ALIASES = {"ATK": "ATK", "DEF": "DEF", "SPD": "SPD", "HP": "HP",
                "CRIT": "CRIT", "CRT": "CRIT", "CRT DMG": "CRIT_DMG",
                "CRIT DMG": "CRIT_DMG"}
# "ATK-35%", "SPD +15%", "DEF Boost+25%", "CRT-35%", and flat "SPD-200"
STAT_RE = re.compile(
    r"\b(ATK|DEF|SPD|HP|CRIT|CRT(?:\s*DMG)?)\s*(?:Boost|Break|Weaken|Down|Up)?\s*"
    r"([+\-])\s*(\d+)(%?)")
# damage dealt/taken/received modifiers, ADJACENT form: "Damage dealt-40%",
# "Final damage dealt+35%", "Healing received -50%".
DMG_MOD_RE = re.compile(
    r"(Final damage dealt|Damage dealt|damage taken|Healing received|Healing dealt|"
    r"damage taken amount)\s*([+\-])\s*(\d+)%", re.I)
# ...and the far more common VERB form: "reduce[s] the AOE damage taken by 20%",
# "increases the damage taken by 10%", "Reduce damage taken by 6%". The verb carries the
# sign. "Damage dealt by the first attack+30%" is caught by the adjacent form above.
DMG_MOD_BY_RE = re.compile(
    r"(reduce|decrease|increase)s?\s+(?:the\s+|all\s+|AOE\s+|caster's\s+)*"
    r"(damage taken|damage dealt|damage dealt by the first attack)\s+"
    r"(?:amount\s+)?by\s+(\d+)%", re.I)
# Subject-first order: "(the caster's) Damage Taken reduces by 5%", "Damage dealt
# increases by 30%". Verb after the noun.
DMG_MOD_SUBJ_RE = re.compile(
    r"(damage taken|damage dealt)\s+(reduces?|decreases?|increases?)\s+by\s+(\d+)%", re.I)
DMG_MOD_KEY = {
    "final damage dealt": "final_damage", "damage dealt": "damage_dealt",
    "damage dealt by the first attack": "damage_dealt",
    "damage taken": "damage_taken", "damage taken amount": "damage_taken",
    "healing received": "healing_received", "healing dealt": "healing_dealt",
}
STACK_RE = re.compile(r"stacks?\s+up\s+to\s+(\d+)|up\s+to\s+(\d+)\s+stacks?")
WORDNUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
           "seven": 7, "eight": 8, "nine": 9, "ten": 10}
# "Lasts for 2 turns", "for3 turns", "lasts for three turns", "over 3 turns"
DURATION_RE = re.compile(r"(?:lasts?|last|for|over)\s*(?:for\s*)?(\d+|" +
                         "|".join(WORDNUM) + r")\s*turns?", re.I)
PERMANENT_RE = re.compile(r"entire battle|permanently|whole battle|rest of the battle", re.I)
# "deals 30% (the affected)/(the caster's) ATK as damage" for DoT/reflect magnitude
PCT_ATK_RE = re.compile(r"(\d+)%\s+(?:of\s+)?(?:the\s+)?[\w' ]*?ATK\s+as\s+damage", re.I)
# "Absorbs damage equal to 75% of the caster's ATK" (shield), "restores 25% of Max HP"
SHIELD_RE = re.compile(r"[Aa]bsorbs? damage.*?(\d+)%\s+of[\w' ]*ATK")
HEAL_RE = re.compile(r"[Rr]estores?[\w' ]*?(\d+)%\s+of[\w' ]*(?:Max )?HP")

FLAG_PHRASES = [
    (r"[Ii]mmobiliz", "immobilize"),
    (r"[Ss]kips? action", "skip_action"),
    (r"over time|damage to it over time|as damage.*turn starts|turn starts.*as damage",
     "damage_over_time"),
    (r"[Rr]estores?.*HP|[Rr]egenerat", "heal_over_time"),
    (r"[Aa]bsorbs? damage", "shield"),
    (r"[Uu]nable to (?:gain HP|be healed|gain.*recovery)|[Bb]lock.*[Hh]eal", "heal_block"),
    (r"[Uu]nable to be revived|[Bb]lock Revive", "revive_block"),
    (r"[Uu]nable to reduce Skill CD|CD Reduction Block", "cd_reduction_block"),
    (r"[Ii]mmunity to|[Ii]mmune to|blocks all damage", "immunity"),
    (r"cannot cast|Power Attack Seal|[Ss]eal", "ability_seal"),
    (r"attack the caster|only attack", "forced_target"),
    (r"attack.*allies|attack both", "confused_targeting"),
    (r"Move Gauge", "move_gauge_mod"),
    (r"[Uu]nremovable|[Cc]annot be removed", "unremovable"),
    (r"[Uu]nstackable", "unstackable"),
    (r"[Rr]emoves all debuffs", "cleanse_self"),
]


def clean(text):
    return COLOR_RE.sub("", text or "")


def _duration(text):
    if PERMANENT_RE.search(text):
        return "battle"
    m = DURATION_RE.search(text)
    if not m:
        return None
    tok = m.group(1).lower()
    return WORDNUM.get(tok, tok if not tok.isdigit() else int(tok))


def parse_definition(text):
    rec = {"raw": text}
    stat_mods = []
    for stat, sign, mag, pct in STAT_RE.findall(text):
        stat_mods.append({"stat": STAT_ALIASES.get(stat.upper().strip(), stat.upper()),
                          "value": (1 if sign == "+" else -1) * int(mag),
                          "unit": "pct" if pct else "flat"})
    for label, sign, mag in DMG_MOD_RE.findall(text):
        stat_mods.append({"stat": DMG_MOD_KEY[label.lower().strip()],
                          "value": (1 if sign == "+" else -1) * int(mag), "unit": "pct"})
    for verb, label, mag in DMG_MOD_BY_RE.findall(text):
        sign = 1 if verb.lower() == "increase" else -1
        stat_mods.append({"stat": DMG_MOD_KEY[label.lower().strip()],
                          "value": sign * int(mag), "unit": "pct"})
    for label, verb, mag in DMG_MOD_SUBJ_RE.findall(text):
        sign = 1 if verb.lower().startswith("increase") else -1
        stat_mods.append({"stat": DMG_MOD_KEY[label.lower().strip()],
                          "value": sign * int(mag), "unit": "pct"})
    if stat_mods:
        rec["stat_mods"] = stat_mods

    dur = _duration(text)
    if dur is not None:
        rec["duration"] = dur
    stk = STACK_RE.search(text)
    if stk:
        rec["max_stacks"] = int(stk.group(1) or stk.group(2))

    for regex, key in ((SHIELD_RE, "shield_pct_atk"), (HEAL_RE, "heal_pct_maxhp"),
                       (PCT_ATK_RE, "tick_pct_atk")):
        m = regex.search(text)
        if m:
            rec[key] = int(m.group(1))

    flags = []
    for pattern, flag in FLAG_PHRASES:
        if re.search(pattern, text) and flag not in flags:
            flags.append(flag)
    # Qualitative damage-taken change with NO percent (e.g. Stun "increases its damage
    # taken"), only when it wasn't already captured as a numeric stat_mod.
    if not any(m.get("stat") == "damage_taken" for m in stat_mods):
        if re.search(r"increases?[\w' ]*damage taken", text):
            flags.append("damage_taken_up")
        elif re.search(r"reduces?[\w' ]*damage taken", text):
            flags.append("damage_taken_down")
    if flags:
        rec["flags"] = flags

    rec["parsed"] = bool(stat_mods or dur is not None or flags
                         or any(k in rec for k in ("shield_pct_atk", "heal_pct_maxhp",
                                                   "tick_pct_atk")))
    return rec


def main():
    sys.path.insert(0, SERVER)
    import design_data as dd                                         # noqa: E402
    skill_rows = dd.rows("skill")

    applied = set()
    defs = defaultdict(lambda: defaultdict(int))
    for row in skill_rows.values():
        for field in ("_note1_en", "_note2_en"):
            body = clean(row.get(field))
            for m in APPLY_RE.finditer(body):
                nm = m.group(1).strip()
                # Skip pure targeting words the verb-object regex sometimes grabs
                # ("grants all allies ..." -> "allies"); they are not status names.
                if nm.lower() in ("allies", "enemies", "all allies", "all enemies",
                                  "all enemy targets", "all enemy", "the caster",
                                  "all damage"):
                    continue
                applied.add(nm)
            for line in body.split("\n"):
                m = DEF_RE.match(line.strip())
                if not m:
                    continue
                name = m.group(1).strip()
                if name.split()[0] in ("Deals", "After", "Before", "When", "If",
                                       "Unlock", "Base"):
                    continue
                defs[name][m.group(2).strip()] += 1

    catalog, dropped = {}, 0
    for name, variants in defs.items():
        canonical, _ = max(variants.items(), key=lambda kv: kv[1])
        rec = parse_definition(canonical)
        # A genuine status: applied somewhere, OR its own definition is mechanical.
        if name not in applied and not rec["parsed"]:
            dropped += 1
            continue
        rec["occurrences"] = sum(variants.values())
        rec["variant_count"] = len(variants)
        rec["applied"] = name in applied
        catalog[name] = rec

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(catalog, f, ensure_ascii=False, indent=1, sort_keys=True)

    parsed = sum(1 for r in catalog.values() if r["parsed"])
    print(f"{len(catalog)} statuses -> {OUT}  ({dropped} name-only lines dropped as noise)")
    print(f"  {parsed} parsed into structured records "
          f"({100 * parsed // max(len(catalog), 1)}%), "
          f"{len(catalog) - parsed} raw-only")


if __name__ == "__main__":
    main()

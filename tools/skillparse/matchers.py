"""The rules: triggers, segment splitting, no-op recognition, and the per-segment op
matchers. THIS is the file that grows as skills are recreated (new patterns, and later
the condition grammar) -- add matchers here, keep text/targets/driver stable. Every op a
matcher emits must have a handler in battle_effects.ops (asserted by
server/test_battle_effects.py). See docs/BATTLE_SKILL_PLAN.md."""
import json
import re

from .targets import target_of
from .text import WORDNUM

# Verb forms of a status ("stuns" = apply Stun). The cross-check against `_actID`
# (tools/check_skill_parse.py) says this is the single biggest parse gap: 564 flags
# where the prose uses the status as a VERB, led by taunt (286) and charm (52), which
# were simply absent from this map.
VERB_STATUS = {"stun": "Stun", "freeze": "Freeze", "silence": "Silence",
               "poison": "Poison", "burn": "Burn", "seal": "Seal", "daze": "Daze",
               "taunt": "Taunt", "charm": "Charm"}
# What may follow the verb for it to read as an action on somebody: "stun ONE RANDOM
# enemy", "taunt THE enemy with the highest ATK", "charm 2 enemies". Without this the
# pattern only caught "stuns" and "stun the", missing most of the real phrasings --
# and it must NOT catch "removes Taunt FROM all allies" or "immunity to Freeze".
VERB_OBJECT = (r"(?=\s+(?:the|all|one|two|three|a|an|\d+|random|another|"
               r"target|enem|ally|allies))")

DUR_RE = re.compile(r"for (\d+|" + "|".join(WORDNUM) + r") turns?", re.I)
CHANCE_RE = re.compile(r"(\d+)% (?:fixed )?chance to", re.I)


def _dur(text):
    m = DUR_RE.search(text)
    if not m:
        return None
    t = m.group(1).lower()
    return WORDNUM.get(t, int(t) if t.isdigit() else t)


# --- clause-level trigger prefixes -----------------------------------------
TRIGGERS = [
    (re.compile(r"^before (?:the )?(?:attack )?action[,:]?\s*", re.I), "before_action"),
    (re.compile(r"^before (?:dealing )?(?:the )?attack(?: action)?[,:]?\s*", re.I),
     "before_action"),
    (re.compile(r"^when (?:a|the) turn starts[,:]?\s*", re.I), "before_action"),
    (re.compile(r"^at the start of a turn[,:]?\s*", re.I), "before_action"),
    (re.compile(r"^while taking damage[,:]?\s*", re.I), "on_counter"),
    (re.compile(r"^after attack(?:ing)?[,:]?\s*", re.I), "after_attack"),
    (re.compile(r"^after (?:an? )?attack[,:]?\s*", re.I), "after_attack"),
    (re.compile(r"^after (?:the )?(?:action|dealing damage)[,:]?\s*", re.I), "after_action"),
    (re.compile(r"^when (?:a|the) battle starts[,:]?\s*", re.I), "battle_start"),
    (re.compile(r"^when taking (?:enemy )?counterattack[,:]?\s*", re.I), "on_counter"),
]

# In-attack context riders that add no trigger of their own ("When attacking, if ...");
# stripped so the 'if' underneath is visible. The trigger stays on_use.
RIDER_PREFIX_RE = re.compile(
    r"^(?:when attacking|while attacking|while dealing (?:damage|attacks?)|"
    r"while (?:the caster is )?dealing damage|when dealing damage)[,:]?\s*", re.I)


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


# Clauses that carry NO executable effect -- status metadata (duration/stack/removability
# restatements), damage-cap footnotes, trigger caveats, and split noise. Consuming these
# keeps an otherwise fully-parsed skill from being marked incomplete over a stray
# "Lasts for 2 turns." An empty-effect skill still stays incomplete (parse_skill requires
# got > 0), so this never fakes a do-nothing skill into `complete`.
NOOP_RES = [
    re.compile(r"^\(?\s*unremovable\s*\)?\.?$", re.I),
    re.compile(r"^\s*lasts\s+(?:for\s+\d+\s+turns?|the\s+entire\s+battle)"
               r"(?:\s+and\s+(?:un)?removable)?\.?$", re.I),
    re.compile(r"cannot be cleansed", re.I),
    re.compile(r"stacks?\s+up\s+to\s+\d+", re.I),
    re.compile(r"will not stack", re.I),
    re.compile(r"^\(?\s*(?:un)?stackable\s*\)?\.?$", re.I),
    re.compile(r"^\(?\s*(?:un)?carried\s*\)?\.?$", re.I),
    re.compile(r"will not exceed", re.I),            # damage-cap footnote
    re.compile(r"will not trigger on", re.I),        # trigger caveat
    re.compile(r"triggers once while", re.I),
    re.compile(r"^turns?\s*/?$", re.I),
]


def is_noop_clause(clause):
    """A clause with no executable effect: pure metadata/caveat/noise (see NOOP_RES),
    or a fragment with no 3+ letter word (bare numbers, punctuation, '*')."""
    c = clause.strip()
    if not re.search(r"[A-Za-z]{3,}", c):
        return True
    return any(r.search(c) for r in NOOP_RES)


def parse_segment(text, trigger, catalog):
    """Parse ONE effect segment (local target/chance scope) -> list of effect dicts."""
    effects = []
    base = {"trigger": trigger}
    ch = CHANCE_RE.search(text)
    if ch:
        base["chance"] = int(ch.group(1))

    # damage, two word orders:
    #  "deals X% ATK as damage [N times]"
    #  "deals damage [on <t>] by X% ATK [N times]"
    for m in re.finditer(r"deal(?:s|ing)?\s+(\d+)%\s+ATK\s+as\s+(?:pursuit\s+|extra\s+)?"
                         r"damage(?:\s+(\d+|two|three)\s+times?)?", text, re.I):
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
    # extra ATK damage: "deals X% extra ATK as damage" -> ordinary ATK-scaled hit.
    for m in re.finditer(r"deals?\s+(\d+)%\s+extra\s+ATK\s+as\s+damage", text, re.I):
        effects.append({**base, "op": "damage", "pct_atk": int(m.group(1)), "times": 1,
                        "target": target_of(text) or "enemy_target"})
    # DEF-scaled damage: "deals X% DEF as damage [N times]" (caster's DEF).
    for m in re.finditer(r"deals?\s+(\d+)%\s+DEF\s+as\s+damage"
                         r"(?:\s+(\d+|two|three)\s+times)?", text, re.I):
        t = m.group(2)
        effects.append({**base, "op": "damage", "pct_def": int(m.group(1)),
                        "times": WORDNUM.get((t or "").lower(),
                                             int(t) if t and t.isdigit() else 1),
                        "target": target_of(text) or "enemy_target"})
    # HP-based absolute damage: "deals X% HP-based absolute damage" (% of target Max HP).
    for m in re.finditer(r"deals?\s+(\d+)%?\s+HP-based\s+absolute\s+damage", text, re.I):
        effects.append({**base, "op": "damage", "pct_target_maxhp": int(m.group(1)),
                        "times": 1, "target": target_of(text) or "enemy_target"})
    # "% Max HP as damage" + optional chance-status: "Deals X% MAX HP as damage with a
    # Y% chance of inflicting <Status> on the target." Damage is % of the target's Max HP
    # (ignores DEF); the status is chance-gated (the dispatcher rolls eff["chance"]).
    for m in re.finditer(r"deals?\s+(\d+)%\s+MAX\s+HP\s+as\s+damage"
                         r"(?:\s+with\s+a\s+(\d+)%\s+chance\s+of\s+inflicting\s+"
                         r"([A-Za-z][A-Za-z ]+?)\s+on\s+"
                         r"(the target|the enemy[\w ]*|all enemies))?", text, re.I):
        tgt = target_of(m.group(4) or text) or "enemy_target"
        effects.append({**base, "op": "damage", "pct_target_maxhp": int(m.group(1)),
                        "times": 1, "target": tgt})
        status = (m.group(3) or "").strip()
        if m.group(2) and status in catalog:
            effects.append({**base, "op": "apply_status", "status": status,
                            "chance": int(m.group(2)), "target": tgt,
                            "duration": _dur(text)})
    # move gauge, verb phrasing: "increases/decreases the Move Gauge of <t> by N[%]"
    for m in re.finditer(r"(increase|decrease)s?\s+the\s+Move\s+Gauge\s+of\s+(.+?)\s+by\s+(\d+)",
                         text, re.I):
        sign = 1 if m.group(1).lower() == "increase" else -1
        effects.append({**base, "op": "move_gauge", "pct": sign * int(m.group(3)),
                        "target": target_of(m.group(2)) or "self"})
    # crowd-control immunity: "gains immunity to (all) crowd control / all control effects"
    if re.search(r"immunity to\s+(?:all\s+)?(?:crowd control|control effects)", text, re.I):
        effects.append({**base, "op": "immunity", "status": "CrowdControl",
                        "duration": _dur(text) or "battle", "target": target_of(text) or "self"})
    # stat shorthand: "ATK+30%", "MAX HP+3000", "Basic SPD+15", "DEF-20%" (buff = self).
    for m in re.finditer(r"\b(MAX HP|Basic SPD|ATK|DEF|SPD|HP|CRIT)\s*([+\-])\s*(\d+)(%?)",
                         text, re.I):
        raw = m.group(1).upper()
        stat = "HP" if "HP" in raw else ("SPD" if "SPD" in raw else raw)
        effects.append({**base, "op": "stat_mod", "stat": stat,
                        "pct": (1 if m.group(2) == "+" else -1) * int(m.group(3)),
                        "unit": "pct" if m.group(4) else "flat",
                        "duration": _dur(text), "target": target_of(text) or "self"})
    # skill-unlock passive stat: "Gains ability increase when unlocking skill: Base <STAT>
    # Stat +N" -- a permanent always-on buff, so it fires at battle_start (the passive
    # path), on self, flat.
    m = re.search(r"ability increase when unlocking skill:\s*Base\s+(ATK|DEF|HP|SPD)\s+Stat"
                  r"\s*([+\-])\s*(\d+)", text, re.I)
    if m:
        effects.append({"trigger": "battle_start", "op": "stat_mod",
                        "stat": m.group(1).upper(),
                        "pct": (1 if m.group(2) == "+" else -1) * int(m.group(3)),
                        "unit": "flat", "duration": "battle", "target": "self"})
    # heal: "restores X% of Max HP"
    m = re.search(r"restores?\s+(\d+)%\s+of\s+Max\s+HP", text, re.I)
    if m:
        effects.append({**base, "op": "heal", "pct_maxhp": int(m.group(1)),
                        "target": target_of(text) or "self"})
    # heal: "recovers 15% of the caster's Max HP"
    m = re.search(r"(?:restores?|recovers?)\s+(\d+)%\s+of\s+(?:the\s+)?(.+?)'\s*s?\s+Max\s+HP",
                  text, re.I)
    if m:
        effects.append({**base, "op": "heal", "pct_maxhp": int(m.group(1)),
                        "target": target_of(m.group(2)) or "self"})
    # heal, by-phrasing: "restores/recovers <t>'(s) HP by X% [of the caster's (Max) HP]".
    # Plain % and "of ... Max HP" scale off the caster's Max HP; "of the caster's HP"
    # scales off the caster's CURRENT HP (pct_caster_hp).
    m = re.search(r"(?:restores?|recovers?)\s+(.+?)'\s*s?\s+HP\s+by\s+(\d+)%"
                  r"(\s+of\s+the\s+caster'\s*s\s+(Max\s+)?HP)?", text, re.I)
    if m:
        key = "pct_caster_hp" if (m.group(3) and not m.group(4)) else "pct_maxhp"
        effects.append({**base, "op": "heal", key: int(m.group(2)),
                        "target": target_of(m.group(1)) or "self"})
    # skill CD: "increases the Skill CD of <t> by N" / "reduce ... Skill CD ... by N"
    m = re.search(r"(increase|reduce|decrease)s?\s+the\s+Skill\s+CD\s+of\s+(.+?)\s+by\s+(\d+)",
                  text, re.I)
    if m:
        sign = 1 if m.group(1).lower() == "increase" else -1
        effects.append({**base, "op": "skill_cd", "delta": sign * int(m.group(3)),
                        "target": target_of(m.group(2)) or "enemy_target"})
    # skill CD, possessive: "increases the target's Skill CD by N" / "its skill CD by N"
    m = re.search(r"(increase|reduce|decrease)s?\s+(.+?)(?:'s|s')\s+[Ss]kill\s+CD\s+by\s+(\d+)",
                  text, re.I)
    if m:
        sign = 1 if m.group(1).lower() == "increase" else -1
        effects.append({**base, "op": "skill_cd", "delta": sign * int(m.group(3)),
                        "target": target_of(m.group(2)) or
                        ("self" if "caster" in m.group(2).lower() else "enemy_target")})
    # move gauge, gain phrasing: "gains 100% Move Gauge" (the caster's own)
    m = re.search(r"gains?\s+(\d+)%\s+Move\s+Gauge", text, re.I)
    if m:
        effects.append({**base, "op": "move_gauge", "pct": int(m.group(1)),
                        "target": "self"})
    # buff/debuff strip: "removes all buffs from <t>" (unremovable-excluding caveat is
    # implicit -- the engine's classifier only ever strips removable classes)
    m = re.search(r"removes?\s+(?:all\s+)?(?:the\s+)?(buffs?|debuffs?)\s+from\s+(.+)",
                  text, re.I)
    if m:
        cls = "_buff" if m.group(1).lower().startswith("buff") else "_debuff"
        effects.append({**base, "op": "cleanse_class", "cls": cls,
                        "target": target_of(m.group(2)) or "enemy_target"})
    # stacked status: "inflicts 1 (more) stack(s) of <Name> on <t>" /
    # "grants the caster 2 stacks of Spirit"
    m = re.search(r"inflicts?\s+(\d+)\s+(?:more\s+)?stacks?\s+of\s+([A-Z][A-Za-z' ]+?)"
                  r"\s+on\s+(the target|it|all enemies|the caster)", text)
    if m and m.group(2).strip() in catalog:
        effects.append({**base, "op": "apply_status", "status": m.group(2).strip(),
                        "stacks": int(m.group(1)),
                        "target": target_of(m.group(3)) or "enemy_target",
                        "duration": _dur(text)})
    m = re.search(r"grants?\s+(the caster|all allies|the target)\s+(\d+|" +
                  "|".join(WORDNUM) + r")\s+stacks?\s+of\s+([A-Z][A-Za-z' ()]+?)(?:[.,]|$)",
                  text, re.I)
    if m:
        names = [n.strip() for n in re.split(r"\s+and\s+|,\s*", m.group(3)) if n.strip()]
        n = m.group(2).lower()
        stacks = WORDNUM.get(n, int(n) if n.isdigit() else 1)
        for nm in names:
            if nm in catalog:
                effects.append({**base, "op": "apply_status", "status": nm,
                                "stacks": stacks, "target": target_of(m.group(1)) or "self",
                                "duration": _dur(text)})
    # class cleanse by name: "removes [healing over time] statuses from <t>" /
    # "remove the DoT status from all allies"
    m = re.search(r"removes?\s+(?:the\s+)?\[?(healing over time|damage over time|DoT|HoT)\]?"
                  r"\s+status(?:es)?\s+from\s+(.+)", text, re.I)
    if m:
        cls = "_hot" if m.group(1).lower() in ("healing over time", "hot") else "_dot"
        effects.append({**base, "op": "cleanse_class", "cls": cls,
                        "target": target_of(m.group(2)) or "enemy_target"})
    # possessive buff strip: "removes the target's buffs"
    m = re.search(r"removes?\s+(.+?)(?:'s|s')\s+(buffs?|debuffs?)", text, re.I)
    if m:
        cls = "_buff" if m.group(2).lower().startswith("buff") else "_debuff"
        effects.append({**base, "op": "cleanse_class", "cls": cls,
                        "target": target_of(m.group(1)) or "enemy_target"})
    # bare move gauge: "increase Move Gauge by N%" (the caster's own)
    m = re.search(r"(increase|decrease)s?\s+(?:the\s+)?Move\s+Gauge\s+by\s+(\d+)%", text, re.I)
    if m and "of" not in text[max(0, m.start() - 1):m.end() + 4].lower():
        sign = 1 if m.group(1).lower() == "increase" else -1
        effects.append({**base, "op": "move_gauge", "pct": sign * int(m.group(2)),
                        "target": target_of(text[:m.start()]) or "self"})
    # bare status gain: "gains All DMG Reduction (2 turns)" -- name must be in the
    # catalog, so ordinary prose never matches.
    m = re.search(r"gains?\s+([A-Z][A-Za-z' ]+?)(?:\s*\((\d+)\s*turns?\))?(?:[.,]|$)", text)
    if m and m.group(1).strip() in catalog and "immunity" not in m.group(1).lower():
        effects.append({**base, "op": "apply_status", "status": m.group(1).strip(),
                        "target": "self",
                        "duration": int(m.group(2)) if m.group(2) else _dur(text)})
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
        # Either an explicit object ("stun ONE random enemy") or the plural form on its
        # own ("inflicts stuns and increases...", "freezes and reduces..."), which is
        # what the original pattern caught and the object form alone would drop.
        vm = (re.search(rf"\b{verb}(?:s|es)\b", text, re.I)
              or re.search(rf"\b{verb}\b" + VERB_OBJECT, text, re.I))
        if vm:
            # target sits right after the verb ("Freeze the target"), not earlier in
            # the segment ("the caster has a chance to Freeze the target").
            effects.append({**base, "op": "apply_status", "status": status,
                            "target": target_of(text[vm.end():]) or "enemy_target",
                            "duration": _dur(text)})
            # no break: "stun and charm" is two effects, and stopping at the first
            # silently dropped the rest
    # apply status: "inflicts/grants <A> [and <B>] on/to <target> [for N turns]"
    m = re.search(r"(?:inflicts?|grants?|inflict|grant)\s+(?:the\s+\w+\s+|all\s+\w+\s+)?"
                  r"(.+?)(?:\s+(?:on|to)\s+(.+?))?(?:\s+for\s+\d+\s+turns?)?[.]?$", text, re.I)
    if m:
        # split "Freeze and Fragile" / "Gale on all allies and Slow on all enemies"
        chunk = m.group(1)
        pairs = re.findall(r"([A-Z][A-Za-z ]+?)\s+on\s+(all allies|all enemies|the target|the enemy[\w ]*)",
                           text)
        if pairs:
            for names, tgt in pairs:
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
            seen.add(k)
            uniq.append(e)
    return uniq

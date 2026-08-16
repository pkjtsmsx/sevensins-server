"""Condition grammar: an 'If ...' gate -> a machine-evaluable cond dict (Phase 2).

Each cond dict's "kind" must have an evaluator in server/battle_effects/conditions.py
(lock-step asserted by server/test_battle_effects.py). parse_condition returns None for
anything it doesn't fully understand -- the driver then leaves the whole clause unparsed
rather than firing an effect on a guessed gate.

Cond shapes:
  {"kind": "hp",  "subject": "self"|"target", "cmp": "gt|ge|lt|le", "pct": N}
  {"kind": "hp_vs", "cmp": "gt"|"lt"}                    # caster's HP vs the target's
  {"kind": "status", "subject": "self|target|any_enemy|any_ally", "names": [...],
   "negate": bool, "min_stacks": N?}   # names may be the classes _buff/_debuff/_dot/_hot
  {"kind": "cast_type", "subject": "target", "type": "STR"|"AGI"|"TEC"}
  {"kind": "crit"}                                       # env-driven; server has no crits yet
  {"kind": "kill", "negate": bool}                       # this attack defeated someone
  {"kind": "turn_parity", "parity": "odd"|"even"}
  {"kind": "turn_cmp", "cmp": "gt|ge|lt|le", "n": N}
  {"kind": "alive", "subject": "target"}
  {"kind": "all"|"any", "conds": [...]}
"""
import re

from .text import WORDNUM

# STR/AGI/TEC <- char _job 2/3/4: CommonUtil.GetJobUseText (0x17C0188) renders the
# job-gated rune texts via GetText(job + 12099), and text rows 12101/12102/12103 are
# the STR/AGI/TEC templates. "Tech"/"Technique" are prose variants of TEC.
CAST_TYPES = {"str": "STR", "strength": "STR",
              "agi": "AGI", "agility": "AGI", "speed": "AGI",
              "tec": "TEC", "tech": "TEC", "technique": "TEC", "skill": "TEC"}

# Context riders that decorate a condition without changing what it tests ("while
# dealing damage", "when being attacked", ...). Stripped from both ends.
RIDER_RE = re.compile(
    r"(?:^|,?\s+)(?:while|when|before|after|during)\s+"
    r"(?:the\s+caster\s+is\s+)?(?:dealing\s+|taking\s+)?(?:multiple\s+)?"
    r"(?:attack(?:s|ing)?(?:\s+action)?|damage(?:\s+on\s+(?:it|the\s+target))?|"
    r"action|the\s+action|an?\s+action|"
    r"the\s+attack|being\s+attacked|attacked|hit|the\s+battle|the\s+turn|"
    r"a\s+turn\s+starts?|the\s+turn\s+starts?|battle|pursuing)\s*(?=,|$)", re.I)

_STACKS_RE = re.compile(
    r"^(?:at\s+least\s+)?(\d+|" + "|".join(WORDNUM) + r")\s+(?:or\s+more\s+)?"
    r"stacks?\s+of\s+", re.I)


def _num(tok):
    tok = tok.lower()
    return WORDNUM.get(tok, int(tok) if tok.isdigit() else None)


# Adjective forms of a status gate: "if the target is STUNNED" means "is affected by
# Stun". Only the unambiguous ones -- these read as states, never as verbs, in a gate.
STATUS_ADJECTIVES = {
    "stunned": "Stun", "frozen": "Freeze", "charmed": "Charm", "dazed": "Daze",
    "silenced": "Silence", "sealed": "Seal", "poisoned": "Poison", "burned": "Burn",
    "taunted": "Taunt", "petrified": "Petrify", "confused": "Confuse",
}
ADJECTIVE_RE = re.compile(r"\bis\s+(not\s+)?(" + "|".join(STATUS_ADJECTIVES) + r")\b",
                          re.I)


def _expand_adjectives(text):
    def sub(m):
        neg = m.group(1) or ""
        return f"is {neg}affected by {STATUS_ADJECTIVES[m.group(2).lower()]}"
    return ADJECTIVE_RE.sub(sub, text)


def _strip(text):
    t = _expand_adjectives(text.strip())
    t = re.sub(r"^if\b\s*", "", t, flags=re.I)
    while True:
        t2 = RIDER_RE.sub("", t).strip(" ,.-")
        if t2 == t:
            return t
        t = t2


def _subject(word):
    w = (word or "").lower()
    if w in ("caster", "yourself", "self", "caster's", "it will"):
        return "self"
    return "target"


def _status_names(chunk):
    """'removable [DoT]' / 'Freeze, Charm and Elite' / 'any buffs status' -> class or
    name list. Returns None if the chunk doesn't read as statuses."""
    c = re.sub(r"\s*\((?:excluding[^)]*)\)", "", chunk)     # "(excluding ...)" caveats
    c = re.sub(r"^status(?:es)?\s+like\s+", "", c.strip(" ,."), flags=re.I)
    low = c.lower()
    if re.fullmatch(r"\[?healing\s+over\s+time\]?(?:\s+effects?)?", low):
        return ["_hot"]
    if re.fullmatch(r"\[?damage\s+over\s+time\]?(?:\s+effects?)?", low):
        return ["_dot"]
    if re.fullmatch(r"(?:any\s+)?(?:removable\s+)?buffs?(?:\s+status(?:es)?)?(?:\s+effects?)?", low):
        return ["_buff"]
    if re.fullmatch(r"(?:any\s+)?(?:removable\s+(?:and\s+unstackable\s+)?|unstackable\s+)?"
                    r"debuffs?(?:\s+status(?:es)?)?(?:\s+effects?)?", low):
        return ["_debuff"]
    if re.fullmatch(r"(?:removable\s+)?\[?dot\]?(?:\s+effects?)?", low):
        return ["_dot"]
    if re.fullmatch(r"(?:removable\s+)?\[?hot\]?(?:\s+effects?)?", low):
        return ["_hot"]
    names = []
    for nm in re.split(r"\s+and\s+|,\s*", c):
        nm = nm.strip().strip("[]").strip()
        nm = re.sub(r"^(?:removable|the)\s+", "", nm, flags=re.I)
        # A status name: capitalized word(s), no verbs/digits.
        if not nm or not re.fullmatch(r"[A-Z][A-Za-z'! ()]*", nm):
            return None
        names.append(nm)
    return names or None


CMP = {"above": "gt", "over": "gt", "higher than": "gt", "greater than": "gt",
       "more than": "gt", "at least": "ge", "below": "lt", "under": "lt",
       "lower than": "lt", "less than": "lt", "lesser than": "lt", "at most": "le"}


def _cmp_with_includes(word, inclusive):
    c = CMP.get(word.lower())
    if c and inclusive:
        c = {"gt": "ge", "lt": "le"}.get(c, c)
    return c


def _atom(t):
    # HP shorthand: "HP>90%" (bare = the caster) / "the target's HP <=30%"
    m = re.fullmatch(r"(?:(?:the\s+)?(caster|target)'?\s*s?\s+)?HP\s*([<>]=?)\s*(\d+)%?",
                     t, re.I)
    if m:
        cmp_ = {">": "gt", ">=": "ge", "<": "lt", "<=": "le"}[m.group(2)]
        return {"kind": "hp", "subject": _subject(m.group(1) or "caster"),
                "cmp": cmp_, "pct": int(m.group(3))}
    # "the caster's HP is full"
    m = re.fullmatch(r"(?:the\s+)?(caster|target)'s\s+HP\s+is\s+full", t, re.I)
    if m:
        return {"kind": "hp", "subject": _subject(m.group(1)), "cmp": "ge", "pct": 100}
    # "the caster's HP is higher/lower than the target's"
    m = re.fullmatch(r"(?:the\s+)?caster's\s+HP\s+is\s+(higher|lower)\s+than\s+"
                     r"(?:the\s+)?target's?", t, re.I)
    if m:
        return {"kind": "hp_vs", "cmp": "gt" if m.group(1).lower() == "higher" else "lt"}
    # HP threshold: "the caster's HP is above 50% (includes 50%)"
    m = re.fullmatch(r"(?:the\s+|a\s+)?(caster|target)(?:\(s\))?'\s*s?\s+HP\s+is\s+"
                     r"(?:still\s+)?(above|over|higher than|greater than|at least|below|"
                     r"under|lower than|less than|at most)(\s+or\s+equal\s+to)?\s+(\d+)%?"
                     r"(\s*\(includ\w*\s*\d+%?\))?", t, re.I)
    if m:
        cmp_ = _cmp_with_includes(m.group(2), bool(m.group(3) or m.group(5)))
        if cmp_:
            return {"kind": "hp", "subject": _subject(m.group(1)), "cmp": cmp_,
                    "pct": int(m.group(4))}
    # cast type: "the target is a STR Type cast" / "the target is TEC Type" / "[Tech] Type"
    m = re.fullmatch(r"(?:the\s+target\s+is\s+)?(?:an?\s+)?\[?(\w+)\]?\s+Type(?:\s+cast)?",
                     t, re.I)
    if m and m.group(1).lower() in CAST_TYPES:
        return {"kind": "cast_type", "subject": "target",
                "type": CAST_TYPES[m.group(1).lower()]}
    # elite: "the target is an Elite" (an elite-mob marker, checked as status/flag)
    if re.fullmatch(r"(?:the\s+(?:pursued\s+)?target\s+is\s+)?(?:an?\s+)?\[?Elite\]?", t, re.I):
        return {"kind": "status", "subject": "target", "names": ["Elite"], "negate": False}
    if re.fullmatch(r"any\s+of\s+the\s+enem(?:y|ies)\s+(?:is|are)\s+(?:an?\s+)?\[?Elite\]?",
                    t, re.I):
        return {"kind": "status", "subject": "any_enemy", "names": ["Elite"], "negate": False}
    # "any of the enemy targets with Elite effect"
    m = re.fullmatch(r"any\s+of\s+the\s+enemy\s+targets?\s+with\s+(?:the\s+)?(.+?)\s+effects?",
                     t, re.I)
    if m:
        names = _status_names(m.group(1).strip())
        if names:
            return {"kind": "status", "subject": "any_enemy", "names": names,
                    "negate": False}
    # "this attack damages a target affected by <Status>"
    m = re.fullmatch(r"(?:this|the)\s+(?:attack|skill)\s+damages?\s+a\s+target\s+"
                     r"(?:affected|affeted)\s+by\s+(.+)", t, re.I)
    if not m:
        m = re.fullmatch(r"a\s+target\s+damaged\s+by\s+this\s+(?:attack|skill)\s+is\s+"
                         r"(?:affected|affeted)\s+by\s+(.+)", t, re.I)
    if m:
        names = _status_names(m.group(1).strip())
        if names:
            return {"kind": "status", "subject": "target", "names": names, "negate": False}
    # stack build-up: "Wrath has stacked up to 5" / "the caster's Wrath effect has
    # stacked up to 5" / "the caster has already stacked Wrath up to 5 times"
    m = re.fullmatch(r"(?:the\s+caster'?\s*s\s+)?([A-Z][A-Za-z'! ]*?)(?:\s+effect)?\s+has\s+"
                     r"(?:already\s+)?stacked\s+up\s+to\s+(\d+)(?:\s+times)?", t)
    if not m:
        m = re.fullmatch(r"the\s+caster\s+has\s+(?:already\s+)?stacked\s+"
                         r"([A-Z][A-Za-z'! ]*?)\s+up\s+to\s+(\d+)(?:\s+times)?", t)
    if m:
        return {"kind": "status", "subject": "self", "names": [m.group(1).strip()],
                "negate": False, "min_stacks": int(m.group(2))}
    # crit: "this attack is a critical hit" / "the attack is Critical" / "it is a critical hit"
    if re.fullmatch(r"(?:this|the|the\s+current|current|it)\s+(?:attack\s+)?is\s+"
                    r"(?:a\s+)?[Cc]ritical(?:\s+[Hh]it)?", t, re.I):
        return {"kind": "crit"}
    # "the caster attacks a target with the Fallen Angel Mark" -- a status gate on the
    # TARGET, written from the attacker's side. 19 skills, all of them damage riders.
    m = re.fullmatch(r"(?:the\s+caster\s+)?attacks?\s+(?:a|an|the)\s+target\s+"
                     r"(?:with|carrying|affected\s+by)\s+(?:the\s+)?(.+)", t, re.I)
    if m:
        name = m.group(1).strip().rstrip(".")
        if name:
            return {"kind": "status", "subject": "target", "negate": False,
                    "names": [name]}
    # "the caster has no removable debuffs" -- a class gate, negated. The class names
    # (_buff/_debuff/_dot/_hot) are what the status gate already speaks.
    m = re.fullmatch(r"(?:the\s+)?(caster|target)\s+has\s+(no|any)\s+"
                     r"(?:removable\s+)?(buffs?|debuffs?)", t, re.I)
    if m:
        return {"kind": "status",
                "subject": "self" if m.group(1).lower() == "caster" else "target",
                "negate": m.group(2).lower() == "no",
                "names": ["_buff" if m.group(3).lower().startswith("buff")
                          else "_debuff"]}
    # unit_count: "there are still at least 3 enemies on the field",
    # "there are 2 enemies or below (includes 2)", "there are at most 2 allies left".
    # **"more than N ... (includes N)" means >= N**, not > N -- the parenthetical is the
    # game telling you the bound is inclusive, and 19 skills hang on it.
    m = re.fullmatch(
        r"there\s+are\s+(?:still\s+)?"
        r"(?:(at\s+least|more\s+than|at\s+most|less\s+than|fewer\s+than)\s+)?"
        r"(\d+|" + "|".join(WORDNUM) + r")\s+(enemies|enemy|allies|ally)"
        r"(?:\s+or\s+(below|above|more|less|fewer))?"
        r"(?:\s*\(includes?\s+\d+\))?"
        r"(?:\s+(?:left|surviv\w+|remain\w*|on\s+the\s+field(?:\s+surviv\w+)?))*",
        t, re.I)
    if m:
        n = _num(m.group(2))
        word, tail = (m.group(1) or "").lower().replace(" ", ""), (m.group(3) or "")
        side = "enemy" if m.group(3).lower().startswith("enem") else "ally"
        inclusive = "(include" in t.lower()
        cmp_ = {"atleast": "ge", "morethan": "ge" if inclusive else "gt",
                "atmost": "le", "lessthan": "lt", "fewerthan": "lt"}.get(word)
        if cmp_ is None:
            tail = (m.group(4) or "").lower()
            cmp_ = {"below": "le", "less": "le", "fewer": "le",
                    "above": "ge", "more": "ge"}.get(tail, "ge")
        if n is not None:
            return {"kind": "unit_count", "side": side, "cmp": cmp_, "n": n}
    # kill: "this attack defeats an enemy" / "fails to defeat the enemy"
    m = re.fullmatch(r"(?:this|the)\s+attack\s+(?:successfully\s+|sucessfully\s+)?"
                     r"(defeats?|does\s+not\s+defeat|doesn't\s+defeat|"
                     r"fails\s+to\s+defeat)\s+"
                     r"(?:an|the)\s+enemy", t, re.I)
    if m:
        return {"kind": "kill", "negate": "defeat" != m.group(1).lower()[:6]}
    if re.fullmatch(r"(?:the\s+)?(?:affected\s+)?target\s+is\s+defeated", t, re.I):
        return {"kind": "kill", "negate": False}
    if re.fullmatch(r"(?:the\s+)?(?:pursued\s+)?target\s+has\s+not\s+(?:been\s+)?"
                    r"defeated(?:\s+yet)?", t, re.I):
        return {"kind": "kill", "negate": True}
    # turn parity: "it is an odd numbered turn" / "the current total number of turns is odd"
    m = re.fullmatch(r"it\s+is\s+an?\s+(odd|even)(?:[- ]numbered)?\s+turn", t, re.I)
    if not m:
        m = re.fullmatch(r"(?:the\s+)?(?:current\s+|total\s+)*number\s+of\s+"
                         r"(?:current\s+|total\s+)*turns\s+is\s+(odd|even)", t, re.I)
    if m:
        return {"kind": "turn_parity", "parity": m.group(1).lower()}
    # turn count: "the total number of turns is under 25 (includes 25)" /
    # "the total turns is less than 25" / "the number of total turns is 30 at most"
    m = re.fullmatch(r"(?:the\s+)?(?:current\s+|total\s+)*(?:number\s+of\s+)?"
                     r"(?:current\s+|total\s+)*turns\s+is\s+"
                     r"(above|over|more than|at least|below|under|less than|at most)\s+"
                     r"(\d+)(\s*\(includ\w*\s*\d+\))?", t, re.I)
    if m:
        cmp_ = _cmp_with_includes(m.group(1), bool(m.group(3)))
        if cmp_:
            return {"kind": "turn_cmp", "cmp": cmp_, "n": int(m.group(2))}
    m = re.fullmatch(r"(?:the\s+)?(?:total\s+)?(?:number\s+of\s+)?(?:total\s+)?turns\s+is\s+"
                     r"(\d+)\s+at\s+(most|least)", t, re.I)
    if m:
        return {"kind": "turn_cmp", "cmp": "le" if m.group(2).lower() == "most" else "ge",
                "n": int(m.group(1))}
    # alive: "the target is still alive"
    if re.fullmatch(r"(?:the\s+)?target\s+is\s+(?:still\s+)?alive", t, re.I):
        return {"kind": "alive", "subject": "target"}
    # "the target is freezed" (typo form of "affected by Freeze")
    if re.fullmatch(r"(?:the\s+)?target\s+is\s+freezed", t, re.I):
        return {"kind": "status", "subject": "target", "names": ["Freeze"], "negate": False}
    # "the target still has a shield"
    if re.fullmatch(r"(?:the\s+)?target\s+still\s+has\s+a\s+shield", t, re.I):
        return {"kind": "status", "subject": "target", "names": ["Shield"], "negate": False}
    # status: "<subj> is (not) (already) affected by <statuses>" / "carries the [X]" /
    # "<subj> has <Status>" / "<subj> is under the effect of <Status>"
    m = re.fullmatch(
        r"(?:the\s+)?(caster|target|it|pursued\s+target|any\s+of\s+(?:the\s+)?enem(?:y|ies)|"
        r"any\s+of\s+(?:the\s+)?all(?:y|ies)|any\s+of\s+(?:the\s+)?targets?|"
        r"none\s+of\s+(?:the\s+)?all(?:y|ies)|any\s+enemy|any\s+ally)"
        r"(?:\(s\))?\s+(?:is|are|was|were)\s+(not\s+)?(?:already\s+)?"
        r"(?:(?:affected|affeted)\s+(?:by\s+)?|under\s+the\s+effect\s+of\s+)(.+)", t, re.I)
    if not m:
        m = re.fullmatch(r"(?:the\s+)?(caster|target|it)\s+"
                         r"(?:carr(?:ies|y)|has)()\s+(?:the\s+)?(.+)", t, re.I)
    if m:
        subj_raw = m.group(1).lower()
        if "enem" in subj_raw or "target" in subj_raw and "any" in subj_raw:
            subject = "any_enemy"
        elif "all" in subj_raw:
            subject = "any_ally"
        elif subj_raw.startswith(("caster",)):
            subject = "self"
        else:
            subject = "target"
        negate = bool(m.group(2)) or subj_raw.startswith("none")
        chunk = m.group(3).strip()
        cond = {"kind": "status", "subject": subject, "negate": negate}
        sm = _STACKS_RE.match(chunk)
        if sm:
            n = _num(sm.group(1))
            if n:
                cond["min_stacks"] = n
            chunk = chunk[sm.end():]
        names = _status_names(chunk)
        if names:
            cond["names"] = names
            return cond
    return None


def parse_condition(text):
    """The gate text (with or without a leading 'if') -> a cond dict, or None."""
    t = _strip(text)
    if not t:
        return None
    got = _atom(t)
    if got:
        return got
    # top-level or / and between full sub-conditions ("TEC Type or Elite",
    # "still alive and is affected by Gale")
    for sep, kind in ((r"\s+or\s+", "any"), (r"\s+and\s+", "all")):
        parts = re.split(sep, t)
        if len(parts) > 1:
            subs = [_atom(_strip(p)) for p in parts]
            if all(subs):
                return {"kind": kind, "conds": subs}
    return None


def cond_kinds(cond):
    """Every 'kind' used inside a cond tree (for the lock-step assertion)."""
    out = {cond.get("kind")}
    for c in cond.get("conds", []):
        out |= cond_kinds(c)
    return out

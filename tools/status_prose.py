#!/usr/bin/env python3
"""Parse magnitude / duration / stacks out of one `* Name: body` glossary line.

Split out of the two compilers because both need it and they must agree: the status
registry uses it to sanity-check, and the skill compiler uses it to attach the real
numbers to each `apply_status` effect.

**Why this is per-skill and not per-status.** A type-6 status row has no magnitude or
duration column -- see docs/BATTLE_CLIENT_CONTRACT.md 5.1. The values belong to the
(skill, status) pair: Frozen Inferno Thorn keeps `act_id [6050, 2005, 4283, 4101, 0]`
identical from lv1 to lv6 while its Gash goes "+35%, two turns" -> "+40%, three turns".
Corpus-wide, 740 of 1,592 (status, skill-group) pairs change their glossary body across
levels. So there is nothing to cross-validate across skills, and reading one status's
line once and reusing it everywhere would be wrong 46% of the time.
"""
import re

WORD_NUM = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
            "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
_NUM = r"(\d+|" + "|".join(WORD_NUM) + r")"

# "lasting two turns." / "Lasts for 1 turn." / "for 3 turns" / "lasts forN turn" (sic --
# the pack drops the space often enough that requiring one loses real rows).
DURATION_RE = re.compile(
    r"(?:last(?:s|ing)?|for|during)\s*(?:for)?\s*" + _NUM + r"\s*turns?\b", re.I)
# "lasts the entire battle" is the pack's most common way of saying permanent, and it
# matches none of the patterns below on its own -- no "until", no "permanent", no "rest
# of". Missing it made The Fallen a 2-turn buff that the client counted straight down to
# zero and deleted, so Lucifer's stance swap dropped her marker instead of holding it.
PERMANENT_RE = re.compile(
    r"until the (?:end of (?:the )?)?(?:battle|stage)|permanent|for the rest of"
    r"|(?:entire|whole|full)\s+(?:battle|stage)", re.I)

# "DEF-50%", "Final damage dealt+40%", "ATK -30%", "+25%"
SIGNED_PCT_RE = re.compile(r"([+\-])\s*(\d+(?:\.\d+)?)\s*%")
BARE_PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")

# "stacks up to 7 times", "up to 10 stacks", "max 5 stacks"
STACKS_RE = re.compile(
    r"(?:stack(?:s|ing)?\s*up\s*to|up\s*to|max(?:imum)?(?:\s*of)?)\s*" + _NUM +
    r"\s*(?:times|stacks?)\b", re.I)

STAT_RE = re.compile(r"\b(ATK|DEF|SPD|HP|CRI|CRIT)\b", re.I)


def _num(tok):
    tok = (tok or "").strip().lower()
    return int(tok) if tok.isdigit() else WORD_NUM.get(tok)


def parse(body):
    """-> {duration, permanent, magnitude, magnitude_sign, stat, stacks, raw}.

    Any field the line does not state comes back None rather than a default. A default
    would be indistinguishable from a real value at runtime, and "this skill never said"
    is exactly the thing an engine has to be able to see.
    """
    out = {"duration": None, "permanent": False, "magnitude": None,
           "magnitude_sign": None, "stat": None, "stacks": None,
           "raw": (body or "").strip() or None}
    if not body:
        return out

    if PERMANENT_RE.search(body):
        out["permanent"] = True
    m = DURATION_RE.search(body)
    if m:
        out["duration"] = _num(m.group(1))

    # A signed percentage is the magnitude; an unsigned one is only trustworthy when the
    # line has exactly one, otherwise "deal 20% ... up to 7 times" would read the stack
    # count as the size of the effect.
    m = SIGNED_PCT_RE.search(body)
    if m:
        out["magnitude_sign"] = -1 if m.group(1) == "-" else 1
        out["magnitude"] = float(m.group(2))
    else:
        pcts = BARE_PCT_RE.findall(body)
        if len(pcts) == 1:
            out["magnitude"] = float(pcts[0])

    m = STACKS_RE.search(body)
    if m:
        out["stacks"] = _num(m.group(1))
    m = STAT_RE.search(body)
    if m:
        out["stat"] = m.group(1).upper().replace("CRIT", "CRI")
    return out


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


def norm_name(s):
    """Join key for matching a status row's name to its `* Name:` glossary line.

    Strips ANY trailing parenthetical, not just `(N)` and `(SP)`. Status rows carry
    qualifiers the glossary line does not repeat -- `Serum Injection(ATK)` and
    `Serum Injection(CRT)` are two rows sharing one `* Serum Injection:` entry, and
    `Admonition (Reduce CRT)` / `(Reduce SPD)` likewise. Without this they join to
    nothing and both lose the duration the prose plainly states.
    """
    s = re.sub(r"\s*\([^)]*\)\s*$", "", s or "")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s.lower()).split())

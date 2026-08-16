"""Text cleaning and structural splitting: colour-code stripping, inline status-def
glosses, named passive blocks, and sentence clauses. No effect semantics here."""
import re

COLOR_RE = re.compile(r"\[[0-9A-Fa-f]{6}\]|\[-\]")
WORDNUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
           "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}


# **Typos in the game's own EN text that hide a damage clause from the matchers.**
# Both were found by asking which skills promise "N% ATK as damage" in prose but parsed
# no damage op at all -- the answer was 58 skills that dealt their statuses and nothing
# else. "Luminous Vortex" (2037111..) is the reported one.
#   * "Delas 210% ATK as damage"        -- 33 skills, a straight misspelling of Deals
#   * "Deals damage 110% ATK as damage" -- 25 skills, a redundant "damage" before the
#     percentage. Anchored on a following digit so the legitimate
#     "deals damage on <target> by N% ATK" phrasing (its own matcher) is left alone.
# Misspellings of words the matchers key on, all counted across the shipped EN text:
#   aliies 38, affeted 25, alies 15, tatget 1
TYPO_FIXES = (
    (re.compile(r"\bdelas\b", re.I), "Deals"),
    (re.compile(r"\bali+es\b", re.I), "allies"),
    (re.compile(r"\btatget\b", re.I), "target"),
    (re.compile(r"\baffeted\b", re.I), "affected"),
    (re.compile(r"\b(deals?)\s+damage\s+(?=\d+\s*%)", re.I), r"\1 "),
)


def clean(text):
    text = COLOR_RE.sub("", text or "").replace("\n", " ").strip()
    for pattern, repl in TYPO_FIXES:
        text = pattern.sub(repl, text)
    return text


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


def split_clauses(body):
    """Sentence/segment split that keeps a trigger attached to what it governs."""
    return [p.strip() for p in re.split(r"(?<=[.;])\s+", body) if p.strip()]

"""Text cleaning and structural splitting: colour-code stripping, inline status-def
glosses, named passive blocks, and sentence clauses. No effect semantics here."""
import re

COLOR_RE = re.compile(r"\[[0-9A-Fa-f]{6}\]|\[-\]")
WORDNUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}


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


def split_clauses(body):
    """Sentence/segment split that keeps a trigger attached to what it governs."""
    return [p.strip() for p in re.split(r"(?<=[.;])\s+", body) if p.strip()]

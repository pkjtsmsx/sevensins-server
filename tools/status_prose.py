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


# ---- the ORIGINAL language ---------------------------------------------------------
#
# `_note1` is Chinese and `_note1_en` a translation, and the translation renames things
# mid-sentence: Scorpion Kiss's status row is `SPD UP(5)` in English while its own
# glossary line reads `*Boost Up:` -- so the English-only join above found nothing and
# the effect shipped with no magnitude, no stacks, no duration. The Chinese row is
# `加速(5)` and the line is `※ 加速：速度+5%，可疊加5次，持續3回合`. That shape -- a `※`
# marker, the status name, a full-width colon, and a fixed vocabulary for magnitude,
# stacking and duration -- is consistent enough across the pack to parse directly, and
# it is the text the game was written in (CLAUDE.md section 3). These are tried FIRST.

# 持續3回合 / 持續整場戰鬥 / 持續整個戰鬥 / 直到戰鬥結束 / 常駐
DURATION_ZH_RE = re.compile(r"持續\s*(\d+)\s*回合")
PERMANENT_ZH_RE = re.compile(r"持續整[場個]|整場戰鬥|直到戰鬥結束|常駐|持續到戰鬥結束")
# 攻擊力+18% / 速度-25% / 受到的傷害降低30% / 回復自身體力最大值25%
SIGNED_PCT_ZH_RE = re.compile(r"([+\-－])\s*(\d+(?:\.\d+)?)\s*[%％]")
DOWN_WORD_ZH = re.compile(r"降低|減少|下降")
UP_WORD_ZH = re.compile(r"提升|增加|提高|上升|回復|恢復")
# 可疊加5次 / 最多可堆疊3層 / 疊加至5層
STACKS_ZH_RE = re.compile(r"(?:疊加|堆疊)(?:至|到)?\s*(\d+)\s*[次層]|最多\s*(\d+)\s*[次層]")
STAT_ZH = (("攻擊力", "ATK"), ("防禦力", "DEF"), ("速度", "SPD"), ("體力", "HP"),
           ("爆擊率", "CRI"), ("暴擊率", "CRI"), ("會心率", "CRI"),
           ("爆擊傷害", "CDI"), ("暴擊傷害", "CDI"))
UNREMOVABLE_ZH_RE = re.compile(r"不可解除|不可清除|解除不可|無法解除|無法清除|不可移除")
# A percentage inside a CONDITION is a threshold, not a magnitude: "當前血量<90%時則立即
# 死亡" read as a 90% heal-over-tick is the worst case, and it happened. Anything from a
# 若/當 up to the next clause break, and any "<N%" / "低於N%" comparison, is blanked
# before the magnitude is looked for.
# 當 must not match inside 相當於 ("equivalent to"): "吸收相當於75%攻擊力" is a shield
# SIZE, and blanking it left every percent-sized shield with no number.
THRESHOLD_ZH_RE = re.compile(
    r"(?:若|(?<!相)當|如果)[^，。；]*?[%％][^，。；]*|[<>＜＞≤≥]\s*\d+(?:\.\d+)?\s*[%％]"
    r"|(?:低於|高於|不高於|不低於|超過|未滿)\s*\d+(?:\.\d+)?\s*[%％](?:以上|以下)?")


def parse_zh(body):
    """-> the same dict as parse(), read from a Chinese glossary body.

    Same contract: anything the line does not state is None. Sign comes from an explicit
    +/- first, then from the verb (降低/減少 -> -1, 提升/增加 -> +1) when the line has
    exactly one percentage -- a line like 造成攻擊力50%傷害 (a DoT sized in ATK) has no
    direction word and no sign, and stays sign-None for the engine's category rule.
    """
    out = {"duration": None, "permanent": False, "magnitude": None,
           "magnitude_sign": None, "stat": None, "stacks": None,
           "raw": (body or "").strip() or None}
    if not body:
        return out
    if PERMANENT_ZH_RE.search(body):
        out["permanent"] = True
    m = DURATION_ZH_RE.search(body)
    if m:
        out["duration"] = int(m.group(1))
    # Magnitude is read from the line with its conditions blanked -- see THRESHOLD_ZH_RE.
    body = THRESHOLD_ZH_RE.sub(" ", body)
    m = SIGNED_PCT_ZH_RE.search(body)
    if m:
        out["magnitude_sign"] = -1 if m.group(1) in "-－" else 1
        out["magnitude"] = float(m.group(2))
    else:
        pcts = re.findall(r"(\d+(?:\.\d+)?)\s*[%％]", body)
        if len(pcts) == 1:
            out["magnitude"] = float(pcts[0])
            if DOWN_WORD_ZH.search(body) and not UP_WORD_ZH.search(body):
                out["magnitude_sign"] = -1
            elif UP_WORD_ZH.search(body) and not DOWN_WORD_ZH.search(body):
                out["magnitude_sign"] = 1
    m = STACKS_ZH_RE.search(body)
    if m:
        out["stacks"] = int(next(g for g in m.groups() if g))
    for word, stat in STAT_ZH:
        if word in body:
            out["stat"] = stat
            break
    if UNREMOVABLE_ZH_RE.search(body):
        out["unremovable"] = True
    # SHIELDS come in three sizes and the percent model holds one of them. "吸收相當於
    # 7500點體力的傷害" is a FLAT amount (95 of 244 shield lines); "75%攻擊力" is sized
    # in the CASTER's ATK; "施術者最大體力30%" in the caster's max HP; "自身最大體力70%"
    # in the HOLDER's. Carried as `flat` and `basis` for the engine to size the shield
    # with -- which it never did before: shield_hp was never set on apply at all.
    m = re.search(r"(\d{2,6})\s*點", body)
    if m:
        out["flat"] = int(m.group(1))
    if "攻擊力" in body:
        out["basis"] = "atk"
    elif re.search(r"施術者|施放者", body) and "體力" in body:
        out["basis"] = "caster_max_hp"
    elif "體力" in body:
        out["basis"] = "max_hp"
    return out


def glossary_lines_zh(note):
    """-> {name: body} for the `※ 名稱：body` lines of one _note1 -- and, for a note with
    no ※ at all, its `名稱：body` LINES, which is how a passive writes its clauses
    ("會心高揚I：常時暴擊率+4%"). A passive's statuses are named by those labels, so the
    clause IS the status's glossary entry; without this, every passive-granted stat mod
    had "no line at all" and no magnitude.
    """
    out = {}
    if not note:
        return out
    if "※" in note:
        for part in re.split(r"※\s*", note)[1:]:
            flat = " ".join(part.split())
            m = re.match(r"^(.{1,40}?)\s*[：:]\s*(.+)$", flat)
            if m:
                out[m.group(1).strip()] = m.group(2).strip()
        return out
    for line in note.split("\n"):
        flat = " ".join(line.split())
        m = re.match(r"^(.{1,20}?)\s*[：:]\s*(.+)$", flat)
        if m and not re.search(r"\d+\s*[%％]", m.group(1)):
            out[m.group(1).strip()] = m.group(2).strip()
    return out


def norm_name_zh(s):
    """Join key for a Chinese status name: qualifiers off, spacing and dots off.

    Rows are `加速(5)`, `穩固(SP)`, `超 •鐵腕`; the glossary writes `加速`, `穩固`,
    `超•鐵腕`. norm_name() cannot be reused -- it strips everything but [a-z0-9] and
    turns every Chinese name into the empty string.
    """
    s = re.sub(r"\s*[(（][^)）]*[)）]\s*$", "", s or "")
    return re.sub(r"[\s•·・]+", "", s)

"""Target-phrase resolution: prose -> a canonical target token the engine understands."""
import re

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

"""Skill-description parser, as a package (was tools/parse_skills.py).

Split by concern so the rule set can grow toward full coverage without one giant file
(see docs/BATTLE_SKILL_PLAN.md):
  * text.py     -- cleaning + structural splitting (glosses, blocks, clauses).
  * targets.py  -- target-phrase resolution.
  * matchers.py -- the RULES: triggers, no-op recognition, per-segment op matchers. Grows.
  * driver.py   -- clause -> effects, skill row -> record. Ties it together.

tools/parse_skills.py is the thin CLI (validate / --all / --write coverage report).
"""
from .driver import parse_clause, parse_skill          # noqa: F401
from .text import clean                                # noqa: F401

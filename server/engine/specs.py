"""Load the compiled skill and status data.

These files are build artifacts of `tools/compile_skills.py` and
`tools/compile_statuses.py`, checked by `test_skill_specs.py`. Nothing is parsed or
inferred here -- if a value is missing it stays missing, because every consumer needs to
be able to tell "the pack never said" apart from a default. See
docs/BATTLE_CLIENT_CONTRACT.md 5.1 for why so many numbers are absent.
"""
import glob
import json
import os
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(os.path.dirname(HERE), "battle_data")
SKILLS_DIR = os.path.join(DATA, "skills")
STATUSES_FILE = os.path.join(DATA, "statuses.json")

_lock = threading.Lock()
_skills = None
_statuses = None


def _load_skills():
    """-> {skill id: spec}, merged across the per-cast split.

    Shared skills are duplicated into each owning cast's file so each file is readable on
    its own; the copies are asserted identical by the harness, so last-write-wins here is
    safe.
    """
    out = {}
    for path in glob.glob(os.path.join(SKILLS_DIR, "**/*.json"), recursive=True):
        if os.path.basename(path) == "_index.json":
            continue
        with open(path, encoding="utf-8") as f:
            for sid, spec in json.load(f).items():
                out[int(sid)] = spec
    if not out:
        raise RuntimeError(
            f"no compiled skills in {SKILLS_DIR} -- run tools/compile_skills.py")
    return out


def skills():
    global _skills
    with _lock:
        if _skills is None:
            _skills = _load_skills()
        return _skills


def statuses():
    global _statuses
    with _lock:
        if _statuses is None:
            if not os.path.isfile(STATUSES_FILE):
                raise RuntimeError(
                    f"{STATUSES_FILE} missing -- run tools/compile_statuses.py")
            with open(STATUSES_FILE, encoding="utf-8") as f:
                _statuses = {int(k): v for k, v in json.load(f).items()}
        return _statuses


def skill(skill_id):
    return skills().get(int(skill_id))


def status(status_id):
    return statuses().get(int(status_id))


def reset():
    """Drop the caches. For tests that regenerate the data mid-run."""
    global _skills, _statuses
    with _lock:
        _skills = _statuses = None

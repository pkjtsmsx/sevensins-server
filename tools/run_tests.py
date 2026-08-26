#!/usr/bin/env python3
"""Lint server/ with pyflakes, then run every server/test_*.py -- one line each.

    python3 tools/run_tests.py            # all suites
    python3 tools/run_tests.py gear karma # only suites whose name contains a word
    python3 tools/run_tests.py -v         # stream each suite's output as it runs

The suites are standalone scripts with their own `check()` helpers, not pytest, and
about a third of them print nothing on success -- the exit code is the only verdict.
Before this existed, "run the tests" meant a shell loop somebody typed from memory, and
a suite that silently regressed to rc=1 with no "FAIL" line was easy to read as green.

Each suite runs in its own interpreter, in `server/`, so a suite that pollutes module
state (several set SEVENSINS_ACCOUNTS before importing player_state) cannot leak into
the next. Exit status is non-zero if ANY suite fails, which is what CI and a pre-push
habit both want.

This is the unit net. `tools/battle_fuzz.py` is the regression net for battle and is
NOT run from here -- it takes minutes, and CLAUDE.md section 6 says when it is owed.
"""
import glob
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "server")


# What lint is allowed to FAIL the run. pyflakes exits 1 on any message, and most of
# what it says about this tree is advisory ("assigned but never used" in a test). These
# are the ones that are bugs: a name that does not exist, or one that stops meaning
# what the code thinks it means. `from wire import *` had switched the first off for
# all of titan_server.py, and a `_clamp` that exists nowhere sat in battle.py for as
# long as it was off -- see commit 02a5ee9.
LINT_FATAL = ("undefined name", "undefined local", "shadowed by loop variable",
              "duplicate argument", "redefinition of unused")
# player_state/__init__.py is the package's re-export facade; its star imports draw a
# warning that applies only to that near-empty file and is deliberate.
LINT_IGNORE = ("player_state/__init__.py",)


def lint():
    """pyflakes over server/ -- -> (ok, one-line summary)."""
    files = sorted(glob.glob(os.path.join(SERVER, "*.py"))
                   + glob.glob(os.path.join(SERVER, "engine", "*.py"))
                   + glob.glob(os.path.join(SERVER, "player_state", "*.py")))
    proc = subprocess.run([sys.executable, "-m", "pyflakes", *files],
                          capture_output=True, text=True)
    if "No module named pyflakes" in (proc.stderr or ""):
        return False, "pyflakes is not installed (pip install pyflakes)"
    fatal, other = [], 0
    for ln in (proc.stdout + proc.stderr).splitlines():
        if any(ig in ln for ig in LINT_IGNORE):
            continue
        if any(k in ln for k in LINT_FATAL):
            fatal.append(ln.replace(SERVER + os.sep, ""))
        elif ln.strip():
            other += 1
    if fatal:
        return False, f"{len(fatal)} fatal:\n" + "\n".join(f"        {f}" for f in fatal)
    return True, f"{len(files)} files, {other} advisory"


def main(argv):
    verbose = "-v" in argv
    words = [a for a in argv if not a.startswith("-")]
    failed = []
    if not words:
        # Lint first, every time: it is faster than any suite and it is the only check
        # here that can see a NameError before a player does.
        t0 = time.time()
        ok, summary = lint()
        print(f"{'ok  ' if ok else 'FAIL'}  {'pyflakes':<34} {time.time() - t0:5.1f}s  {summary}")
        if not ok:
            failed.append("pyflakes")
    suites = sorted(glob.glob(os.path.join(SERVER, "test_*.py")))
    if words:
        suites = [s for s in suites if any(w in os.path.basename(s) for w in words)]
    if not suites:
        print("no suites matched", file=sys.stderr)
        return 2

    t_all = time.time()
    for path in suites:
        name = os.path.basename(path)
        t0 = time.time()
        proc = subprocess.run([sys.executable, path], cwd=SERVER,
                              capture_output=not verbose, text=True)
        secs = time.time() - t0
        ok = proc.returncode == 0
        # The suite's own last verdict line, when it prints one ("0 failure(s)",
        # "all OK"); otherwise just the exit code, which is all some of them have.
        tail = ""
        if not verbose:
            lines = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
            for ln in reversed(lines):
                if "failure" in ln or "OK" in ln:
                    tail = ln.strip()
                    break
        print(f"{'ok  ' if ok else 'FAIL'}  {name:<34} {secs:5.1f}s  {tail}")
        if not ok:
            failed.append(name)
            if not verbose:
                # Show what a failing suite said; a bare "FAIL" helps nobody.
                out = (proc.stdout or "") + (proc.stderr or "")
                for ln in out.splitlines()[-25:]:
                    print(f"        {ln}")

    checks = len(suites) + (0 if words else 1)          # lint counts as a check
    print(f"\n{checks - len(failed)}/{checks} checks passed "
          f"in {time.time() - t_all:.0f}s")
    if failed:
        print("failed: " + ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

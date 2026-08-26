#!/usr/bin/env python3
"""Run every server/test_*.py and say, in one line each, which passed.

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


def main(argv):
    verbose = "-v" in argv
    words = [a for a in argv if not a.startswith("-")]
    suites = sorted(glob.glob(os.path.join(SERVER, "test_*.py")))
    if words:
        suites = [s for s in suites if any(w in os.path.basename(s) for w in words)]
    if not suites:
        print("no suites matched", file=sys.stderr)
        return 2

    failed = []
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

    print(f"\n{len(suites) - len(failed)}/{len(suites)} suites passed "
          f"in {time.time() - t_all:.0f}s")
    if failed:
        print("failed: " + ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

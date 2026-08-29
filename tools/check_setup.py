#!/usr/bin/env python3
"""Preflight: report which bring-up inputs are missing, and what to do about each.

A fresh clone cannot run anything table-driven -- the design pack and the il2cpp type
information are the game's and are not in this repo (docs/BRINGUP.md). Without this
script that surfaces as the same 30-line traceback repeated once per suite, with the
real cause buried in the middle:

    RuntimeError: design cache miss and no DummyDll staged under re/*/DummyDll

Exit code is 0 when everything needed to run the tests is present, 1 otherwise, so this
is usable as a gate in a script. Checks are ordered the way a person fixes them.

Usage:  python3 tools/check_setup.py
"""
import glob
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "server")

OK, WARN, BAD = "ok  ", "warn", "MISS"


class Report:
    """Collects results so the whole picture prints at once. A person fixing setup wants
    every missing input in one pass, not one per re-run."""

    def __init__(self):
        self.rows = []
        self.blocking = 0

    def add(self, status, name, detail, fix=None):
        self.rows.append((status, name, detail, fix))
        if status == BAD:
            self.blocking += 1

    def ok(self, name, detail):
        self.add(OK, name, detail)

    def warn(self, name, detail, fix=None):
        self.add(WARN, name, detail, fix)

    def miss(self, name, detail, fix):
        self.add(BAD, name, detail, fix)


def check_python(r):
    v = sys.version_info
    got = f"{v.major}.{v.minor}.{v.micro}"
    if (v.major, v.minor) >= (3, 12):
        r.ok("python", got)
    else:
        # Not a hard stop -- the server may well run -- but the phone build pins 3.12
        # and nothing here is tested below it.
        r.warn("python", f"{got} (3.12+ expected)",
               "the hostapp build pins 3.12; older versions are untested")


def check_deps(r):
    """UnityPy and TypeTreeGeneratorAPI are needed to READ the pack, not to run a server
    that already has a warm cache -- so a miss here is only blocking when the cache is
    cold too. That is resolved in summarise(), which sees both results."""
    for mod, why in (("UnityPy", "reads the design pack"),
                     ("TypeTreeGeneratorAPI", "regenerates il2cpp type trees")):
        try:
            __import__(mod)
            r.ok(mod, why)
        except ImportError:
            r.warn(mod, f"not installed -- {why}",
                   "see tools/SETUP.md (pip install --user, PEP 668 may need "
                   "--break-system-packages)")


def find_pack():
    packs = glob.glob(os.path.join(SERVER, "patch_root", "bundles", "design_pack_*.ab"))
    return max(packs, key=os.path.getmtime) if packs else None


def check_pack(r):
    pack = find_pack()
    if pack:
        mb = os.path.getsize(pack) / 1e6
        r.ok("design pack", f"{os.path.basename(pack)} ({mb:.0f} MB)")
    else:
        r.miss("design pack", "no server/patch_root/bundles/design_pack_*.ab",
               "pull it from a device's Unity cache -- docs/BRINGUP.md section 1")
    return pack


def check_dummydll(r):
    for d in ("il2cpp227", "il2cpp"):
        p = os.path.join(ROOT, "re", d, "DummyDll")
        dlls = glob.glob(os.path.join(p, "*.dll"))
        if dlls:
            # Flag the fallback: 2.2.4 trees on a 2.2.7 pack yield garbage SILENTLY,
            # which is the single nastiest failure in this whole chain.
            if d == "il2cpp":
                r.warn("DummyDll", f"re/il2cpp/DummyDll ({len(dlls)} dlls) -- fallback",
                       "re/il2cpp227/DummyDll is preferred; trees must match the "
                       "client build the pack came from, or rows parse to garbage")
            else:
                r.ok("DummyDll", f"re/{d}/DummyDll ({len(dlls)} dlls)")
            return True
    r.miss("DummyDll", "no re/il2cpp227/DummyDll/*.dll",
           "run Il2CppDumper over libil2cpp.so + global-metadata.dat -- "
           "docs/BRINGUP.md section 2")
    return False


def check_cache(r):
    cache = os.environ.get("SEVENSINS_DESIGN_CACHE") or os.path.join(SERVER, "design_cache")
    stamp = os.path.join(cache, ".source")
    if os.path.isfile(stamp):
        n = len(glob.glob(os.path.join(cache, "*.json")))
        try:
            with open(stamp, encoding="utf-8") as f:
                src = f.read().strip()
        except OSError:
            src = "unreadable"
        r.ok("design cache", f"{n} forms, stamp {src[:48]}")
        return True
    r.warn("design cache", "cold -- will be built on first table read (~30s)")
    return False


def check_compiled(r):
    skills = os.path.join(SERVER, "battle_data", "skills", "_index.json")
    statuses = os.path.join(SERVER, "battle_data", "statuses.json")
    for path, name, tool in ((skills, "battle specs", "tools/compile_skills.py"),
                             (statuses, "status catalogue", "tools/compile_statuses.py")):
        if os.path.isfile(path):
            r.ok(name, os.path.relpath(path, ROOT))
        else:
            r.miss(name, f"{os.path.relpath(path, ROOT)} not built",
                   f"python3 {tool}  (needs the design pack first)")


def summarise(r, pack, cache_warm):
    print()
    width = max(len(n) for _, n, _, _ in r.rows)
    for status, name, detail, fix in r.rows:
        print(f"  [{status}] {name.ljust(width)}  {detail}")
        if fix:
            print(f"         {' ' * width}  -> {fix}")

    print()
    if r.blocking == 0:
        print("  Ready. Next: python3 tools/run_tests.py  (expect 32/32)")
        return 0

    # The one combination worth calling out: a cold cache AND no way to fill it is what
    # produces the confusing traceback, and it needs BOTH inputs, not either.
    if not cache_warm and not pack:
        print("  The design cache is cold and there is no pack to build it from, so every")
        print("  table read will fail. Start with docs/BRINGUP.md.")
    print(f"  {r.blocking} blocking item(s). See docs/BRINGUP.md.")
    return 1


def main():
    r = Report()
    check_python(r)
    check_deps(r)
    pack = check_pack(r)
    check_dummydll(r)
    cache_warm = check_cache(r)
    check_compiled(r)
    return summarise(r, pack, cache_warm)


if __name__ == "__main__":
    sys.exit(main())

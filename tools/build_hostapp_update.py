#!/usr/bin/env python3
"""Build a hot-update package for the on-device host app (hostapp/).

The host app already ships a full copy of server/'s code, staged into the APK by
hostapp/app/build.gradle's `stageServer` task at BUILD time. Rebuilding and reinstalling
the APK for every server-side tweak (e.g. a battle-engine change) is slow and, until the
release key is pinned, forces an uninstall that wipes app storage. This is the other half:
a small CODE-ONLY zip the app can pull over the network and apply WITHOUT a reinstall.

Reads the SAME file list as build.gradle (../hostapp/server_files.json) -- one list, so a
module added to the engine can't silently go unshipped in one path while shipping in the
other (that already happened once: battle_effects/battle_data were missing from
build.gradle's hardcoded list for several commits after the Phase 0 package split).

**Code only.** Deliberately excludes accounts/, design_cache/ and patch_root/ -- those are
either user data (accounts) or large/rarely-changing baseline assets the app already seeds
into its own writable storage on first run and re-seeds only when empty (main.py never
overwrites an existing file there). A hot-update must never be able to touch a save.

Output: hostapp_update/update.json + hostapp_update/server_update.zip, meant to be served
by tools/serve_hostapp_update.py and pulled by the app's "Check for updates" button (see
UpdateManager.java). "Newer" is decided by comparing the zip's sha256 against what the app
already has applied -- no version counter to keep in sync, and it self-corrects if a build
is re-run with identical output.

Usage:  tools/build_hostapp_update.py [--out DIR]
"""
import argparse
import hashlib
import json
import os
import sys
import ast
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "server")
FILE_LIST = os.path.join(ROOT, "hostapp", "server_files.json")
DEFAULT_OUT = os.path.join(ROOT, "hostapp_update")


# A package directory ships WHOLESALE, so anything lying in it rides along. A stale
# `player_state/core.py.bak-refactor` (225 KB, ~30% of the payload) was going out to the
# phone this way: dead weight on a metered connection, and the kind of thing that grows
# silently because nobody looks at the file list. Editor leftovers and backups are never
# importable anyway -- a module name cannot contain a dot -- so exclude them by shape
# rather than maintaining a denylist of specific filenames.
_STRAY_SUFFIXES = (".orig", ".rej", ".swp", ".swo", "~")


def _is_stray(name):
    if name.endswith(_STRAY_SUFFIXES):
        return True
    # core.py.bak-refactor, foo.py.old, bar.json.2 -- anything with a suffix AFTER the
    # real extension. A legitimate data file has exactly one dot-extension.
    stem, _, _ = name.partition(".")
    return name.count(".") > 1 and not name.endswith((".py", ".json", ".html"))


def _iter_files(base, rel):
    """-> every real file under base/rel (a module path or a package/data dir), skipping
    __pycache__/.pyc/accounts -- the same exclusions stageServer applies."""
    full = os.path.join(base, rel)
    if os.path.isfile(full):
        yield rel
        return
    for dirpath, dirs, files in os.walk(full):
        dirs[:] = [d for d in dirs if d != "__pycache__" and d != "accounts"]
        for name in files:
            if name.endswith(".pyc") or _is_stray(name):
                continue
            yield os.path.relpath(os.path.join(dirpath, name), base)


def _local_top_levels():
    """-> the top-level module/package names that live in server/."""
    out = set()
    for name in os.listdir(SERVER):
        full = os.path.join(SERVER, name)
        if os.path.isdir(full) and os.path.isfile(os.path.join(full, "__init__.py")):
            out.add(name)
        elif name.endswith(".py"):
            out.add(name[:-3])
    return out


def _catches_import_error(handler):
    names = handler.type
    if names is None:                       # bare except
        return True
    targets = names.elts if isinstance(names, ast.Tuple) else [names]
    return any(getattr(t, "id", None) in ("ImportError", "ModuleNotFoundError")
               for t in targets)


def _check_imports_covered(rels):
    """Refuse to build a zip whose own code cannot import on the device.

    server_files.json is hand-maintained, and the failure mode when it falls behind is
    the worst kind: the zip builds, uploads and applies cleanly, then the server dies on
    the phone at import time with nothing in the local logs. `engine` was missing from
    `packages` from the day it was created -- battle.py imports it at module scope, so
    the first hot update carrying the new battle.py would have bricked the server.
    `battle_effects` had already gone stale the same way once before.

    Tests are excluded: they import things (pytest and friends) the device never needs,
    and so is anything wrapped in `try: import x / except ImportError`, which is how a
    module declares itself optional. battle_inspector is the case: a dev-only tool that
    can rewrite live battle traffic, deliberately kept off the phone, so titan_server
    falls back to a stub rather than the build demanding it be shipped.
    """
    shipped = {r.replace(os.sep, "/") for r in rels}
    have = {n.split("/")[0].removesuffix(".py") for n in shipped}
    local = _local_top_levels()
    missing = {}
    for rel in sorted(shipped):
        if not rel.endswith(".py") or os.path.basename(rel).startswith("test_"):
            continue
        try:
            tree = ast.parse(open(os.path.join(SERVER, rel), encoding="utf-8").read())
        except SyntaxError as exc:
            sys.exit(f"{rel}: {exc}")
        optional = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Try) and any(
                    _catches_import_error(h) for h in node.handlers):
                for sub in node.body:
                    for inner in ast.walk(sub):
                        optional.add(id(inner))
        for node in ast.walk(tree):
            if id(node) in optional:
                continue
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [(node.module or "").split(".")[0]] if not node.level else []
            else:
                continue
            for name in names:
                if name and name in local and name not in have:
                    missing.setdefault(name, set()).add(rel)
    if missing:
        lines = [f"  {name}  (imported by {', '.join(sorted(who))})"
                 for name, who in sorted(missing.items())]
        sys.exit("server_files.json does not ship everything the shipped code imports:\n"
                 + "\n".join(lines)
                 + "\nadd the missing name to `packages` or `modules`.")


def build(out_dir):
    with open(FILE_LIST, encoding="utf-8") as f:
        spec = json.load(f)
    rels = []
    # `data_files` are single non-code files the runtime opens by name
    # (save_editor_ui.html). Same treatment as a module: shipped verbatim, and its
    # absence is a hard error rather than a page that renders blank on the phone.
    for m in spec["modules"] + spec.get("data_files", []):
        if not os.path.isfile(os.path.join(SERVER, m)):
            sys.exit(f"missing {os.path.join(SERVER, m)} -- update server_files.json?")
        rels.append(m)
    for p in spec["packages"] + spec["data_dirs"]:
        if not os.path.isdir(os.path.join(SERVER, p)):
            sys.exit(f"missing {os.path.join(SERVER, p)} -- update server_files.json?")
        rels.extend(_iter_files(SERVER, p))

    _check_imports_covered(rels)

    os.makedirs(out_dir, exist_ok=True)
    zip_path = os.path.join(out_dir, "server_update.zip")
    # Deterministic order + fixed timestamps, so re-running the tool on unchanged source
    # produces a byte-identical zip -- the sha256-equality "is this new?" check on the
    # device depends on that, not just on the file CONTENTS matching.
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in sorted(set(rels)):
            info = zipfile.ZipInfo(rel.replace(os.sep, "/"), date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            with open(os.path.join(SERVER, rel), "rb") as f:
                zf.writestr(info, f.read())

    sha256 = hashlib.sha256()
    with open(zip_path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            sha256.update(chunk)
    digest = sha256.hexdigest()
    size = os.path.getsize(zip_path)

    manifest = {"sha256": digest, "size": size, "file": "server_update.zip",
               "file_count": len(set(rels))}
    with open(os.path.join(out_dir, "update.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    print(f"wrote {zip_path} ({size} bytes, {len(set(rels))} files, sha256={digest[:12]}…)")
    print(f"wrote {os.path.join(out_dir, 'update.json')}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args()
    build(args.out)


if __name__ == "__main__":
    main()

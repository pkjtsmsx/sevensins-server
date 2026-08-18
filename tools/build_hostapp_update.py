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
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "server")
FILE_LIST = os.path.join(ROOT, "hostapp", "server_files.json")
DEFAULT_OUT = os.path.join(ROOT, "hostapp_update")


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
            if name.endswith(".pyc"):
                continue
            yield os.path.relpath(os.path.join(dirpath, name), base)


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

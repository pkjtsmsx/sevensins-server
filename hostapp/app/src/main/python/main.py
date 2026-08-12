"""Embedded Seven Sins server: entry point for the Android host app.

Runs both servers on the phone so the game needs nothing else:

  * titan_server on 0.0.0.0:22110 -- the game protocol. A RAW TCP socket, which is why
    Android's cleartext rules never apply to it.
  * bundle_server on 0.0.0.0:8088 -- asset bundles over plain HTTP, matching the CDN
    host patched into the game APK (see server/patch_cdn_config.py).

Both are pure stdlib, so this app carries no pip dependencies at all -- unlike reTBHost,
which needed a hand-built pydantic-core wheel. That holds only while design_data never
reparses the design pack: it imports UnityPy lazily, and we ship a pre-built cache plus
the pack itself so its stamp matches and the lazy path is never taken.

PATHS. The in-APK package dir is read-only and the code is written to open files next to
itself, so before importing anything we relocate the writable paths via the environment
(SEVENSINS_ACCOUNTS / SEVENSINS_DESIGN_CACHE / SEVENSINS_PATCH_ROOT). Those reads happen
at MODULE IMPORT time, so the environment must be set before the first import of
design_data or player_state on any path.

HOT UPDATES. UpdateManager.java (the "Check for updates" button) extracts a newer code
snapshot to <data_dir>/sevensins/server_update/<id>/ and points
server_update/active.txt at it -- see tools/build_hostapp_update.py for what that
snapshot contains (code + battle_data only, never accounts/design_cache/patch_root, so a
hot update can never touch a save). If active.txt names a directory that actually has
titan_server.py in it, `configure_runtime_env` puts it on sys.path AHEAD OF the
originally-shipped package dir, so `import battle_effects` (etc.) resolves the update's
copy first. A corrupt/missing pointer just falls back to the shipped copy -- this must
never be the thing that stops the server from starting.
"""
from __future__ import annotations

import os
import shutil
import sys
import threading

TITAN_PORT = 22110
BUNDLE_PORT = 8088

_pkg_dir = None
_state = {"titan": None, "bundles": None, "started": False}


def _package_dir():
    """Absolute path of the extracted `sevensins` package (real files, not the bundle)."""
    global _pkg_dir
    if _pkg_dir is None:
        import sevensins
        # __file__ is None for a namespace package (one with no __init__.py), which is
        # what this was until the stage task started generating one. __path__ is
        # populated either way, so prefer it and keep __file__ as the fallback.
        path = list(getattr(sevensins, "__path__", []) or [])
        _pkg_dir = (os.path.abspath(path[0]) if path
                    else os.path.dirname(os.path.abspath(sevensins.__file__)))
    return _pkg_dir


def configure_runtime_env(data_dir):
    """Point the server's relocatable paths at writable storage (idempotent).

    Returns the base directory. `setdefault` keeps it safe to call more than once and
    lets an explicit environment win.
    """
    base = os.path.join(data_dir, "sevensins")
    accounts = os.path.join(base, "accounts")
    cache = os.path.join(base, "design_cache")
    patch_root = os.path.join(base, "patch_root")
    for d in (accounts, cache, os.path.join(patch_root, "bundles")):
        os.makedirs(d, exist_ok=True)

    pkg = _package_dir()
    # The design cache must be WRITABLE: design_data._ensure_stamp creates the stamp file
    # and purges stale forms. Copy the shipped cache out on first run rather than pointing
    # at the read-only package copy.
    if not os.listdir(cache):
        src = os.path.join(pkg, "design_cache")
        if os.path.isdir(src):
            for name in os.listdir(src):
                shutil.copy2(os.path.join(src, name), os.path.join(cache, name))
    # Seed the minimum patch_root the client needs to boot: the AssetBundleManifest and
    # the design pack. Copied whole so the layout in the package is the layout served.
    # Missing manifest = "Retrieving patch manifest ... Retry n/10" forever.
    src_root = os.path.join(pkg, "pack")
    if os.path.isdir(src_root):
        for dirpath, _dirs, files in os.walk(src_root):
            rel = os.path.relpath(dirpath, src_root)
            dest = os.path.join(patch_root, rel) if rel != "." else patch_root
            os.makedirs(dest, exist_ok=True)
            for name in files:
                target = os.path.join(dest, name)
                # Never clobber a user-imported file with the seed copy.
                if not os.path.exists(target):
                    shutil.copy2(os.path.join(dirpath, name), target)
    # A starting account, so a fresh install can log in immediately.
    if not os.listdir(accounts):
        src = os.path.join(pkg, "seed_accounts")
        if os.path.isdir(src):
            for name in os.listdir(src):
                shutil.copy2(os.path.join(src, name), os.path.join(accounts, name))

    os.environ.setdefault("SEVENSINS_ACCOUNTS", accounts)
    os.environ.setdefault("SEVENSINS_DESIGN_CACHE", cache)
    os.environ.setdefault("SEVENSINS_PATCH_ROOT", patch_root)
    # titan_server and bundle_server write logs next to themselves; chdir so those land
    # on writable storage instead of the read-only package dir.
    os.chdir(base)
    if pkg not in sys.path:
        # The modules import each other flat (`import player_state`), so the package dir
        # itself goes on the path rather than being imported as a package.
        sys.path.insert(0, pkg)
    update_dir = active_update_dir(base)
    if update_dir and update_dir not in sys.path:
        # Inserted LAST so it lands at index 0, ahead of `pkg` -- both blocks insert at
        # 0, so whichever runs second wins the front of sys.path. Import resolution is
        # per top-level module/package name, so a PARTIAL update (only some files changed
        # since the last one) still falls through to the shipped copy for anything the
        # update snapshot doesn't carry. In practice build_hostapp_update.py always ships
        # the full set, but nothing here depends on that -- a stale/incomplete update_dir
        # degrades to "some modules come from the update, the rest from the shipped
        # copy", never a crash.
        sys.path.insert(0, update_dir)
    return base


def active_update_dir(base):
    """The applied hot-update's code directory, or None if there isn't one / it's
    missing its own marker file (a half-written or since-deleted extraction). Never
    raises -- a bad pointer here must fall back to the shipped copy, not break startup."""
    pointer = os.path.join(base, "server_update", "active.txt")
    try:
        with open(pointer, encoding="utf-8") as f:
            name = f.read().strip()
    except OSError:
        return None
    if not name or os.sep in name or name in (os.curdir, os.pardir):
        return None                        # never let a corrupt pointer escape the dir
    path = os.path.join(base, "server_update", name)
    return path if os.path.isfile(os.path.join(path, "titan_server.py")) else None


def start_server(data_dir):
    """Start both servers once, in daemon threads. Returns a short status string."""
    if _state["started"]:
        return "already running"
    base = configure_runtime_env(data_dir)

    import bundle_server
    import titan_server

    def run_titan():
        try:
            titan_server.main(TITAN_PORT)
        except Exception:                       # noqa: BLE001 -- surface, never die silent
            import traceback
            traceback.print_exc()

    def run_bundles():
        try:
            bundle_server.serve(BUNDLE_PORT, os.environ["SEVENSINS_PATCH_ROOT"])
        except Exception:                       # noqa: BLE001
            import traceback
            traceback.print_exc()

    _state["titan"] = threading.Thread(target=run_titan, name="titan", daemon=True)
    _state["bundles"] = threading.Thread(target=run_bundles, name="bundles", daemon=True)
    _state["titan"].start()
    _state["bundles"].start()
    _state["started"] = True
    return f"listening on {TITAN_PORT} (game) and {BUNDLE_PORT} (bundles), data={base}"


def stop_server():
    """Close both listeners and let the daemon threads unwind."""
    if not _state["started"]:
        return "not running"
    try:
        import bundle_server
        import titan_server
        titan_server.shutdown()
        bundle_server.shutdown()
    except Exception:                           # noqa: BLE001
        import traceback
        traceback.print_exc()
    for key in ("titan", "bundles"):
        t = _state[key]
        if t is not None:
            t.join(timeout=5)
        _state[key] = None
    _state["started"] = False
    return "stopped"


def is_running():
    return bool(_state["started"])


def status(data_dir):
    """One line for the UI: whether the asset pack has been imported yet, and whether a
    hot-updated code snapshot is the one actually in effect."""
    base = os.path.join(data_dir, "sevensins")
    bundles = os.path.join(base, "patch_root", "bundles")
    try:
        n = len([x for x in os.listdir(bundles) if x.endswith(".ab")])
    except OSError:
        n = 0
    update_dir = active_update_dir(base)
    code = f"code: update {os.path.basename(update_dir)}" if update_dir else "code: shipped"
    return f"{n} bundle(s) imported, {code}"


# Asset import lives in Java (AssetImporter), not here. The archive is ~2.6 GB and the
# picker hands back a content:// InputStream, so the Java side streams it straight into
# `tar -x` -- no temp copy, and no need for twice the free space. Doing it here would
# mean writing the whole archive to disk first just to hand Python a path.

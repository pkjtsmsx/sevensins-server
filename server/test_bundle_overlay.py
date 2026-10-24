#!/usr/bin/env python3
"""Per-file SFW/NSFW mixing: an overlay answers for the bundles it holds, root for the rest.

    python3 test_bundle_overlay.py

The two releases ship different art under the same bundle NAMES, and the client fetches
bundles one at a time by name -- so which release answers can be decided per file. This
checks `Handler.resolve` against real files on disk rather than mocking it, because the
behaviour being asserted IS the filesystem lookup: overlay order, the flat
`bundles/` fallback each root gets, and that path traversal is still refused per root.

With no overlay set, resolution must be byte-identical to the behaviour before overlays
existed -- that is the check that keeps this feature free when it is unused.
"""
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import bundle_server as bs                                      # noqa: E402

FAILURES = []
PREFIX = "Android_AssetBundles/"


def check(cond, msg):
    if not cond:
        FAILURES.append(msg)


class _Probe(bs.Handler):
    """resolve() needs only `path`, `root` and `overlays` -- no socket, no server."""

    def __init__(self, path):
        self.path = path


def _write(root, rel, body):
    full = os.path.join(root, rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w") as f:
        f.write(body)
    return full


def _resolve(root, overlays, name):
    _Probe.root, _Probe.overlays = os.path.realpath(root), tuple(
        os.path.realpath(o) for o in overlays)
    hit = _Probe(f"/cdn/20240101/{PREFIX}{name}").resolve()
    return open(hit).read() if hit else None


def main():
    tmp = tempfile.mkdtemp(prefix="sevensins-overlay-test-")
    try:
        root = os.path.join(tmp, "patch_root")
        chars = os.path.join(tmp, "nsfw_chars")
        # The main root holds everything, flat under bundles/ the way patch_root does.
        for name in ("char_leviathan.ab", "ui_mainmenu.ab"):
            _write(root, f"bundles/{name}", f"sfw:{name}")
        # The overlay holds ONLY the bundle it replaces.
        _write(chars, "bundles/char_leviathan.ab", "nsfw:char_leviathan.ab")

        check(_resolve(root, [], "char_leviathan.ab") == "sfw:char_leviathan.ab",
              "no overlay: the main root must answer, unchanged")
        check(_resolve(root, [], "ui_mainmenu.ab") == "sfw:ui_mainmenu.ab",
              "no overlay: menus must come from the main root")
        check(_resolve(root, [], "missing.ab") is None,
              "no overlay: an absent bundle must still 404")

        check(_resolve(root, [chars], "char_leviathan.ab") == "nsfw:char_leviathan.ab",
              "the overlay holds this bundle and must win")
        check(_resolve(root, [chars], "ui_mainmenu.ab") == "sfw:ui_mainmenu.ab",
              "the overlay does NOT hold this one -- the main root must still answer")
        check(_resolve(root, [chars], "missing.ab") is None,
              "an absent bundle must 404 with an overlay in play too")

        # Order matters, first hit wins.
        first = os.path.join(tmp, "first")
        _write(first, "bundles/char_leviathan.ab", "first:char_leviathan.ab")
        check(_resolve(root, [first, chars], "char_leviathan.ab")
              == "first:char_leviathan.ab", "the earlier overlay must win")
        check(_resolve(root, [chars, first], "char_leviathan.ab")
              == "nsfw:char_leviathan.ab", "overlay order is not being honoured")

        # An overlay that exists but is empty must change nothing.
        empty = os.path.join(tmp, "empty")
        os.makedirs(empty, exist_ok=True)
        check(_resolve(root, [empty], "char_leviathan.ab") == "sfw:char_leviathan.ab",
              "an empty overlay must fall through to the main root")

        # Containment, per root. A crafted path must not climb out of either tree.
        _write(tmp, "secret.ab", "SECRET")
        _Probe.root, _Probe.overlays = os.path.realpath(root), (os.path.realpath(chars),)
        for attack in (f"/{PREFIX}../secret.ab", f"/{PREFIX}../../secret.ab",
                       f"/cdn/{PREFIX}subdir/../../secret.ab"):
            hit = _Probe(attack).resolve()
            got = open(hit).read() if hit else None
            check(got != "SECRET", f"path traversal escaped the served tree: {attack}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    for f in FAILURES:
        print("  FAIL", f)
    print(f"\n{'all OK' if not FAILURES else str(len(FAILURES)) + ' failure(s)'}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())

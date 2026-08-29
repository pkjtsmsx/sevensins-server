#!/usr/bin/env python3
"""Build the hostapp hot-update package and publish it as a GitHub Release, so the
in-app "Check for updates" button can reach it from anywhere (cellular data included),
not just the same LAN as tools/serve_hostapp_update.py.

Publishes to a SEPARATE, dedicated repo -- never the main project repo -- because the
update payload (server code only, built by build_hostapp_update.py from
hostapp/server_files.json) is the only thing meant to be public here; the main repo's
RE dumps and patch tooling are not. See that repo's README for the split rationale.

Each run creates a NEW release (tag = the zip's own sha256 prefix, so two publishes of
identical source collide on the same tag rather than piling up duplicates) and uploads
update.json + server_update.zip. The app always points at the "latest" release download
URLs, which GitHub keeps pointed at whichever release was published most recently:

    https://github.com/<repo>/releases/latest/download/update.json
    https://github.com/<repo>/releases/latest/download/server_update.zip

Requires the `gh` CLI, authenticated as an account with push access to the channel repo.
Check `gh auth status` first: on a machine with several accounts the ACTIVE one is used,
and it is not necessarily the one that owns the channel.

Usage:  tools/publish_hostapp_update.py [--repo owner/name] [--notes "..."]
"""
import argparse
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import build_hostapp_update as builder                               # noqa: E402

OUT_DIR = builder.DEFAULT_OUT

# The release repo is configured per-machine rather than committed -- the same value as
# local.properties' sevensins.updateRepo, which the APK is built against:
#
#     tools/update_channel.txt   (gitignored, one line: owner/name)
#   or  SEVENSINS_UPDATE_REPO=owner/name  in the environment
#   or  --repo owner/name
#
# There is deliberately NO default. Publishing is the one operation that reaches every
# device on the channel, so an unset channel must stop the run, never guess a destination.
CHANNEL_FILE = os.path.join(ROOT, "tools", "update_channel.txt")


def default_repo():
    env = os.environ.get("SEVENSINS_UPDATE_REPO", "").strip()
    if env:
        return env
    try:
        with open(CHANNEL_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=default_repo(),
                    help="owner/name of the dedicated release repo (default: "
                         "SEVENSINS_UPDATE_REPO, or tools/update_channel.txt)")
    ap.add_argument("--notes", default="", help="release notes (optional)")
    args = ap.parse_args()

    if not args.repo:
        sys.exit("no update channel configured -- write 'owner/name' into "
                 f"{CHANNEL_FILE}, set SEVENSINS_UPDATE_REPO, or pass --repo. "
                 "Refusing to guess: this is the one command that reaches every device.")

    builder.build(OUT_DIR)
    with open(os.path.join(OUT_DIR, "update.json"), encoding="utf-8") as f:
        manifest = json.load(f)
    tag = "update-" + manifest["sha256"][:12]

    existing = subprocess.run(
        ["gh", "release", "view", tag, "--repo", args.repo],
        capture_output=True, text=True)
    if existing.returncode == 0:
        print(f"release {tag} already exists on {args.repo} (identical source, "
             "nothing to publish) -- it is already the latest, most recent wins.")
        return

    notes = args.notes or (f"{manifest['file_count']} files, {manifest['size']} bytes, "
                           f"sha256={manifest['sha256']}")
    subprocess.run(
        ["gh", "release", "create", tag,
         os.path.join(OUT_DIR, "update.json"),
         os.path.join(OUT_DIR, "server_update.zip"),
         "--repo", args.repo,
         "--title", tag,
         "--notes", notes],
        check=True)

    print(f"\npublished {tag} to {args.repo}. The app's stable URLs (unchanged"
         " across every future publish):")
    print(f"  https://github.com/{args.repo}/releases/latest/download/update.json")
    print(f"  https://github.com/{args.repo}/releases/latest/download/server_update.zip")


if __name__ == "__main__":
    main()

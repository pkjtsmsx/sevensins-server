#!/usr/bin/env python3
"""Serve the hot-update package (tools/build_hostapp_update.py's output) over plain HTTP
for the host app's "Check for updates" button to pull.

A separate tiny server rather than bolting this onto bundle_server.py: bundle_server is
embedded IN the app (it ships to the device and runs there too), and its `resolve()` is
tuned to the CDN's `Android_AssetBundles/...` path shape -- growing it to also carry an
unrelated update route means every future bundle_server change has to reason about update
traffic too, on a file that already runs live on-device. This one is dev-machine-only and
never ships.

Usage:  tools/serve_hostapp_update.py [port] [--dir DIR]
Then on the phone: Check for updates -> http://<this box's LAN IP>:<port>/
"""
import argparse
import http.server
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DIR = os.path.join(ROOT, "hostapp_update")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("port", nargs="?", type=int, default=8089)
    ap.add_argument("--dir", default=DEFAULT_DIR)
    ap.add_argument("--bind", default="0.0.0.0")
    args = ap.parse_args()

    if not os.path.isdir(args.dir):
        raise SystemExit(f"{args.dir} does not exist -- run "
                         "tools/build_hostapp_update.py first")

    handler = lambda *a, **kw: http.server.SimpleHTTPRequestHandler(  # noqa: E731
        *a, directory=args.dir, **kw)
    httpd = http.server.ThreadingHTTPServer((args.bind, args.port), handler)
    print(f"serving {args.dir} on {args.bind}:{args.port} "
         f"(app pulls update.json + server_update.zip from here)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

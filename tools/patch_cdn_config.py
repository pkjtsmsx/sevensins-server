#!/usr/bin/env python3
"""Repoint the client's asset-bundle CDN at a local server, inside the APK.

The patch/CDN host is the ONE endpoint that is neither in the binary nor in the
design pack. `Game.Conf.ConfigPatch` holds `PatchServer` entries, serialized into a
`globalgamemanagers.assets` split that ships in the APK as
`assets/bin/Data/37e9dcc2b6462d44795a2bea3a1b2eeb`. Layout, straight from the dump:

    PatchServer { string _name; UriScheme _scheme; string _host; }
    UriScheme { Http = 0, File = 1, Https = 2 }

on the wire as `[int32 len]"ob"[pad][int32 scheme=2][int32 len=55][host][pad]`.

Redirecting it is what lets a device fetch bundles from a server on the phone (or
from the host machine over `adb reverse`) instead of the dead CDN — no mitmproxy, no
hosts file, no system-CA bind-mount, and therefore no root. `_scheme` goes to Http
because a local server has no cert a stock client would trust; that in turn needs
`usesCleartextTraffic` in the manifest (see patch_manifest_cleartext.py).

WHY THE HOST STRING IS PADDED. This is a raw in-place byte patch of a Unity
serialized file, whose header carries per-object byte sizes and offsets. Changing the
string's LENGTH would invalidate every offset after it, so the replacement must be
exactly as long as the original (55 bytes). `sevensins/` happens to be exactly the 10
characters needed to pad `127.0.0.1:<port>/Android_AssetBundles/231101en/` up to 55 —
so it is a real path segment the local server serves under, not filler.
"""
import argparse, os, shutil, struct, sys, zipfile

ASSET = "assets/bin/Data/37e9dcc2b6462d44795a2bea3a1b2eeb"
OLD_HOST = b"7sins-en-patch.uj.com.tw/Android_AssetBundles/231101en/"
SCHEME_HTTP, SCHEME_HTTPS = 0, 2


def build_host(port, host="127.0.0.1"):
    """-> the replacement host string, padded to exactly len(OLD_HOST)."""
    tail = "Android_AssetBundles/231101en/"
    head = f"{host}:{port}/"
    pad = len(OLD_HOST) - len(head) - len(tail)
    if pad < 0:
        sys.exit(f"port {port} makes the host too long by {-pad} bytes")
    # One padding segment, kept a real directory name so the served tree is sane.
    seg = ("sevensins/" if pad == 10 else ("p" * (pad - 1) + "/")) if pad else ""
    host = f"{head}{seg}{tail}".encode()
    assert len(host) == len(OLD_HOST), (len(host), len(OLD_HOST))
    return host


def patch_bytes(data, port, host="127.0.0.1"):
    i = data.find(OLD_HOST)
    if i < 0:
        sys.exit("CDN host string not found -- wrong asset or already patched")
    length = struct.unpack_from("<I", data, i - 4)[0]
    if length != len(OLD_HOST):
        sys.exit(f"unexpected length prefix {length}")
    scheme_off = i - 8
    scheme = struct.unpack_from("<I", data, scheme_off)[0]
    if scheme != SCHEME_HTTPS:
        sys.exit(f"expected _scheme=Https(2) before the host, found {scheme}")

    host = build_host(port, host)
    out = bytearray(data)
    struct.pack_into("<I", out, scheme_off, SCHEME_HTTP)
    out[i:i + len(host)] = host
    print(f"  _scheme  Https(2) -> Http(0)")
    print(f"  _host    {OLD_HOST.decode()}")
    print(f"        -> {host.decode()}")
    return bytes(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("apk", help="APK to patch in place")
    ap.add_argument("--port", type=int, default=8088,
                    help="port the bundle server listens on (default 8088)")
    ap.add_argument("--host", default="127.0.0.1",
                    help="host serving bundles: 127.0.0.1 for the on-device host app, "
                         "10.0.2.2 for a dev server on the machine running the AVD")
    args = ap.parse_args()

    with zipfile.ZipFile(args.apk) as z:
        data = z.read(ASSET)
    print(f"patching {ASSET} ({len(data)} bytes)")
    patched = patch_bytes(data, args.port, args.host)
    assert len(patched) == len(data)

    # Rewrite just this entry: stage it on disk and let `zip` replace it, so the other
    # ~1600 entries are never re-deflated.
    stage = os.path.join(os.path.dirname(os.path.abspath(args.apk)), "_cdnstage")
    dest = os.path.join(stage, ASSET)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "wb") as f:
        f.write(patched)
    rc = os.system(f'cd {stage!r} && zip -q {os.path.abspath(args.apk)!r} {ASSET!r}')
    shutil.rmtree(stage, ignore_errors=True)
    if rc:
        sys.exit(f"zip update failed ({rc})")
    print(f"updated {args.apk} -- re-sign it before installing")


if __name__ == "__main__":
    main()

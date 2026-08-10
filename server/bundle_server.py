#!/usr/bin/env python3
"""Plain-HTTP asset-bundle server: the CDN, with no TLS and no interception.

This replaces the mitmproxy path for bundle downloads. Once the client's own CDN
config points here (server/patch_cdn_config.py), nothing has to be intercepted at
all -- which removes the global http_proxy, the /system/etc/hosts bind-mount and the
system-CA bind-mount, none of which exist on an unrooted phone. It is deliberately
stdlib-only so it can be embedded as-is in an on-device host app.

Requests arrive as
    /<prefix>/Android_AssetBundles/<dated dir>/<clearname>_<hash32>.ab?v=<hash>
where <prefix> is whatever padding the in-APK config carries ("sevensins/" by
default -- see patch_cdn_config.py for why that padding exists at all).

Usage:  bundle_server.py [port] [--root DIR]
"""
import argparse, http.server, os, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ROOT = os.path.join(HERE, "patch_root")
LOG = os.path.join(HERE, "bundle_traffic.log")


def log(msg):
    line = time.strftime("%H:%M:%S ") + msg
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


class Handler(http.server.BaseHTTPRequestHandler):
    root = DEFAULT_ROOT

    def resolve(self):
        """Map a request path onto a file under root, or None.

        Bundles are requested under the dated CDN dir by their full manifest name,
        but we keep ONE flat copy in patch_root/bundles/, so the same set serves
        whatever dated path a given client build asks for. Any leading path segments
        before `Android_AssetBundles` are padding from the in-APK config and are
        ignored.
        """
        rel = self.path.split("?", 1)[0].lstrip("/")
        marker = "Android_AssetBundles/"
        if marker in rel:
            rel = rel[rel.index(marker):]
        candidate = os.path.normpath(os.path.join(self.root, rel))
        # Never let a crafted path escape the served tree.
        if not candidate.startswith(os.path.realpath(self.root)):
            return None
        if os.path.isfile(candidate):
            return candidate
        flat = os.path.join(self.root, "bundles", os.path.basename(rel))
        return flat if os.path.isfile(flat) else None

    def _serve(self, body):
        fp = self.resolve()
        if not fp:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            log(f"[{self.command}] {self.path} -> 404")
            return
        size = os.path.getsize(fp)
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.end_headers()
        if body:
            # The client HEADs each bundle first to size the download. Answering a
            # HEAD with a body makes its libcurl bail out ("Received HTTP/0.9 when
            # not allowed"), so a HEAD must send headers only.
            with open(fp, "rb") as f:
                while True:
                    chunk = f.read(1 << 20)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        log(f"[{self.command}] {self.path} -> 200 ({size}b {os.path.relpath(fp, self.root)})")

    def do_GET(self):
        self._serve(True)

    def do_HEAD(self):
        self._serve(False)

    def log_message(self, *a):
        pass          # silenced; we do our own logging


_httpd = None


def serve(port=8088, root=DEFAULT_ROOT, bind="0.0.0.0"):
    """Blocking serve loop, callable from an embedding host (the Android app)."""
    global _httpd
    Handler.root = os.path.realpath(root)
    _httpd = http.server.ThreadingHTTPServer((bind, port), Handler)
    log(f"bundle_server listening on {bind}:{port} (root={Handler.root})")
    try:
        _httpd.serve_forever()
    finally:
        _httpd = None


def shutdown():
    if _httpd is not None:
        _httpd.shutdown()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("port", nargs="?", type=int, default=8088)
    ap.add_argument("--root", default=DEFAULT_ROOT)
    ap.add_argument("--bind", default="0.0.0.0")
    args = ap.parse_args()
    serve(args.port, args.root, args.bind)


if __name__ == "__main__":
    main()

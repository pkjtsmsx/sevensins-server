"""Logging HTTPS server for the dead UJ AssetBundle patch/CDN host.
Serves on :8443 (adb reverse maps device :443 -> host :8443).
Logs every request so we learn the patch-check protocol, and serves
craftable responses.
"""
import http.server, ssl, os, time, sys

HERE = os.path.dirname(__file__)
LOG = os.path.join(HERE, "patch_traffic.log")
SERVE_DIR = os.path.join(HERE, "patch_root")   # files we serve back
os.makedirs(SERVE_DIR, exist_ok=True)

def log(msg):
    line = time.strftime("%H:%M:%S ") + msg
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")

class H(http.server.BaseHTTPRequestHandler):
    def _handle(self, method):
        host = self.headers.get("Host", "?")
        log(f"[{method}] host={host} path={self.path}")
        # serve a local file if we've staged one
        rel = self.path.split("?", 1)[0].lstrip("/")
        fp = os.path.join(SERVE_DIR, rel)
        if os.path.isfile(fp):
            with open(fp, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if method == "GET":
                self.wfile.write(data)
            log(f"    -> 200 served {rel} ({len(data)}b)")
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            log(f"    -> 404 (no staged file for {rel})")

    def do_GET(self):  self._handle("GET")
    def do_HEAD(self): self._handle("HEAD")
    def do_POST(self):
        ln = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(ln) if ln else b""
        log(f"[POST] host={self.headers.get('Host','?')} path={self.path} body={body[:200]!r}")
        self.send_response(404); self.send_header("Content-Length","0"); self.end_headers()

    def log_message(self, *a): pass  # silence default

class LoggingTLSServer(http.server.ThreadingHTTPServer):
    def get_request(self):
        # log every raw TCP connection BEFORE TLS handshake
        sock, addr = self.socket.accept()
        log(f"[CONN] tcp from {addr}")
        return sock, addr
    def handle_error(self, request, client_address):
        import traceback
        log(f"[ERR] during handshake/handle from {client_address}: "
            f"{traceback.format_exc().splitlines()[-1]}")

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8443
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(os.path.join(HERE, "uj_leaf.pem"))
    httpd = LoggingTLSServer(("0.0.0.0", port), H)
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True, do_handshake_on_connect=False)
    log(f"patch_server listening on :{port} (serve dir={SERVE_DIR})")
    httpd.serve_forever()

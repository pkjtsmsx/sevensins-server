"""mitmproxy addon: emulate the dead UserJoy 'Mars' SDK backend (service.php).
Decrypts each request, logs cmd, returns a valid encrypted reply so the client
gets past the login/connection screen.
"""
import json, sys, os, time
sys.path.insert(0, os.path.dirname(__file__))
import marscrypto as mc
from mitmproxy import http

LOG = os.path.join(os.path.dirname(__file__), "mars_traffic.log")

def log(msg):
    line = time.strftime("%H:%M:%S ") + msg
    print(line)
    with open(LOG, "a") as f:
        f.write(line + "\n")

def make_reply(entries: dict) -> bytes:
    """entries: {svrcb_key: {reply,status,...}} -> encrypted HTTP body bytes"""
    body = json.dumps({"SVRCB": entries}, separators=(",", ":"))
    return mc.encrypt_reply(body).encode()

# Request cmd -> expected reply code, recovered from the SDK's own handlers in the apk
# dex (each com.userjoy.mars.net.marsagent handler sets f175true=cmd, var=reply).
# Full table + the fields each reply handler reads: re/mars_cmd_map.txt.
# cmd+1 is NOT a safe guess -- 40/41/69/72 all reply 42, 43->45, 44->47.
CMD_REPLY = {
    11: 12, 36: 37, 38: 39, 40: 42, 41: 42, 43: 45, 44: 47, 57: 58, 67: 68,
    69: 42, 70: 71, 72: 42, 86: 87, 88: 89, 90: 91, 92: 93, 94: 95, 101: 102,
    107: 108, 109: 110, 111: 112, 115: 116,
}

def handle_cmd(cmd: int, req: dict) -> dict:
    """Return the SVRCB entries dict for a given request cmd."""
    reply = CMD_REPLY.get(cmd, cmd + 1)
    e = {"reply": reply, "status": "0"}

    # LoginByHashedAccountIdHandler: reply 47 must carry
    #   "0"=access token, "1"=login session, "2"=pass key   (all three or it bails
    #   with "Login by Hashed Acount Fail" and drops back to the title screen).
    # Optional: "3"=bound-platform list json, "4"=account level.
    # The access token is what the web API's AskTokenVerify (id=6) later receives.
    if cmd == 44:
        account_id = req.get("0", "guestacct0001")
        player_id = req.get("1", "1000001")
        e.update({
            "0": f"offline_token_{player_id}",
            "1": f"offline_session_{player_id}",
            "2": f"offline_passkey_{account_id}",
            "3": "{}",   # no bound social platforms
        })

    # RequestHashedAccountIdByPlayerIdHandler: "0"=account id, "1"=player id,
    # "2"=is-new-account, "3"=device id.
    elif cmd == 43:
        e.update({"0": "guestacct0001", "1": "1000001", "2": "0",
                  "3": req.get("session", "")})

    # RequestHashedAccountIdV2Handler (2.2.7 replaces 43 with this on the guest
    # path).  RequestHashedAccountIdV2Handler.m325byte() bails unless
    #   status == "0"  and  platformId == "9"   (9 = OneClick/guest),
    # then reads "0"=account id, "1"=player id, "2"=is-new-account,
    # "3"=OneClick password, "4"=password version (optional), and only after all
    # of that calls LoginByHashAccountId -> cmd 44.  Without platformId it logs
    # "NG... platform is not onclick" and silently returns to the sign-in sheet.
    elif cmd in (69, 40, 41, 72):
        e.update({"platformId": "9",
                  "0": "guestacct0001", "1": "1000001", "2": "0",
                  "3": "offline_oneclick_pwd", "4": "1"})

    return {"0": e}

ALLLOG = os.path.join(os.path.dirname(__file__), "all_traffic.log")
IGNORE = ("graph.facebook.com", "app-measurement.com", "googleapis.com",
          "cloud.unity3d.com", "appsflyer.com", "gstatic.com", "crashlytics",
          "google.com", "doubleclick", "googleadservices", "firebaseio")

def _alllog(msg):
    with open(ALLLOG, "a") as f:
        f.write(time.strftime("%H:%M:%S ") + msg + "\n")

def response(flow: http.HTTPFlow):
    host = flow.request.pretty_host
    if any(h in host for h in IGNORE):
        return
    # log non-analytics responses that we did NOT synthesize
    if "service.php" not in flow.request.path:
        _alllog(f"[PASS] {flow.request.method} {flow.request.pretty_url} -> "
                f"{flow.response.status_code} ({len(flow.response.content or b'')}b)")

PATCH_ROOT = os.path.join(os.path.dirname(__file__), "patch_root")

def request(flow: http.HTTPFlow):
    host = flow.request.pretty_host
    if any(h in host for h in IGNORE):
        return
    # --- AssetBundle patch/CDN host: serve staged files, else 404 ---
    if "patch" in host and "uj.com.tw" in host:
        rel = flow.request.path.split("?", 1)[0].lstrip("/")
        fp = os.path.join(PATCH_ROOT, rel)
        # Bundles are requested under the dated CDN dir by their full manifest name
        # ("<clearname>_<hash32>.ab"), but we keep one flat copy in patch_root/bundles/
        # so the same set serves whatever dated path this client build asks for.
        if not os.path.isfile(fp):
            alt = os.path.join(PATCH_ROOT, "bundles", os.path.basename(rel))
            if os.path.isfile(alt):
                fp = alt
        if os.path.isfile(fp):
            size = os.path.getsize(fp)
            # The client HEADs each bundle first to size the download. Answering a HEAD
            # with a body makes its libcurl bail out ("Received HTTP/0.9 when not
            # allowed" / "Error While Getting [...] Length"), so send headers only.
            if flow.request.method == "HEAD":
                resp = http.Response.make(
                    200, b"", {"Content-Type": "application/octet-stream"})
                resp.headers["Content-Length"] = str(size)
                flow.response = resp
                _alllog(f"[PATCH] HEAD {flow.request.pretty_url} -> 200 (len={size} {rel})")
                return
            with open(fp, "rb") as f:
                data = f.read()
            flow.response = http.Response.make(
                200, data, {"Content-Type": "application/octet-stream"})
            _alllog(f"[PATCH] {flow.request.method} {flow.request.pretty_url} -> 200 ({len(data)}b {rel})")
        else:
            flow.response = http.Response.make(404, b"", {})
            _alllog(f"[PATCH] {flow.request.method} {flow.request.pretty_url} -> 404 (no {rel})")
        return
    # --- Web API (7sins-en-web...): GET service.php?id=&time=&data=&cs= ---
    if "service.php" in flow.request.path and "web" in host:
        from urllib.parse import unquote
        api_id = flow.request.query.get("id", "?")
        data_raw = unquote(flow.request.query.get("data", "") or "")
        # best-effort "everything is fine / no update / not in maintenance" payload
        payload = {
            # 15 = clientV of the EN 2.2.4 apk (versionCode 35) installed on userjoy_re;
            # anything higher trips the "new app update available" gate.
            "version": 15, "ios_version": 15, "goo_version": 15,
            "compatibility": "0.0.0", "versionCheck": False,
            "patchServer": "https://7sins-en-patch.uj.com.tw/Android_AssetBundles/231101en/",
            "androidGoogleAdsId": "", "iosGoogleAdsId": "",
            "default_set_id": 1, "maintain": 0, "maintain_text": "",
            "url": "",  # GetNewApkUrl: empty => no new apk
        }
        # id=6 AskTokenVerify: the bridge from the Mars token to the TitanStack login
        # token. LoginUtil.UJLogin blocks here -- without a "token" it proceeds and then
        # times out on the game-server TCP connect (Error 19). See docs/GAME_SERVER.md.
        if api_id == "6":
            try:
                d = json.loads(data_raw) if data_raw else {}
            except Exception:
                d = {}
            uid = d.get("bili_uid") or "1000001"
            payload.update({
                "open_id": uid,
                "user_name": f"guest{uid}",
                "token": f"titan_token_{uid}",
                "first_login": 1,
                "session_status": 0,
            })
        body = json.dumps({"errno": 0, "data": payload}, separators=(",", ":")).encode()
        resp = http.Response.make(200, body, {"Content-Type": "application/json"})
        resp.reason = "SUCCESS"
        flow.response = resp
        _alllog(f"[WEBAPI] id={api_id} data={data_raw} -> errno0 {payload}")
        return
    if "service.php" not in flow.request.path and "uj.com.tw" not in host:
        _alllog(f"[REQ-OTHER] {flow.request.method} {flow.request.pretty_url}")
        return
    # parse p=...&e=...
    try:
        form = dict(x.split("=", 1) for x in flow.request.get_text().split("&"))
    except Exception as e:
        log(f"[!] cannot parse form on {host}: {e}")
        return
    from urllib.parse import unquote
    p = unquote(form.get("p", ""))
    plain = mc.decrypt_request(p)
    if not plain:
        log(f"[!] decrypt failed host={host} p_len={len(p)}")
        return
    try:
        req = json.loads(plain)
    except Exception:
        log(f"[?] non-json request: {plain[:200]}")
        req = {}
    cmd = req.get("cmd", -1)
    log(f"[REQ] host={host} cmd={cmd} json={plain}")

    entries = handle_cmd(cmd, req)
    reply_body = make_reply(entries)
    log(f"[RSP] cmd={cmd} entries={json.dumps(entries)}")
    resp = http.Response.make(
        200, reply_body,
        {"Content-Type": "text/html; charset=UTF-8"},
    )
    # CRITICAL: NetworkAgentBase's read loop only breaks + parses the body when the
    # response is 200 with reason phrase != "OK" (a literal "OK" makes it follow a
    # null Location and NPE into the error path). Use a non-"OK" reason.
    resp.reason = "SUCCESS"
    flow.response = resp

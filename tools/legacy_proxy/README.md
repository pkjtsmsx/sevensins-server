# Legacy proxy stack (retired 2026-08-09)

This is the man-in-the-middle stack the offline server used **before** the client was
patched to talk to us directly. It is no longer part of any workflow — kept only for the
reverse-engineered protocol knowledge it encodes. Nothing imports it.

## Why it is dead

The stack existed to intercept the client's HTTPS traffic to dead UserJoy hostnames. That
whole layer was replaced by four in-APK patches (see `../../server/patch_cdn_config.py`,
`../patch_manifest_cleartext.py`, `../patch_server_row.py`, and the `IsGENTELMAN` /
`MarsSDKEnabled` binary patches in `apkpatch/`):

- **Mars sign-in** is patched OUT (`MarsSDKEnabled -> false`), so the client no longer
  calls the Mars SDK or the web API — `mars_addon.py` / `marscrypto.py` have nothing to
  answer.
- **The asset CDN** host is patched to a local plain-HTTP server, so `patch_server.py`
  (the old HTTPS patch/CDN host) is replaced by `../../server/bundle_server.py`.
- No proxy, no `/etc/hosts` bind-mount, no system-CA install — so the mitmproxy CA in
  `../../certs/` and the `uj_leaf.pem` leaf cert here are unused.

Full write-up: memory `sevensins-apk-patch` and `sevensins-onphone-server`.

## What each file is

| file | was | reference value |
|---|---|---|
| `mars_addon.py` | mitmproxy addon emulating Mars `service.php` + the web API (id=6 AskTokenVerify, id=105) | the exact web-API response shapes |
| `marscrypto.py` | Mars packet crypto | the "FULLY REVERSED" Mars wire format cited in `docs/PROTOCOL.md` |
| `patch_server.py` | HTTPS patch/CDN server on :8443 | superseded by `bundle_server.py`; the HEAD-only rule was carried across |
| `titan_capture.py` | wire-capture helper used while reversing the titan protocol | historical |
| `uj_leaf.pem` | not in the repo — generate the mitmproxy-signed leaf locally if you run `patch_server.py` | — |

## Running it again (only if reversing more web/Mars traffic)

`mitmdump -s mars_addon.py --set connection_strategy=lazy --set upstream_cert=false`
plus the hosts + CA + proxy bring-up in `docs/GAME_SERVER.md`. `mars_addon` imports
`marscrypto` from its own directory, so keep them together.

Safe to delete this whole directory if the RE reference is no longer wanted — there is no
runtime dependency on any of it.

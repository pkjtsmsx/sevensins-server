# Seven Mortal Sins X-TASY — backend protocol notes

Game: `com.userjoy.sineng` "Seven Mortal Sins X-TASY" v2.2.7 (build 44), Unity
2019.4.40f1 il2cpp, arm64-v8a. Engine scripting: C#/il2cpp + Puerts (JS/TS) +
Mars SDK (Java). Servers shut down; this project re-creates them enough to run
the client offline.

## Client stack (what talks to what)

| Layer | Tech | Host | Notes |
|-------|------|------|-------|
| SDK / account | Mars SDK (Java, `com.userjoy.mars`) | `gmsdk-en.uj.com.tw/SINENG/game/service.php` | HttpURLConnection, honors device proxy |
| Version / login-servers / maintenance | Web API (C#, `Game.WebApi`) | `7sins-en-web.uj.com.tw/web_ob01_en/game/service.php` | UnityWebRequest (libcurl) — honors device proxy |
| Asset patch CDN | Unity AssetBundle | `7sins-en-patch.uj.com.tw/Android_AssetBundles/231101en/` | UnityWebRequest — honors device proxy |
| Game/session server | TitanStack RPC (post-login) | (unknown yet) | not reached yet |

Key gotcha: Unity's `System.Net.Dns` raw sockets do NOT use the device proxy
(need DNS/hosts), but UnityWebRequest (libcurl) DOES use the global http_proxy.
So the web API + patch traffic can all be intercepted at the mitmproxy on
`10.0.2.2:8080`; only the early `GetHostEntry` availability probe needs the hosts
redirect.

## Mars SDK service.php  (FULLY REVERSED — see tools/legacy_proxy/marscrypto.py)

Request body: `p=URLENC(Enc)&e=URLENC(proto)`

- `Enc = TrimmedB64( "n1,B64(json),n2,SIG,n5" )`, with `n1+n2+n5 == 5000`
  and `SIG = XXTEA_encrypt(key, str(n2))` (base64/trimmed).
- Cipher = **XXTEA** (`com.userjoy.mars.core.common.utils.Ccase`, delta 0x9E3779B9).
- Keys derived at load (validated):
  - request key `f290null = XXTEA_enc("=8sw","&3jax")` = `P6XbZbLUD_I`
  - response key `f289false = XXTEA_enc("atu","8*+=")` = `uMtz4bNirqo`
  - proto key `cast = XXTEA_enc("9aapruc&","S4u?Hu")` = `o3J1zdHRpZhU1AvX`
- `e = XXTEA_enc("k,666-k,3", protoKey)` (k random).

Response: **200 with reason phrase != "OK"** (a literal "OK" makes the client's
read loop follow a null Location and NPE into the error path — see
`NetworkAgentBase.doInBackground`). Body is `Enc` of:
```
{ "SVRCB": { "<any>": { "reply": <code>, "status": "0", ...fields } } }
```
`reply` must equal the handler's expected code. cmd 11 (REQUESTSETTINGS) → reply 12.
Minimal working settings reply: `{"SVRCB":{"0":{"reply":12,"status":"0"}}}`.

## Web API service.php  (Game.WebApi.WebApiRequest)

Request: `GET service.php?id=<n>&time=<ts>&data=<json>&cs=<md5-ish>` (cs is a
request checksum only; response is NOT checksum-verified).
Response: `{"errno":0,"data":{...}}` (errno 0 = OnApiOk).

`GetAllLoginServersRequest` response fields (the version/maintenance gate):
`version, ios_version, goo_version, compatibility, versionCheck(bool),
patchServer, androidGoogleAdsId, iosGoogleAdsId, default_set_id,
maintain(0=ok), maintain_text`. Set `versionCheck=false`, `maintain=0`,
`version` == client to clear the "new app update available" popup.
`GetNewApkUrlRequest` (id=104, data `{"platform":"tw_android"}`) returns the
update APK url (leave empty ⇒ no update).

## Asset patch  (CURRENT BLOCKER)

BundleHost `…/Android_AssetBundles/231101en/`. Client GETs:
- `remoteconfig` — RemoteConfig._Asset JSON (net-monitor knobs only; 404 is
  harmless, uses RemoteConfig.Default).
- `Android?v=<ts>` — the top-level manifest. **CONFIRMED (IDA):** downloaded via
  `SimpleAssetBundleDownloader` → `UnityWebRequestAssetBundle` /
  `DownloadHandlerAssetBundle.GetContent` ⇒ it MUST be a real Unity **AssetBundle**
  (a raw/empty/JSON body gives "Failed to decompress data for the AssetBundle").
  Then `bundle.LoadAsset("AssetBundleManifest")` cast to
  `UnityEngine.AssetBundleManifest` → registered via
  `AssetBundleManifestManager.Add(manifest, location=2 [remote])`, while
  `ToCompatibilityAssetBundleManifest(3 [local])` registers the local PAD packs.
  It then compares remote-vs-local Hash128 per bundle; equal ⇒ no download.
  So: serve a **standard Unity `AssetBundleManifest` bundle named `Android`**
  whose bundle names + Hash128 match the local packs (⇒ zero downloads).
  `Add` reads it via the `IAssetBundleManifest` iface: GetAllAssetBundles(),
  GetAssetBundleHash(name), GetAllDependencies(name). Bundle names are the
  **hashed names** = local assetpack filenames (`<name>_<hash32>`);
  `ExtractHashedName` splits name+hash. Hash128 = the 32-hex suffix (4 LE u32).
  NOTE: `JsonUtility.FromJson<CompatibilityAssetBundleManifest>` exists but is a
  DIFFERENT/local path — the remote `Android` is a real Unity manifest bundle.
  Two ways forward: (A) build the manifest bundle (UnityPy 1.25, or reconstruct
  from local pack names/hashes/deps) or (B) binary-patch libil2cpp to skip the
  remote-manifest requirement (TB-style). **DECISION PENDING.**

## Device / emulator setup (see tools/)

- AVD `userjoy_re`: android-31 google_apis **x86_64** (arm64 via NDK translation),
  4G RAM. `ro.product.cpu.abilist = x86_64,arm64-v8a`.
- `setenforce 0` (SELinux Permissive) — REQUIRED or netd ignores the bind-mounted
  hosts (silent). This was the big early blocker.
- mitmproxy CA installed into system store via tmpfs overlay of
  `/system/etc/security/cacerts` (verity blocks remount).
- hosts redirect for `7sins-en-patch.uj.com.tw → 127.0.0.1` bind-mounted +
  `adb reverse tcp:443 tcp:8443` (only needed for the raw-socket availability probe).
- Global proxy: `settings put global http_proxy 10.0.2.2:8080`.
- mitmproxy: `mitmdump -s tools/legacy_proxy/mars_addon.py --set connection_strategy=lazy --set upstream_cert=false`
  (lazy + no upstream cert lets it answer without the dead origin).

## Design data (game tables) — the real ceiling

53 `DesignXForm : DesignFormBase<DesignXRow>` ScriptableObjects loaded by
`DesignManager` from `design/pack/<name>.asset` (52 forms; "Design forms count: 52").
**NOT in the APK** (scanned all 1623 packs: 0 design hits; all local containers are
`assets/game/*` or `packages/*`). Was CDN-hosted (`231101en`), now gone; **not on
web.archive.org** (patch/web/sdk hosts never crawled). Content unrecoverable; only
the *schema* is (il2cpp). Empty forms → past DesignInitiator → toward login.

Serving a plain valid bundle as `Android` (no `AssetBundleManifest` asset) makes
`LoadAsset("AssetBundleManifest")` return null → `Add(null)` no-op → local PAD packs
used → passes AssetInitiator. But design forms still "not in resources" (not local).

Tooling proven for empty-form fabrication:
- `TypeTreeGeneratorAPI` (venv `.venv`, needs dotnet) + `gen.load_local_dll_folder(DummyDll)`
  → `get_nodes("Assembly-CSharp", "Game.Design.DesignXForm")` works (il2cpp direct load fails).
- `server/form_map.json` = definitive name→class map (51 forms; wordfilter added).
- UnityPy 1.25 has typed ctors (MonoScript/MonoBehaviour/AssetBundle/AssetBundleManifest)
  BUT no clean from-scratch object-add API → authoring bundles is fragile.

**DEEP FINDING (loader traced):** `DesignManager._LoadAndParse_d__38.MoveNext` does
`ResManager.Acquire<ScriptableObject>("design/pack/<name>.asset")` per form; on error it
sets `DesignManager.Error` + aborts. A branch-flip to skip the error is NOT enough — the
next step casts the (null) ResObj to IDesignForm and calls an interface method → NPE. A
real fix needs code injection to `ScriptableObject.CreateInstance(FormType)` per form.

**OBSOLETE — this section's conclusion was wrong.** It claimed revival was "effectively
blocked by lost server-side data". A real `design_pack` was subsequently recovered (see
`server/design_pack_source.ab`), so the forms are populated with genuine data and none of
the empty-shell fabrication below is needed. The client now boots, logs in, syncs all 14
subsystems, reaches the home scene and plays campaign stage 1-1 to completion against
`server/titan_server.py`. See `docs/GAME_SERVER.md` §6.

What IS still missing from that pack is narrow and worked around rather than fatal:
DesignText row 499 (the battle formation table) is absent and is injected by
`server/patch_design.py`, and ~13.2k skill rows carry the developers' `"Unuseful"`
placeholder in the English columns, backfilled from the Traditional-Chinese/Japanese
columns by the same script. The pack predates EN 2.2.4, so a few rows the EN client wants
simply never existed in it.

**ACHIEVED:** client boots through ALL network gates (Mars XXTEA auth fully reversed +
emulated, web-API version/maintenance gate, asset manifest via the LoadAsset(null) trick),
loads all local PAD art/audio, and stops only at the missing server-side design tables.

## il2cpp dump location

`re/il2cpp227/` for the 2.2.7 client, `re/il2cpp/` for 2.2.4 — each an Il2CppDumper run
holding `dump.cs`, `script.json`, `il2cpp.h`, `stringliteral.json` and `DummyDll/`.

**Neither is in the repo.** They are ~130 MB apiece, rebuildable from the client, and not
ours to redistribute, so `.gitignore` excludes them; `docs/BRINGUP.md` covers producing
your own. `design_data.py` needs only the `DummyDll/` half, and prefers `il2cpp227` —
2.2.4 type trees applied to a 2.2.7 pack parse to garbage without erroring.

The IDA database has symbols applied from `script.json`. MCP: ida-pro-mcp.

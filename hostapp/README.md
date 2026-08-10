# Seven Sins Host

Runs the Seven Sins server **on the phone**, so the game plays offline with no PC, no
proxy and no root. Two listeners:

| port  | server            | serves                                    |
|-------|-------------------|-------------------------------------------|
| 22110 | `titan_server.py` | the game protocol (raw TCP)               |
| 8088  | `bundle_server.py`| asset bundles over plain HTTP             |

The game APK must be patched to match — see `../server/patch_cdn_config.py`,
`../server/patch_manifest_cleartext.py` and `../server/patch_server_row.py --host
127.0.0.1`, and `../docs/GAME_SERVER.md`.

## Build

Needs JDK 17 and an Android SDK with `android-35`. No Python dependencies are
downloaded: the whole server is stdlib-only, which is why there is no `pip` block in
`app/build.gradle`.

```sh
ANDROID_SDK_ROOT=/path/to/android-sdk ./gradlew assembleDebug
# -> app/build/outputs/apk/debug/SevenSinsHost.apk   (~58 MB)
```

`stageServer` copies the server out of `../server` at build time (there is one copy of
the truth, and it is not this directory): the five runtime modules, the warm
`design_cache`, the design pack, the AssetBundleManifest and a seed account.

### "design cache was NOT built from the staged pack"

The build fails deliberately. `design_data` decides its cache is stale by hashing the
pack, so the shipped cache must have been built from the shipped pack — and re-running
`patch_server_row.py` changes the pack. On device there is no UnityPy to reparse with, so
shipping a mismatch would mean serving tables that disagree with the client's own data.

Fix by re-warming the cache against the current pack, then rebuild:

```sh
cd ../server
../.venv/bin/python - <<'EOF'
import design_data as dd, UnityPy
env = UnityPy.load(dd.PACK)
forms = sorted({o.read(check_read=False).m_Name for o in env.objects
                if o.type.name == "MonoBehaviour"})
for f in forms:
    try: dd.warm(f)
    except Exception: pass          # a few client-only forms have no type tree
EOF
```

(Three forms — `editor_msg`, `shop_goods`, `soulbook_kizuna` — cannot generate type trees
and are not read by the server.)

## Running

1. Install the APK and open it.
2. Grant the battery exemption when asked. **Without it Android freezes the server the
   moment the game takes the foreground**, and the game hangs on a socket that never
   answers. On realme/OPPO/Xiaomi also allow background activity and auto-launch, or the
   vendor's own freezer suspends it anyway.
3. Import the assets (below).
4. Tap **Start server**, then launch the game. Log in with ID `1000001` (the password is
   ignored — the ID selects the account).

Player state lives in the app's private storage and survives APK updates; clearing the
app's data resets it.

## Importing assets

The bundle set is the game's own content (~2.6 GB of art, Live2D, audio, prefabs) and is
**not** shipped — only the manifest and the design pack are. Build a tar from your own
copy:

```sh
cd ../server
tar -cf sevensins_assets.tar -C patch_root bundles Android_AssetBundles
```

Copy it to the phone, then **Import assets (tar)** and pick it. The import streams the
picked file straight into a native `tar -x`, so it never writes a second copy and does
not need twice the free space. `.tar.gz` is detected automatically.

A flat archive of bare `.ab` files also works if extracted into `bundles/`:
`bundle_server` falls back to looking a request up by basename there, which is what lets
one flat set serve whatever dated CDN path a client build asks for.

Stop the server before importing.

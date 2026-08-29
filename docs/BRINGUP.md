# Bring-up: from a fresh clone to a working server

The code in this repository is complete. The **data is not**, and cannot be — the design
tables and type information belong to the game. A fresh clone therefore passes exactly one
check (pyflakes) and fails all 31 test suites, every one of them on the same root cause:

```
RuntimeError: design cache miss and no DummyDll staged under re/*/DummyDll --
cannot reparse design_pack_<hash>.ab
```

That is not a broken clone. It is the expected state until you supply three things from
your own copy of the game. Run `python3 tools/check_setup.py` at any point to see which of
them are still missing.

## What you must supply

| # | Input | Goes in | Needed for |
| --- | --- | --- | --- |
| 1 | `design_pack_<hash>.ab` | `server/patch_root/bundles/` | every table the server reads |
| 2 | Il2CppDumper `DummyDll/*.dll` | `re/il2cpp227/DummyDll/` | regenerating type trees for #1 |
| 3 | `UnityPy`, `TypeTreeGeneratorAPI` | pip — see `tools/SETUP.md` | reading the bundle at all |

Items 1 and 2 come off a device or an APK you already have. Neither is redistributable, so
neither is in this repo and neither ever should be.

### 1. The design pack

The pack is a Unity asset bundle named `design_pack_<hash>.ab`, where the hash changes
whenever the pack is repacked. `design_data.py` finds it by glob and takes the newest, so
the exact name does not matter.

On a device with the game installed, the bundles live in the Unity download cache:

```
/sdcard/Android/data/<package>/files/UnityCache/Shared/<clearname>/<hash>/__data
```

Each `__data` **is** the bundle, byte-exact — the surrounding directory names are the
clearname → hash map. `adb pull` reads this path even on Android 11+, where third-party
file managers cannot. Note the cache only holds bundles that were actually downloaded, so
a more-played account yields a more complete set.

Only the design pack is needed for the server and its tests. The **full** `patch_root`
(~2.5 GB) is needed only to serve a real client — see below.

### 2. The DummyDlls

The client is an il2cpp build, so its asset bundles carry **no type trees**: without them
UnityPy sees the design forms as opaque bytes. The trees are regenerated from the dumped
assemblies via `TypeTreeGeneratorAPI`.

Produce them by running Il2CppDumper over two files from the APK:

- `lib/arm64-v8a/libil2cpp.so`
- `assets/bin/Data/Managed/Metadata/global-metadata.dat`

Stage its `DummyDll` output directory at `re/il2cpp227/DummyDll/`. `design_data.py` prefers
`il2cpp227` and falls back to `il2cpp`.

**The dump must come from the same client build as the pack.** Applying 2.2.4 type trees to
a 2.2.7 pack does not fail loudly — it silently yields garbage rows, which is the case
`_extract` warns about. If you change client versions, re-dump.

## Then

```sh
python3 tools/check_setup.py        # confirms 1-3 are in place
python3 tools/compile_skills.py     # per-cast battle specs
python3 tools/compile_statuses.py   # status catalogue
python3 tools/run_tests.py          # expect 32/32
python3 server/titan_server.py      # listens on 22110
```

The first command that touches a table builds `server/design_cache/` from the pack, which
takes ~30 s and only happens again when the pack changes. Everything after that reads the
cache. The two compilers are what produce `server/battle_data/skills/` and `statuses.json`;
without them nothing battle-related works, and they are deliberately untracked, so a fresh
clone has to run them once.

## Running a real client against it

The steps above give you a server and a green suite. To also play the game against it you
need the **full** `patch_root` — both `bundles/` and `Android_AssetBundles/` — served by
`server/bundle_server.py`, and the client repointed at your server. The client finds the
server through the design pack's own server row, not through a binary patch:
`tools/patch_server_row.py` rewrites it and repacks the bundle (which is why
`TypeTreeGeneratorAPI` is required even if you never run a test).

See `docs/GAME_SERVER.md` for the login chain and `hostapp/README.md` for the on-device
build.

## Building the Android host app

`./gradlew assembleDebug` needs the same game data, and fails loudly without it:

```
no design pack staged
no design_cache/.source staged
```

`stageServer` copies `server/design_cache/` and a minimal `patch_root` into the APK, then
verifies the staged cache's stamp against the staged pack's SHA-1 — so a cache built from a
different pack is caught at build time rather than on the phone.

Two more things the build wants that a fresh clone will not have:

- `hostapp/local.properties` with `sdk.dir`, and optionally `sevensins.updateRepo` if you
  want the in-app updater pointed at a channel of your own.
- `server/accounts/1000001.json`, staged as a seed account so a fresh install can log
  straight in. Account files are never committed; without one the build still succeeds and
  the app simply starts with no seed account.

## Why none of this ships here

`server/patch_root/`, `server/design_cache/`, `re/il2cpp*/` and `server/accounts/` are all
gitignored. The first three are the game's data or derived directly from its binary; the
last is somebody's save. `tools/build_hostapp_update.py` enforces the same split from the
other side — it ships a fixed file list that excludes all of them, so a hot update can
never overwrite a save.


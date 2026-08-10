#!/usr/bin/env python3
"""Repoint the client's server list at our own host, by patching the design pack.

WHY THIS IS NOT AN APK PATCH. The game server address is not baked into
libil2cpp.so. `SceneLogin.OnStartButtonClicked` (0x3427968) fetches DesignServerForm,
looks the row up by PlayerSession's serverId, reads `_address`, splits it on ',' into
host/port and builds a ServerAddress. `FrontendWebRequest.Send` (0x17fcd84) does the
same lookup for `_web_api_address` and uses it as the web root that GetApiUri
concatenates module paths onto. Both fields therefore live in the design pack -- which
WE serve -- so redirecting the client is a data change, not a binary change, and it
replaces the iptables DNAT rule from docs/GAME_SERVER.md 3b entirely.

The address list is '<host>,<port>' entries joined by '_'. OnStartButtonClicked picks
one at RANDOM (Random.Range(0, count)), so we publish exactly one entry to make the
choice deterministic.

Output keeps the SAME FILENAME as the input. That is deliberate and is the opposite of
patch_design.py's rule: the filename hash here is the bundle's identity in the EN 2.2.7
AssetBundleManifest we serve, not a content digest (the shipped pack's md5 does not
match its own name), so renaming it would make the manifest point at a bundle that no
longer exists. The cost is that a device which already cached the bundle keeps its
stale copy -- clear the Unity cache, or reinstall, after running this.

This is 2.2.7 machinery. patch_design.py is the 2.2.4 tool and its source pack and
DummyDll are both stale; its text-499 and localization injections are unnecessary here
because the genuine EN 2.2.7 pack already carries real English text.
"""
import argparse, glob, os, shutil, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
# Anchor the served data at <project>/server explicitly, so this tool resolves the
# same whether it lives in server/ or tools/ (it operates ON server data, it is not
# part of the runtime).
SERVER = os.path.join(ROOT, "server")
BUNDLES = os.path.join(SERVER, "patch_root", "bundles")
DUMMY_DLL = os.path.join(ROOT, "re", "il2cpp227", "DummyDll")
UNITY_VERSION = "2019.4.40f1"

# EVERY row is redirected, not just 601 "Fallen Eden". With the Mars SDK disabled the
# login panel exposes its Select Server button and does not necessarily default to 601 --
# a fresh install came up on 602 "Limbo Park" -- and a row we did not patch still holds a
# dead public IP, which just stalls for ~14s and fails. There is no reason for an offline
# server to leave any entry pointing elsewhere.
SERVER_ROW = None       # None = all rows


def find_pack():
    packs = glob.glob(os.path.join(BUNDLES, "design_pack_*.ab"))
    if not packs:
        sys.exit("no design_pack_*.ab in patch_root/bundles")
    if len(packs) > 1:
        sys.exit(f"expected exactly one design pack, found {len(packs)}")
    return packs[0]


def pristine_copy(pack):
    """Always patch from an untouched pack, never from a previous output.

    Each pass re-serializes the whole MonoBehaviour, so chaining edits would compound
    any round-trip damage. The first run takes the backup; later runs read it.
    """
    backup = pack + ".pristine"
    if not os.path.isfile(backup):
        shutil.copy2(pack, backup)
        print(f"saved pristine copy -> {os.path.basename(backup)}")
    return backup


def patch(pack, host, port, web_api):
    import UnityPy
    from TypeTreeGeneratorAPI import TypeTreeGenerator

    gen = TypeTreeGenerator(UNITY_VERSION)
    for dll in glob.glob(os.path.join(DUMMY_DLL, "*.dll")):
        with open(dll, "rb") as f:
            gen.load_dll(f.read())

    env = UnityPy.load(pristine_copy(pack))
    done = False
    for obj in env.objects:
        if obj.type.name != "MonoBehaviour":
            continue
        mb = obj.read(check_read=False)
        if mb.m_Name != "server":
            continue
        script = mb.m_Script.read()
        nodes = [{"m_Type": n.m_Type, "m_Name": n.m_Name,
                  "m_Level": n.m_Level, "m_MetaFlag": n.m_MetaFlag}
                 for n in gen.get_nodes("Assembly-CSharp",
                                        f"{script.m_Namespace}.{script.m_ClassName}")]
        before = obj.get_raw_data()
        tree = obj.read_typetree(nodes)
        for row in tree["_rows"]:
            if SERVER_ROW is not None and row.get("_id") != SERVER_ROW:
                continue
            print(f"  row {row.get('_id')} {row.get('_server_name')!r}: "
                  f"{row['_address']!r} -> {host},{port}")
            row["_address"] = f"{host},{port}"
            if web_api:
                row["_web_api_address"] = web_api
            done = True
        if not done:
            sys.exit("no server rows matched")
        after = obj.save_typetree(tree, nodes)
        # The generated type tree does not round-trip the m_Script PPtr -- the rewritten
        # object comes back with the top bytes of m_PathID zeroed, so Unity cannot bind
        # the form's script and LoadAsset returns null. The MonoBehaviour header is
        # fixed-size and untouched by a field edit, so restore it verbatim.
        header = 12 + 4 + 4 + 8            # m_GameObject, m_Enabled+pad, m_Script
        if after[:header] != before[:header]:
            obj.set_raw_data(before[:header] + after[header:])
        break
    if not done:
        sys.exit("no 'server' form in the pack")

    # Keep it compressed: an uncompressed re-save turns a 6 MB download into ~30 MB.
    data = env.file.save(packer="lz4")
    tmp = pack + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    # Replace via rename so the shipped pack's hardlink to the asset dump is broken
    # rather than written through.
    os.replace(tmp, pack)
    print(f"wrote {os.path.basename(pack)} ({len(data)} bytes)")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="10.0.2.2",
                    help="host the client dials (default: the AVD's alias for the "
                         "host machine)")
    ap.add_argument("--port", type=int, default=22110,
                    help="titan_server.py's listen port (default: 22110)")
    ap.add_argument("--web-api", default=None,
                    help="replacement web root, e.g. https://10.0.2.2:8443/web_ob01_en/ "
                         "(omit to leave the shipped URL alone)")
    args = ap.parse_args()
    pack = find_pack()
    print(f"patching {os.path.basename(pack)}")
    patch(pack, args.host, args.port, args.web_api)
    print("\nThe filename is unchanged, so a device that already cached this bundle "
          "will keep using its stale copy -- reinstall or clear the Unity cache.")


if __name__ == "__main__":
    main()

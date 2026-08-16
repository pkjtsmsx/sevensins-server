#!/usr/bin/env python3
"""Remove the censor circles from the Soulmirror portraits, server-side.

**These are DATA, not code.** `SoulFragMagicCircle.SetData` (0x18A95A4) is handed a
`DesignHModelInfoRow.SoulFragMagicCircle`, and the placement comes from two columns of
the `h_model_info` form we serve:

    portraitSoulFragMagicCircleLimitString   "2_-72.0_-160.0_48.0_-166.0_1.0_1.0_12.0_12.0"
    portraitSoulFragMagicCircleSuperString   "1_-52.0_-24.0_0.8_30.0"

i.e. `<count>_<x1>_<y1>[_<x2>_<y2>]_<scale...>_<rot...>`. 136 of 261 rows carry one, and
the strings repeat across casts -- which is why the same marks appear on everybody.

`IsGENTELMAN` does NOT reach this. That flag hides censor SPRITES (`DMMHider.Awake` zeroes
their alpha) and suppresses the Live2D magic circle (`ValidateLive2DMagicCircle` only ADDS
it when the flag is false). The Soulmirror portrait draws whatever its row lists either
way, which is why one screen stayed censored in an otherwise uncensored build.

**Why a byte edit rather than a proper re-serialize:** rewriting the form needs typetrees
(TypeTreeGeneratorAPI + the DummyDll set) and re-emits every row. Setting the leading
COUNT to 0 is a single-character change per string, so the edit is length-preserving and
the object's bytes can be written back untouched otherwise. Zero circles is a state the
data itself demonstrates -- row id 1 ships both columns empty.

The output takes a new `design_pack_<md5>.ab` name and the manifest entry is repointed
(name + AssetBundleHash), which is what makes a device re-download it.

**Re-warm the server's design cache afterwards** with the venv python: design_data keys
its cache on the pack's hash, so a new pack drops all 52 cached forms and the system
python cannot reparse them (no TypeTreeGeneratorAPI):

    .venv/bin/python -c "import sys;sys.path.insert(0,'server');import design_data as dd;\
        [dd.rows(f) for f in (...)]"
"""
import glob
import hashlib
import json
import os
import shutil
import sys

import UnityPy

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(HERE, "server")
BUNDLES = os.path.join(SERVER, "patch_root", "bundles")
MANIFEST = os.path.join(SERVER, "patch_root", "Android_AssetBundles", "231101en", "Android")
CACHE = os.path.join(SERVER, "design_cache", "h_model_info.json")
COLUMNS = ("portraitSoulFragMagicCircleLimitString",
           "portraitSoulFragMagicCircleSuperString")


def circle_strings():
    """Every non-empty circle string, read from the parsed cache."""
    with open(CACHE, encoding="utf-8") as fh:
        rows = json.load(fh)
    out = set()
    for row in rows.values():
        for col in COLUMNS:
            value = (row.get(col) or "").strip()
            if value and value[0].isdigit() and value[0] != "0":
                out.add(value)
    return out


def served_pack():
    packs = [p for p in glob.glob(os.path.join(BUNDLES, "design_pack_*.ab"))]
    if not packs:
        sys.exit("no design_pack_*.ab in patch_root/bundles")
    return max(packs, key=os.path.getmtime)


def main():
    wanted = circle_strings()
    src = served_pack()
    print(f"{len(wanted)} distinct circle strings; patching {os.path.basename(src)}")
    env = UnityPy.load(src)
    hits = 0
    for obj in env.objects:
        if obj.type.name != "MonoBehaviour":
            continue
        raw = obj.get_raw_data()
        new = raw
        for value in wanted:
            blob = value.encode()
            if blob in new:
                hits += new.count(blob)
                new = new.replace(blob, b"0" + blob[1:])   # count -> 0, same length
        if new is not raw and new != raw:
            if len(new) != len(raw):
                sys.exit("length changed -- refusing to write")
            obj.set_raw_data(new)
    if not hits:
        sys.exit("no circle strings found -- already patched?")
    blob = env.file.save(packer="lz4")
    digest = hashlib.md5(blob).hexdigest()
    out = os.path.join(BUNDLES, f"design_pack_{digest}.ab")
    with open(out, "wb") as fh:
        fh.write(blob)
    print(f"neutralised {hits} occurrences -> {os.path.basename(out)}")

    shutil.copy2(MANIFEST, MANIFEST + ".bak-precircle")
    env = UnityPy.load(MANIFEST)
    for obj in env.objects:
        if obj.type.name != "AssetBundleManifest":
            continue
        tree = obj.read_typetree()
        target = None
        for i, (idx, name) in enumerate(tree["AssetBundleNames"]):
            if name.startswith("design_pack_"):
                tree["AssetBundleNames"][i], target = (idx, os.path.basename(out)), idx
        raw = bytes.fromhex(digest)
        for idx, info in tree["AssetBundleInfos"]:
            if idx == target:
                for k in range(16):
                    info["AssetBundleHash"][f"bytes[{k}]"] = raw[k]
        obj.save_typetree(tree)
        break
    with open(MANIFEST, "wb") as fh:
        fh.write(env.file.save())
    print("manifest repointed; restart bundle_server, re-warm the design cache, "
          "and relaunch the client")


if __name__ == "__main__":
    main()

#!/usr/bin/env python
"""Give the Rider Soulmirror banner (gacha box 1006) art that matches what it sells.

**Why this exists.** Only two Soulmirror banner atlases shipped -- `atlas_banner_gacha08_en`
("SOUL MIRRORS SUMMON / Sins") and `..._gacha09_en` ("/ Virtues"). There is no Rider one,
because that banner's box data was live-ops. Box 1006 therefore fell back to sprite 768 =
`atlas_banner_gacha07_en`, which is the **Limbo Discovery** event banner, with tab sprite
778 = chip '7', literally captioned "LIMBO DISCOVERY". Both halves advertised the wrong
thing for a banner that rolls real Rider soulmirrors.

**What it does**, entirely server-side -- no APK patch, no design-pack edit, no new sprite
rows, because the sprite ids and rects stay exactly as they are:

  1. builds a Riders banner from the Sins one: the faction word is overprinted (not
     erased) with "Riders" in the same white-fill/orange-stroke treatment. Erasing it
     cleanly is not possible with what we have -- the word's white halo touches the
     "MMON" outline, so every fill either smears the letters or reads as a flat patch;
  2. writes that over `atlas_banner_gacha07_en`, and copies the "Soulmirrors Summon"
     chip ('9', at 768,329) over chip '7' (at 576,148) in `atlas_banner_gacha_tab01_en`;
  3. repacks the bundle (lz4 -- an uncompressed save triples it to ~36MB) under a NEW
     name keyed by the content md5, and rewrites the manifest entry: BOTH the filename
     and the 16-byte AssetBundleHash. That is the cache bust. The client caches under
     UnityCache/Shared/<clearname>/<hash>, so reusing the old name/hash means every
     device keeps showing the art it already has.

Re-running is safe: it locates the entry by prefix, so it patches whatever the manifest
currently points at. The previous manifest is kept as `Android.bak-prebanner`.

The character cards still show Sin casts -- there is no Rider card art to swap in -- and
the title font is FreeSerif Bold Italic, not the game's brush script.
"""

import hashlib
import os
import shutil
import sys

import UnityPy
from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUNDLES = os.path.join(HERE, "patch_root", "bundles")
MANIFEST = os.path.join(HERE, "patch_root", "Android_AssetBundles", "231101en", "Android")
BUNDLE_PREFIX = "ngui_atlases_icon_banner_"

SRC_ATLAS = "atlas_banner_gacha08_en"      # Soul Mirrors Summon / Sins -- the donor
DST_ATLAS = "atlas_banner_gacha07_en"      # what box 1006's sprite 768 points at
TAB_ATLAS = "atlas_banner_gacha_tab01_en"
TAB_SRC = (768, 329, 192, 182)             # chip '9', "Soulmirrors Summon"
TAB_DST = (576, 148)                       # chip '7', was "LIMBO DISCOVERY"

FONT = "/usr/share/fonts/gnu-free/FreeSerifBoldItalic.otf"
WORD_POS = (440, 610)                      # covers the script "Sins" at (450,612)-(552,668)
FONT_SIZE = 48


def riders_banner(sins):
    """The Sins soulmirror banner with its faction word overprinted as "Riders"."""
    font = ImageFont.truetype(FONT, FONT_SIZE)
    # A dark blurred pass first: the backdrop there is a bright glow, and white-on-bright
    # alone does not separate. Then the orange stroke + white fill the game's labels use.
    glow = Image.new("RGBA", sins.size, (0, 0, 0, 0))
    ImageDraw.Draw(glow).text(WORD_POS, "Riders", font=font, fill=(120, 40, 10, 170),
                              stroke_width=10, stroke_fill=(120, 40, 10, 170))
    out = Image.alpha_composite(sins, glow.filter(ImageFilter.GaussianBlur(6)))
    text = Image.new("RGBA", sins.size, (0, 0, 0, 0))
    ImageDraw.Draw(text).text(WORD_POS, "Riders", font=font, fill=(255, 255, 255, 255),
                              stroke_width=6, stroke_fill=(247, 146, 42, 255))
    return Image.alpha_composite(out, text)


def current_bundle():
    names = [f for f in os.listdir(BUNDLES)
             if f.startswith(BUNDLE_PREFIX) and f.endswith(".ab")
             and not any(x in f for x in ("_cht_", "_rev_", "_exp_"))]
    if len(names) != 1:
        # After a re-run the old file is still on disk; prefer whatever the manifest names.
        wanted = manifest_entry()[1]
        names = [n for n in names if n == wanted] or names[:1]
    return os.path.join(BUNDLES, names[0])


def manifest_entry():
    """-> (index, filename) of the icon/banner bundle in the served manifest."""
    env = UnityPy.load(MANIFEST)
    for obj in env.objects:
        if obj.type.name != "AssetBundleManifest":
            continue
        tree = obj.read_typetree()
        for idx, name in tree["AssetBundleNames"]:
            if (name.startswith(BUNDLE_PREFIX)
                    and not any(x in name for x in ("_cht_", "_rev_", "_exp_"))):
                return idx, name
    raise SystemExit("icon/banner bundle not found in the manifest")


def repoint_manifest(new_name, digest):
    shutil.copy2(MANIFEST, MANIFEST + ".bak-prebanner")
    env = UnityPy.load(MANIFEST)
    for obj in env.objects:
        if obj.type.name != "AssetBundleManifest":
            continue
        tree = obj.read_typetree()
        target = None
        names = tree["AssetBundleNames"]
        for i, (idx, name) in enumerate(names):
            if (name.startswith(BUNDLE_PREFIX)
                    and not any(x in name for x in ("_cht_", "_rev_", "_exp_"))):
                names[i], target = (idx, new_name), idx
        raw = bytes.fromhex(digest)
        for idx, info in tree["AssetBundleInfos"]:
            if idx == target:
                for k in range(16):
                    info["AssetBundleHash"][f"bytes[{k}]"] = raw[k]
        obj.save_typetree(tree)
        break
    with open(MANIFEST, "wb") as fh:
        fh.write(env.file.save())


def main():
    path = current_bundle()
    print(f"patching {os.path.basename(path)}")
    env = UnityPy.load(path)
    sins = None
    for obj in env.objects:
        if obj.type.name == "Texture2D" and obj.read().m_Name == SRC_ATLAS:
            sins = obj.read().image.copy()
    if sins is None:
        raise SystemExit(f"{SRC_ATLAS} not in the bundle")
    banner = riders_banner(sins)

    patched = set()
    for obj in env.objects:
        if obj.type.name != "Texture2D":
            continue
        data = obj.read()
        if data.m_Name == DST_ATLAS:
            if data.image.size != banner.size:
                raise SystemExit(f"size mismatch {data.image.size} vs {banner.size}")
            data.image = banner
            data.save()
            patched.add("banner")
        elif data.m_Name == TAB_ATLAS:
            img = data.image.copy()
            x, y, w, h = TAB_SRC
            img.paste(img.crop((x, y, x + w, y + h)), TAB_DST)
            data.image = img
            data.save()
            patched.add("tab")
    if patched != {"banner", "tab"}:
        raise SystemExit(f"only patched {patched}")

    blob = env.file.save(packer="lz4")
    digest = hashlib.md5(blob).hexdigest()
    out = os.path.join(BUNDLES, f"{BUNDLE_PREFIX}{digest}.ab")
    with open(out, "wb") as fh:
        fh.write(blob)
    repoint_manifest(os.path.basename(out), digest)
    print(f"wrote {os.path.basename(out)} ({len(blob):,} bytes)")
    print(f"manifest repointed; restart bundle_server and relaunch the client")


if __name__ == "__main__":
    main()

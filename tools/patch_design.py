#!/usr/bin/env python3
"""Patch missing rows into the design pack we serve.

The recovered design tables are from an older build than the EN 2.2.4 client, so a
few rows the client hard-depends on simply are not there. The client cannot be
changed; the data can, because we host the bundle.

Currently injected:
  text 499  -- battle formation defaults. FormationSetup.InitFormationDataDefault
              does GetText(499), deserializes it into BattleFormationData and, when
              it comes back empty, logs "字串(499)為戰鬥站位預設資料為必填!" and leaves
              FormationDataDefault null. BattleUnit.ResetLayerTransforms then NPEs
              for the first unit spawned, killing the whole battle scene init.
              The class is built through .ctor(List<List<float>> player,
              List<List<float>> enemy) -- so the JSON keys are the CTOR PARAMETER
              names, plus "ps"/"es" from the [JsonProperty] thunks for the scales.
              Each entry is [x, y, z]; the ctor's enemy loop is bounded by the
              PLAYER list length, so both lists must be the same size.

Output goes to patch_root/bundles/design_pack_<md5>.ab under a NEW hash, because the
manifest hash is what invalidates the device's bundle cache -- reusing the old name
would leave the client happily using its stale copy. Run build_manifest.py after.
"""
import glob, hashlib, json, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
# Anchor server data at <project>/server explicitly so this build tool resolves the
# same whether it lives in server/ or tools/.
SERVER = os.path.join(ROOT, "server")
BUNDLES = os.path.join(SERVER, "patch_root", "bundles")
DUMMY_DLL = os.path.join(ROOT, "re", "il2cpp", "DummyDll")
UNITY_VERSION = "2019.4.40f1"

# Five field slots per side, indexed by LightBattleChar.Index (0-based).
# Synthesised: the real values died with the CDN. Teams face each other along Z
# (enemy dolls get localScale.z = -1), so the player side sits at -Z. Tune by eye.
# CALIBRATION LATTICE (temporary). The true positions died with the CDN and cannot be
# derived statically: the battle camera prefab sits at the origin with an animator
# driving its real pose, and InitCamera only clones and parents it.
#
# So: publish a big lattice of candidate slots instead of 5 real ones, and let the
# SERVER choose which lattice index each unit stands on (LightBattleChar.Index).
# Position sweeps then need only a server restart, not a re-patched bundle plus a
# fresh 4 MB download on the device.
#
# index = row * GRID + col  ->  x = (col - GRID/2) * STEP, z = (row - GRID/2) * STEP
# Reconstructed by calibration against the battle camera (the opening cinematic uses
# a different pose, so only frames with the "Turn 1" HUD count). Measured mapping:
#   screen_x_px(2000 wide) ~= 122.5 * z - 220     -> visible band is z in [2, 18]
# i.e. world +Z is screen-right and world X is the into-screen depth axis. Enemy
# dolls are mirrored (localScale.z = -1), so the player side takes the lower z.
# Both teams stand on a diagonal running from lower-left (player front) to upper-right
# (enemy back). Reconstructed from a screenshot of the live game by inverting the
# projection measured against this camera (screen space normalised to 2000x900):
#   screen_x ~= 122.5 * z - 220 - 8.75 * x
#   screen_y ~= 575 + 28.75 * x
# So world +Z runs screen-right and world -X moves a unit up-screen (further away).
FORMATION_499 = {
    "ps": 1.0,
    "player": [[-6.0, 0.0, 6.50],    # slot 0: back rank, up and right of slot 1
               [0.4, 0.0, 5.65],     # slot 1: front-left corner of the field
               [-3.0, 0.0, 7.50],
               [-8.0, 0.0, 7.50],
               [-5.0, 0.0, 8.50]],
    "es": 1.0,
    # BattleFormationData's ctor walks the enemy list with the PLAYER list's length,
    # so the two must stay the same size.
    # Nudged up-and-right from the raw solve so the lead mob sits centred in the
    # tutorial's target highlight rather than just in front of it.
    "enemy": [[-8.4, 0.0, 11.85],
              [-5.4, 0.0, 13.20],
              [-1.7, 0.0, 14.60],
              [-7.0, 0.0, 14.05],
              [-3.5, 0.0, 15.75]],
}

TEXT_ROWS = {
    499: json.dumps(FORMATION_499, separators=(",", ":")),
}


# Always patch from the pristine bundle, never from the previous output: each pass
# re-serializes every object, so chaining them would compound any round-trip damage.
SOURCE = os.path.join(HERE, "design_pack_source.ab")


# The developers' own stub for "this cell was never localized". It fills the English
# and Simplified-Chinese columns of ~13.2k of the 13.9k skill rows, which is why every
# skill in an English client is called "Unuseful".
# The item form uses a different stub: a bare "-" in the EN/SC columns (item 205,
# the Premium Awaker Scroll, shows as "-" in the gacha confirm dialog because of it).
PLACEHOLDERS = {"Unuseful", "-"}
PLACEHOLDER = "Unuseful"   # kept for reference; use PLACEHOLDERS for checks

# Localized columns to backfill, per form: {form: [(target, [sources...]), ...]}.
# The real English strings only ever existed in the EN CDN's design pack, which we do
# not have, so the best available text is the Traditional-Chinese / Japanese column
# that DOES carry real content. Only cells equal to PLACEHOLDER are touched, so this
# stays a narrow, reversible transform: drop in a real pack later and nothing here
# has to be unwound.
LOCALIZED_BACKFILL = {
    "skill": [("_name_en", ["_name", "_name_jp"]),
              ("_name_sc", ["_name", "_name_jp"]),
              ("_note1_en", ["_note1", "_note1_jp"]),
              ("_note1_sc", ["_note1", "_note1_jp"]),
              ("_note2_en", ["_note2", "_note2_jp"]),
              ("_note2_sc", ["_note2", "_note2_jp"])],
    "item": [("_itemName_en", ["_itemName", "_itemName_jp"]),
             ("_itemName_sc", ["_itemName", "_itemName_jp"]),
             ("_itemName_2Lines_en", ["_itemName_2Lines", "_itemName_2Lines_jp"]),
             ("_itemName_2Lines_sc", ["_itemName_2Lines", "_itemName_2Lines_jp"]),
             ("_note1_en", ["_note1", "_note1_jp"]),
             ("_note1_sc", ["_note1", "_note1_jp"])],
}


def _inject_text_rows(rows):
    """Add/replace whole rows in the text form (currently just the formation row)."""
    by_id = {r["_id"]: r for r in rows}
    changed = 0
    for row_id, text in TEXT_ROWS.items():
        # every localized column gets the same value: the client picks one by
        # language and an empty pick is what triggers the "required!" error
        row = by_id.get(row_id) or dict(rows[0], _id=row_id)
        for key in row:
            if key.startswith("_text"):
                row[key] = text
        if row_id in by_id:
            rows[rows.index(by_id[row_id])] = row
        else:
            rows.append(row)
        changed += 1
    rows.sort(key=lambda r: r["_id"])
    return changed


def _backfill_localized(rows, rules):
    """Replace placeholder cells with the first source column that has real text."""
    changed = 0
    for row in rows:
        for target, sources in rules:
            if row.get(target) not in PLACEHOLDERS:
                continue
            for src in sources:
                value = row.get(src)
                if value and value not in PLACEHOLDERS:
                    row[target] = value
                    changed += 1
                    break
    return changed


def main():
    import UnityPy
    from TypeTreeGeneratorAPI import TypeTreeGenerator

    gen = TypeTreeGenerator(UNITY_VERSION)
    for dll in glob.glob(os.path.join(DUMMY_DLL, "*.dll")):
        with open(dll, "rb") as f:
            gen.load_dll(f.read())

    src = SOURCE
    if not os.path.isfile(src):
        sys.exit(f"missing pristine pack {src}")
    env = UnityPy.load(src)
    wanted = {"text"} | set(LOCALIZED_BACKFILL)
    patched = {}
    for obj in env.objects:
        if obj.type.name != "MonoBehaviour":
            continue
        mb = obj.read(check_read=False)
        form = mb.m_Name
        if form not in wanted:
            continue
        script = mb.m_Script.read()
        nodes = [{"m_Type": n.m_Type, "m_Name": n.m_Name,
                  "m_Level": n.m_Level, "m_MetaFlag": n.m_MetaFlag}
                 for n in gen.get_nodes("Assembly-CSharp",
                                        f"{script.m_Namespace}.{script.m_ClassName}")]
        before = obj.get_raw_data()
        tree = obj.read_typetree(nodes)
        rows = tree["_rows"]
        changed = 0
        if form == "text":
            changed += _inject_text_rows(rows)
        if form in LOCALIZED_BACKFILL:
            changed += _backfill_localized(rows, LOCALIZED_BACKFILL[form])
        patched[form] = changed
        after = obj.save_typetree(tree, nodes)
        # The generated type tree does not round-trip the m_Script PPtr: the rewritten
        # object comes back with the top bytes of m_PathID zeroed, so Unity cannot bind
        # the DesignTextForm script and LoadAsset returns null ("A scripted object has a
        # different serialization layout when loading. (Read 36 bytes but expected N)").
        # The MonoBehaviour header is fixed-size and unchanged by our edit, so restore
        # it verbatim from the original bytes -- only the rows array should differ.
        header = 12 + 4 + 4 + 8            # m_GameObject, m_Enabled+pad, m_Script
        if after[:header] != before[:header]:
            obj.set_raw_data(before[:header] + after[header:])
    missing = wanted - set(patched)
    if missing:
        sys.exit(f"forms not found in the pack: {sorted(missing)}")

    # keep it compressed: an uncompressed re-save turns a 4 MB download into 31 MB
    data = env.file.save(packer="lz4")
    digest = hashlib.md5(data).hexdigest()
    out = os.path.join(BUNDLES, f"design_pack_{digest}.ab")
    with open(out, "wb") as f:
        f.write(data)
    for old in glob.glob(os.path.join(BUNDLES, "design_pack_*.ab")):
        if old != out:
            os.remove(old)
    summary = ", ".join(f"{form}: {n} cell(s)" for form, n in sorted(patched.items()))
    print(f"patched {summary} from {os.path.basename(src)} -> "
          f"{os.path.basename(out)} ({len(data)} bytes)")
    print("now run: build_manifest.py")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Count the DAMAGE swings in every skill cinematic, and check them against `hit`.

**Why this is ground truth.** The client consumes exactly one `DamageInfo` group per
`BscTagKind.Damage` tag the cinematic fires (`AttackBehavior.BscTag`, case 5). That read
is GUARDED -- `if (DmgInfo.Count >= 1)` -- so sending too few groups does not error: the
swing simply animates with no damage number and the fight carries on. It is a silent,
cosmetic failure, which is exactly why it survived unnoticed.

So the number of groups the server must send is dictated by the ANIMATION, not by the
design row. `DesignSkillRow.hit` is the design data's *declaration* of the same number,
and this tool measures whether the two agree.

Structure, from the 2.2.7 dump:

    BscDataRes (ScriptableObject, named after DesignSkillRow._actName)
      └ _runtime : BscRuntimeData
          └ _tracks[] : BscTrackData
              └ _timelines[] : BscTimelineData
                   ...of which BscTagTimelineData with `_tag == BscTagKind.Damage (5)` is one swing.

`BscHitTimelineData` is NOT the thing to count -- it is a collision record (hitter/hittee
colliders, `finalHit`) and does not correspond 1:1 with a damage number.

Everything is PPtr-linked between MonoBehaviour objects in the SAME bundle, so the walk
is local -- no cross-bundle resolution needed.

**Requires `TypeTreeGeneratorAPI`** (the dotnet-backed native package):

    pip install --user --break-system-packages TypeTreeGeneratorAPI

The PEP 668 override is needed on Arch-likes and only writes to ~/.local, which is where
UnityPy already lives. Without it none of this is readable: raw-byte parsing returns None
for the object names.

Do NOT pass hand-built node lists to `read_typetree(nodes)` for these types -- that path
raises `read_str out of bounds` because the MonoBehaviour base nodes end up doubled.
Attach the generator to the environment and call `read_typetree()` with no arguments;
UnityPy then asks the generator for the right tree per object.

    tools/skill_cinematics.py [--bundle NAME] [--mismatch] [--json OUT]
"""
import argparse
import collections
import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "server")
DUMMY_DLL = os.path.join(ROOT, "re/il2cpp227/DummyDll")
BUNDLES = os.path.join(SERVER, "patch_root/bundles")
UNITY_VERSION = "2019.4.40f1"

# The cinematics all live here; the per-character art_character_* bundles hold only
# models, animation clips and face textures.
DEFAULT_BUNDLE = "data_battle"

# BscTagKind.Damage -- the tag AttackBehavior.BscTag consumes one DamageInfo group for.
TAG_DAMAGE = 5


def _generator():
    import UnityPy  # noqa: F401  (imported for the side effect of a clear error)
    from UnityPy.helpers.TypeTreeGenerator import TypeTreeGenerator
    gen = TypeTreeGenerator(UNITY_VERSION)
    dlls = glob.glob(os.path.join(DUMMY_DLL, "*.dll"))
    if not dlls:
        sys.exit(f"no DummyDll assemblies under {DUMMY_DLL}")
    for dll in dlls:
        with open(dll, "rb") as f:
            gen.load_dll(f.read())
    return gen


def cinematic_hits(bundle_glob=DEFAULT_BUNDLE):
    """-> {cinematic name: number of BscHitTimelineData swings}."""
    import UnityPy
    gen = _generator()
    out = {}
    files = sorted(glob.glob(os.path.join(BUNDLES, f"*{bundle_glob}*.ab")))
    if not files:
        sys.exit(f"no bundle matching *{bundle_glob}*.ab under {BUNDLES}")
    for path in files:
        env = UnityPy.load(path)
        env.typetree_generator = gen
        objs = {o.path_id: o for o in env.objects}

        # Classify every MonoBehaviour once by its script class.
        kind = {}
        for pid, o in objs.items():
            if o.type.name != "MonoBehaviour":
                continue
            try:
                kind[pid] = o.read(check_read=False).m_Script.read().m_ClassName
            except Exception:                                   # noqa: BLE001
                pass

        def tree(pid):
            return objs[pid].read_typetree()

        for pid, cls in kind.items():
            if cls != "BscDataRes":
                continue
            try:
                res = tree(pid)
                name = res.get("m_Name") or "?"
                runtime = tree((res.get("_runtime") or {}).get("m_PathID"))
                swings = 0
                for track_ref in (runtime.get("_tracks") or []):
                    track = tree(track_ref.get("m_PathID"))
                    for tl in (track.get("_timelines") or []):
                        tp = tl.get("m_PathID")
                        # **Count TAGS, not hits.** BscHitTimelineData is a collision
                        # (hitter/hittee colliders, `finalHit`) and does not correspond
                        # 1:1 with a damage number -- counting those gave swing counts up
                        # to 12 against a `hit` column that maxes at 5. What the client
                        # consumes is a BscTagKind.Damage TAG, which is
                        # BscTagTimelineData with _tag == 5.
                        if kind.get(tp) != "BscTagTimelineData":
                            continue
                        if tree(tp).get("_tag") == TAG_DAMAGE:
                            swings += 1
                out[name] = swings
            except Exception:                                   # noqa: BLE001
                continue
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bundle", default=DEFAULT_BUNDLE,
                    help="substring of the bundle filename (default: data_battle)")
    ap.add_argument("--mismatch", action="store_true",
                    help="list skills whose `hit` disagrees with the cinematic")
    ap.add_argument("--json", help="write {cinematic: swings} here")
    args = ap.parse_args()

    hits = cinematic_hits(args.bundle)
    print(f"cinematics read: {len(hits)}")
    dist = collections.Counter(hits.values())
    print("swing distribution:", dict(sorted(dist.items())))
    if args.json:
        with open(args.json, "w") as f:
            json.dump(hits, f, indent=1, sort_keys=True)
        print("wrote", args.json)

    # Join to the skill form by _actName.
    sys.path.insert(0, SERVER)
    os.chdir(SERVER)
    import battle as bt
    rows = bt.dd.rows("skill") or {}
    # Three buckets, because "no Damage tag" is NOT a disagreement -- it selects the
    # other rendering path. `AttackBehavior.DoAllDamage` iterates every group and every
    # row inside it, so a tagless cinematic consumes the whole list at once and the
    # group count does not have to match anything. `hit` still governs how many swings
    # the skill is *meant* to deal.
    agree = disagree = fallback = 0
    bad = []
    for sid, r in rows.items():
        act = (r.get("_actName") or "").strip()
        if not act or act not in hits:
            continue
        declared = int(r.get("_count") or 0)
        actual = hits[act]
        if actual == 0:
            fallback += 1
        elif declared == actual:
            agree += 1
        else:
            disagree += 1
            bad.append((sid, r.get("_name_en") or r.get("_name"), act, declared, actual))
    total = agree + disagree + fallback
    tagged = agree + disagree
    print(f"\nskills joined to a cinematic: {total}")
    print(f"  tagless (DoAllDamage path): {fallback}")
    print(f"  tagged, hit == swings     : {agree}")
    print(f"  tagged, DISAGREE          : {disagree}"
          + (f"   ({100.0 * agree / tagged:.1f}% agreement)" if tagged else ""))
    if args.mismatch:
        for sid, name, act, declared, actual in sorted(bad)[:60]:
            print(f"    {sid} {str(name)[:28]:<28} {act:<16} hit={declared} swings={actual}")


if __name__ == "__main__":
    main()

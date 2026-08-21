#!/usr/bin/env python3
"""Enumerate every AVG decision scene in the game, from the AVG bundle.

**Why this exists.** The `avg` design form is NOT in the shipped pack, so nothing in
design data says which story scenes carry a choice. Until now the only way to find one
was to play into it and watch the server log -- which is how a first playthrough can
miss them silently, since a scene with no choice looks identical from outside.

The scripts are not in the design pack at all; they are MonoBehaviours in
`data_avg_<hash>.ab`, one per scene, named by avg id. Each carries:

    dialogueFrames[]  .character.charID / .speaker_en   the speaker of each line
                      .strID_en                          the line
    ending            .type == 1 with .options[]         <- THIS IS A DECISION
                      each option: .str_en and the .avgID it branches to

`ending.type == 1` plus a non-empty `options` list is the marker. Everything else is a
plain cutscene that never reaches the server.

Note `character.charID` is an **avg_role id**, not a char row id -- Matina speaks as
5301, not 10821 -- so it joins through `player_state.karma._role_char_map()`.

    python3 avg_decisions.py [--json OUT] [--chapter N]
"""
import argparse
import collections
import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "server")
sys.path.insert(0, SERVER)

BUNDLE_GLOB = os.path.join(SERVER, "patch_root/bundles/data_avg_*.ab")
DLL_GLOB = os.path.join(ROOT, "re/il2cpp227/DummyDll/*.dll")
UNITY_VERSION = "2019.4.40f1"


def load_scenes():
    """-> {avg id (str): typetree dict} for every scene in the AVG bundle."""
    import UnityPy
    from UnityPy.helpers.TypeTreeGenerator import TypeTreeGenerator

    bundles = glob.glob(BUNDLE_GLOB)
    if not bundles:
        raise SystemExit(f"no AVG bundle at {BUNDLE_GLOB}")
    gen = TypeTreeGenerator(UNITY_VERSION)
    for dll in glob.glob(DLL_GLOB):
        with open(dll, "rb") as f:
            gen.load_dll(f.read())

    out = {}
    for path in bundles:
        env = UnityPy.load(path)
        env.typetree_generator = gen
        for obj in env.objects:
            if obj.type.name != "MonoBehaviour":
                continue
            tree = obj.read_typetree()
            name = str(tree.get("m_Name") or "")
            if name:
                out[name] = tree
    return out


def decisions(scenes):
    """-> {avg id (int): {options, speakers}} for the scenes that carry a choice."""
    found = {}
    for name, tree in scenes.items():
        ending = tree.get("ending") or {}
        options = ending.get("options") or []
        if ending.get("type") != 1 or not options:
            continue
        speakers = collections.Counter()
        for frame in (tree.get("dialogueFrames") or []):
            ch = frame.get("character") or {}
            if ch.get("charID"):
                speakers[(int(ch["charID"]), ch.get("speaker_en") or "")] += 1
        try:
            key = int(name)
        except ValueError:
            continue
        found[key] = {
            "options": [{"text": o.get("str_en"), "goto": o.get("avgID")}
                        for o in options],
            "speakers": [{"role": r, "name": n, "lines": c}
                         for (r, n), c in speakers.most_common()],
        }
    return found


# Lucifer narrates most scenes; skipping her surfaces the other cast on screen.
PROTAGONIST_ROLE = 101


def featured_cast(info, role_map, chars):
    """-> (charID, name, lines) for the most-spoken non-protagonist cast in the scene.

    **NOT the payout character.** That was the first guess and play disproved it: every
    chapter-1 decision pays Jacqueline, yet she speaks in only one of the three -- 10103
    has no Jacqueline lines at all and still pays her. The reward follows the CHAPTER's
    antagonist (player_state.karma.KARMA_CHAPTER_CHAR), which is not in any shipped file.

    Still useful as a shortlist: it is right for every confirmed chapter from 3 on, and
    wrong only for the opening arc. Treat a row as a candidate to confirm, never a fact.
    """
    for sp in info["speakers"]:
        if sp["role"] == PROTAGONIST_ROLE:
            continue
        cid = role_map.get(sp["role"])
        if cid and cid in chars:
            return cid, sp["name"], sp["lines"]
    return None, None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", help="write the full table here")
    ap.add_argument("--chapter", type=int, help="only this chapter")
    a = ap.parse_args()

    from player_state import karma
    import design_data as dd

    scenes = load_scenes()
    found = decisions(scenes)
    stages = dd.rows("stage") or {}
    index = karma._avg_stage_index()

    role_map = karma._role_char_map()
    chars = dd.rows("char") or {}
    rows = []
    for avg, info in sorted(found.items()):
        stage = index.get(avg)
        chapter = karma.avg_chapter(avg)
        row = stages.get(stage) or {}
        cid, cname, lines = featured_cast(info, role_map, chars)
        rows.append({
            "avg": avg, "chapter": chapter, "stage": stage,
            "title": row.get("_title_en"), "name": row.get("_stage_name_en"),
            "featured": cid, "featured_name": cname, "featured_lines": lines,
            **info,
        })

    if a.chapter:
        rows = [r for r in rows if r["chapter"] == a.chapter]

    print(f"{len(found)} decision scenes in the pack"
          + (f"; {len(rows)} in chapter {a.chapter}" if a.chapter else "") + "\n")
    print(f"{'avg':>7} {'ch':>3} {'stage':>6}  {'featured cast':16s} scene")
    for r in rows:
        who = r["featured_name"] or "--"
        print(f"{r['avg']:>7} {str(r['chapter']):>3} {str(r['title'] or ''):>6}  "
              f"{who[:16]:16s} {str(r['name'] or '')[:30]}")

    by_ch = collections.Counter(r["chapter"] for r in rows)
    print("\nper chapter: " + ", ".join(f"ch{c}={n}" for c, n in sorted(
        by_ch.items(), key=lambda kv: (kv[0] is None, kv[0]))))

    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(rows, f, indent=1, ensure_ascii=False)
        print(f"\nwrote {a.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

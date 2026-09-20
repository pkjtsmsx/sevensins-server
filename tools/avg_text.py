#!/usr/bin/env python3
"""Dump every story line in the game, one JSON per AVG scene.

The scripts live in `data_avg_*.ab`, not the design pack -- see avg_decisions.py, whose
load_scenes() does the UnityPy/typetree work. That tool filters to the scenes carrying a
decision; this one keeps every dialogue frame.

Output is verbatim game text, so it is untracked like skill_effects.json.

    python3 avg_text.py [-o docs/avg_text.json]
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from avg_decisions import load_scenes  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--out", default="avg_text.json")
    a = ap.parse_args()

    out = {}
    for name, tree in sorted(load_scenes().items()):
        lines = []
        for f in (tree.get("dialogueFrames") or []):
            ch = f.get("character") or {}
            lines.append({
                "role": ch.get("charID"),
                "speaker": ch.get("speaker_en") or ch.get("speaker"),
                "en": f.get("strID_en"),
                "zh": f.get("strID"),
            })
        ending = tree.get("ending") or {}
        out[name] = {
            "lines": lines,
            "options": [{"en": o.get("str_en"), "zh": o.get("str"),
                         "goto": o.get("avgID")}
                        for o in (ending.get("options") or [])],
        }

    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1, ensure_ascii=False)
    print(f"{len(out)} scenes, {sum(len(s['lines']) for s in out.values())} lines"
          f" -> {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

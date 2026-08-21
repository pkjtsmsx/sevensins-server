import sys, os, collections, json
ROOT = "/mnt/mainspace/reverse/sevensins"
sys.path.insert(0, ROOT + "/tools")
sys.path.insert(0, ROOT + "/server")
os.chdir(ROOT + "/server")
import skill_cinematics as sc
import battle as bt

hits = sc.cinematic_hits()
rows = bt.dd.rows("skill") or {}
bad = []
for sid, r in rows.items():
    act = (r.get("_actName") or "").strip()
    if not act or act not in hits:
        continue
    d, a = int(r.get("_count") or 0), hits[act]
    if a == 0 or d == a:
        continue
    bad.append((sid, r, act, d, a))

print("genuine disagreements:", len(bad))
print("\nby (hit, swings):")
for k, n in collections.Counter((d, a) for _, _, _, d, a in bad).most_common():
    print(f"   hit={k[0]} swings={k[1]}   x{n}")

print("\nby distinct cinematic (how many skills share each):")
per = collections.Counter(act for _, _, act, _, _ in bad)
print("   distinct cinematics involved:", len(per))
for act, n in per.most_common(12):
    ex = [(d, a) for _, _, x, d, a in bad if x == act][0]
    print(f"   {act:<18} x{n:<4} hit={ex[0]} swings={ex[1]}")

json.dump([[sid, act, d, a] for sid, _, act, d, a in bad],
          open(ROOT + "/tools/scratch/cine_disagree.json", "w"), indent=1)

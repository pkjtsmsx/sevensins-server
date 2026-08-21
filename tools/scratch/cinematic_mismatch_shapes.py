import sys, os, json, collections
ROOT = "/mnt/mainspace/reverse/sevensins"
sys.path.insert(0, ROOT + "/tools")
sys.path.insert(0, ROOT + "/server")
os.chdir(ROOT + "/server")
import skill_cinematics as sc
hits = sc.cinematic_hits()
import battle as bt
rows = bt.dd.rows("skill") or {}
pat = collections.Counter()
for sid, r in rows.items():
    act = (r.get("_actName") or "").strip()
    if not act or act not in hits:
        continue
    d, a = int(r.get("_count") or 0), hits[act]
    if d == a:
        continue
    pat[(d, a)] += 1
print("mismatch shapes (declared hit, cinematic swings) -> count:")
for (d, a), n in pat.most_common(12):
    print(f"   hit={d} swings={a}  x{n}")
zero = sum(n for (d, a), n in pat.items() if a == 0)
print(f"\nof the mismatches, cinematic had ZERO damage tags: {zero}")

import sys, os, collections
ROOT = "/mnt/mainspace/reverse/sevensins"
sys.path.insert(0, ROOT + "/tools"); sys.path.insert(0, ROOT + "/server")
os.chdir(ROOT + "/server")
import skill_cinematics as sc
import battle as bt
TYPE = {1:"COM_ATTACK",2:"SKILL",3:"SP_SKILL",4:"PASSIVE",5:"SUPPORT",6:"STATUS",
        10:"GOD_ITEM",101:"COL1",102:"COL2",103:"COL3"}
hits = sc.cinematic_hits()
rows = bt.dd.rows("skill") or {}
bad = []
for sid, r in rows.items():
    act = (r.get("_actName") or "").strip()
    if not act or act not in hits: continue
    d, a = int(r.get("_count") or 0), hits[act]
    if a == 0 or d == a: continue
    bad.append((sid, r, act, d, a))

print("by skill TYPE:")
for k, n in collections.Counter(TYPE.get(r.get("_type"), r.get("_type")) for _, r, _, _, _ in bad).most_common():
    print(f"   {str(k):<12} x{n}")

print("\nhit=0 rows -- what type, and do they even deal damage?")
z = [(sid, r, act, a) for sid, r, act, d, a in bad if d == 0]
for k, n in collections.Counter(TYPE.get(r.get("_type"), r.get("_type")) for _, r, _, _ in z).most_common():
    print(f"   {str(k):<12} x{n}")
print("\n  samples:")
for sid, r, act, a in z[:6]:
    note = (r.get("_note1_en") or r.get("_note1") or "").replace("\n", " ")[:78]
    print(f"   {sid} {TYPE.get(r.get('_type'))}: {act} swings={a}")
    print(f"       {note}")

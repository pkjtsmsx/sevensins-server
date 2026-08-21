import sys, os, re, collections
ROOT = "/mnt/mainspace/reverse/sevensins"
sys.path.insert(0, ROOT + "/tools"); sys.path.insert(0, ROOT + "/server")
os.chdir(ROOT + "/server")
import skill_cinematics as sc
import battle as bt
TYPE = {1:"COM_ATTACK",2:"SKILL",3:"SP_SKILL",4:"PASSIVE"}
WORD = {"once":1,"twice":2,"two times":2,"three times":3,"four times":4,"five times":5,
        "2 times":2,"3 times":3,"4 times":4,"5 times":5}
hits = sc.cinematic_hits()
rows = bt.dd.rows("skill") or {}
res = collections.Counter(); rowsout = []
for sid, r in rows.items():
    act = (r.get("_actName") or "").strip()
    if not act or act not in hits: continue
    d, a = int(r.get("_count") or 0), hits[act]
    if a == 0 or d == a: continue
    if r.get("_type") == 4:          # PASSIVE never attacks
        continue
    note = (r.get("_note1_en") or "").lower()
    said = None
    for w, v in WORD.items():
        if w in note: said = v; break
    if said is None and re.search(r"deals?\s+\d+%", note): said = 1
    verdict = ("prose=%s" % said) if said else "prose silent"
    if said is not None:
        if said == a: res["prose agrees with CINEMATIC"] += 1
        elif said == d: res["prose agrees with hit"] += 1
        else: res["prose agrees with neither"] += 1
    else:
        res["prose silent"] += 1
    rowsout.append((sid, TYPE.get(r.get("_type")), act, d, a, said, note[:60]))
print("real (non-PASSIVE) disagreements:", len(rowsout))
for k, v in res.most_common():
    print(f"   {k:<28} {v}")
print()
for x in rowsout[:14]:
    print(f"   {x[0]} {x[1]:<10} {x[2]:<14} hit={x[3]} swings={x[4]} said={x[5]}")
    print(f"       {x[6]}")

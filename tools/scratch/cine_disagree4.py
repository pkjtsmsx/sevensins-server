import sys, os, re, collections
ROOT = "/mnt/mainspace/reverse/sevensins"
sys.path.insert(0, ROOT + "/tools"); sys.path.insert(0, ROOT + "/server")
os.chdir(ROOT + "/server")
import skill_cinematics as sc
import battle as bt
WORD = {"once":1,"twice":2,"two times":2,"three times":3,"four times":4,"five times":5}
hits = sc.cinematic_hits(); rows = bt.dd.rows("skill") or {}
groups = collections.defaultdict(list)
for sid, r in rows.items():
    act = (r.get("_actName") or "").strip()
    if not act or act not in hits: continue
    d, a = int(r.get("_count") or 0), hits[act]
    if a == 0 or d == a or r.get("_type") == 4: continue
    note = (r.get("_note1_en") or "")
    said = None
    for w, v in WORD.items():
        if w in note.lower(): said = v; break
    dev = "不該看到" in (r.get("_note1") or "")
    # is this a player cast (bch*) or a mob (bmo*)?
    fam = "mob" if act.startswith("bmo") else ("player" if act.startswith("bch") else "other")
    groups[(fam, said, dev)].append((sid, act, d, a, note[:70]))
print("real disagreements by (family, prose says, dev-placeholder):")
for k in sorted(groups, key=lambda x: (-len(groups[x]), str(x))):
    fam, said, dev = k
    print(f"   {fam:<7} prose={str(said):<5} dev={dev!s:<5}  x{len(groups[k])}")
print()
for k in sorted(groups, key=lambda x: -len(groups[x])):
    if k[2]:
        continue
    print(f"=== {k}")
    for sid, act, d, a, note in groups[k][:5]:
        print(f"   {sid} {act:<14} hit={d} swings={a}  {note!r}")

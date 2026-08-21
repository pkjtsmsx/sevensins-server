import sys, os, collections
ROOT = "/mnt/mainspace/reverse/sevensins"
sys.path.insert(0, ROOT + "/tools"); sys.path.insert(0, ROOT + "/server")
os.chdir(ROOT + "/server")
import skill_cinematics as sc
import battle as bt
hits = sc.cinematic_hits(); rows = bt.dd.rows("skill") or {}

def real_player_attack(r, act):
    if r.get("_type") in (4, 6):                 # PASSIVE / STATUS never attack
        return False
    if not act.startswith("bch"):                # player casts only
        return False
    note = (r.get("_note1_en") or "").strip()
    if not note or note in ("無",):              # empty / "none"
        return False
    if "不該看到" in (r.get("_note1") or ""):     # dev placeholder rows
        return False
    return True

pop = agree = dis = 0
bad = []
for sid, r in rows.items():
    act = (r.get("_actName") or "").strip()
    if not act or act not in hits or hits[act] == 0:
        continue
    if not real_player_attack(r, act):
        continue
    pop += 1
    if int(r.get("_count") or 0) == hits[act]:
        agree += 1
    else:
        dis += 1
        bad.append((sid, act, r.get("_count"), hits[act], (r.get("_name_en") or "")))
print("REAL player attack skills with a tagged cinematic and English prose:", pop)
print("   hit == cinematic swings :", agree)
print("   DISAGREE                :", dis)
for b in bad[:10]:
    print("     ", b)

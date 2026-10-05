#!/usr/bin/env python3
"""Which LAYER is wrong when a fight looks broken: the engine, the data, or the pairing?

    python3 tools/selfcheck.py

Run it on the HOST -- the desktop checkout, or over adb inside the app's snapshot dir --
and read the four blocks. Every number is read from the install it is run against; nothing
is assumed.

IT EXISTS BECAUSE THE TWO HALVES SHIP SEPARATELY. `server/battle_data/skills/` is generated
by tools/compile_skills.py and gitignored, while `build_hostapp_update.py` reads the WORKING
TREE -- so an update built without a recompile ships new engine code against old compiled
data, and every symptom of that looks like an engine bug. Block 4 names that case outright.

Contributed; the three fights and the verdict logic are kept as submitted, with the host
paths and a couple of API details corrected against this tree.
"""
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(os.path.dirname(HERE), "server")
sys.path.insert(0, SERVER if os.path.isdir(SERVER) else HERE)


def mod(name):
    try:
        __import__("engine." + name)
        return True
    except Exception:
        return False


print("== 1. engine features present")
feat = {
    "pursuit figures (pursuit_values)": mod("pursuit_values"),
    "ATK+DEF second half (multi_basis)": mod("multi_basis"),
    "recovered DoT ticks (dot_values)": mod("dot_values"),
    "prose gates (prose_gates)": mod("prose_gates"),
    "mis-assembled clauses (clause_fixes)": mod("clause_fixes"),
}
for k, v in feat.items():
    print(f"     {'yes' if v else 'NO '}    {k}")

print("\n== 2. compiled data fingerprint")
from engine import specs                                       # noqa: E402

S = specs.skills()
dmg = [e for s in S.values() for e in s["effects"] if e["op"] == "damage"]
fu = [e for s in S.values() for e in s["effects"] if e["op"] == "follow_up"]
trig = [e for s in S.values() if s["type"] == "passive"
        for e in s["effects"] if e.get("trigger")]
with_coef = sum(1 for e in dmg if e.get("coefficient") is not None)
print(f"     skills compiled                {len(S)}")
print(f"     damage effects                 {len(dmg)}, with a coefficient {with_coef}")
print(f"     follow-ups                     {len(fu)}")
print(f"     passive effects WITH a trigger {len(trig)}"
      f"     <- 0 means the data predates the trigger work")

print("\n== 3. three fights with known answers")
from engine import core, status as ST                          # noqa: E402


def U(o, t, hp=100000, atk=3000, df=1000):
    u = core.Unit(order=o, team=t, max_hp=hp, hp=hp, atk=atk, defence=df, spd=100)
    u.skills = []
    u.scv = 0.0
    return u


def basic():
    for i, s in S.items():
        if s["type"] == "com_attack" and any(
                e["op"] == "damage" and e.get("coefficient") for e in s["effects"]):
            return i, s
    return None, None


bid, bspec = basic()
if bspec is None:
    print("     no basic attack carries a coefficient -- the data is unusable")
else:
    a, b = U("101", 1), U("201", 2)
    out = core.execute(a, bspec, [a, b], random.Random(1), chosen="201")
    total = sum(x.amount or 0 for x in out.strikes)
    print(f"     plain basic attack ({bspec['name']}): {total}")
    print("       3000 ATK vs 1000 DEF should land in the low thousands; a two-digit")
    print("       number means coefficients are missing from the data.")

    a, b = U("101", 1), U("201", 2, atk=5000)
    ST.apply_event(a, core.StatusEvent(target="101", status_id=4001, name="Shield",
                                       applied=True, duration=2, flat=7500), b)
    hp0 = a.hp
    core.execute(b, bspec, [a, b], random.Random(2), chosen="101")
    left = [x.shield_hp for x in a.statuses if x.kind == "shield"]
    print(f"     7500 shield vs one basic attack: hp lost {hp0 - a.hp}, shield left {left}")
    print("       hp lost must be 0 while the shield holds. Any loss means `absorb` is")
    print("       not being reached.")

two = None
if feat["ATK+DEF second half (multi_basis)"]:
    from engine import multi_basis as mb
    two = next((sid for sid in mb.SECOND_COMPONENT if sid in S), None)
if two:
    a, b = U("101", 1), U("201", 2, df=2000)
    out = core.execute(a, S[two], [a, b], random.Random(1), chosen="201")
    halves = [x.detail.get("second_amount") for x in out.strikes]
    print(f"     ATK+DEF skill {S[two]['name']}: second halves {halves}")
    print("       all None means multi_basis is installed but not wired into execute.")
else:
    print("     ATK+DEF skill: SKIPPED (multi_basis not installed)")

print("\n== 4. verdict")
if not any(feat.values()):
    print("     None of the engine fixes are installed. This is the stock build.")
elif len(trig) == 0:
    print("     Engine has the fixes, compiled data does NOT (no passive carries a")
    print("     trigger). Run tools/compile_skills.py -- battle_data/skills is generated")
    print("     and gitignored, so an update built without that step ships old data.")
elif len(dmg) and with_coef < len(dmg) * 0.5:
    print("     Most damage effects have no coefficient: the compiled data is broken, or")
    print("     came from a different pack than design_cache.")
else:
    print("     Engine and data agree. If a fight still looks wrong the problem is in")
    print("     what reaches the phone, or in the wire, not in these two.")

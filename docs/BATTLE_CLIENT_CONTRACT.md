# The battle client contract

**What the CLIENT knows, and what the server must invent.** Everything here is derived
from `libil2cpp.so` (2.2.7) and the shipped design pack — *not* from our server code.
This exists because parts of it have been worked out before and lost.

Rule of thumb established by decompiling: **the client renders, the server decides.**
There is no damage calculation anywhere in the client (no `CalcDamage`/`GetDamage`; the
`Formula` static class is progression costs only — level-up XP, rank-up, capacity). Every
combat number is ours. But the client knows far more about a skill's *shape* than we have
been using.

---

## 1. `DesignSkillRow` — the real columns

The design-pack column names come from private consts on the class, and they are NOT the
field names:

| design column | field | exposed as | notes |
|---|---|---|---|
| `hit` | `_count` | `Hit` | swing count — see §3 |
| `target0` | `_target` | `Target` | packed group+range — see §2 |
| `cd` | `_cdTurn` | `CDTurn` | cooldown in turns |
| `charge` | `_charge` | `Charge` | ultimate gauge requirement |
| `type` | `_type` | `Type` (`SkillType`) | see below |
| `statusID` | `_statusID` | `StatusID` | index into the `status` form (icon/fx only) |
| `skill_script` | `_actName` | `ActName` | cinematic prefab, e.g. `bch001_s02` |
| `group` / `lv` | `_group` / `_lv` | | a skill's upgrade chain; `GetSkillByLV(orgID, lv)` |
| `note1_txt` (+ `_en`/`_jp`/`_sc`) | `_note1` | `Note1` | description **and status glossary** — §5 |
| `note2_txt` | `_note2` | `Note2` | per-level bonus list |
| `action` | `_action` | **not exposed** | §6 |
| `act_id` | `_actID` | **not exposed** | §6 |

`SkillType`: `1 COM_ATTACK, 2 SKILL, 3 SP_SKILL, 4 PASSIVE, 5 SUPPORT, 6 STATUS,
10 GOD_ITEM, 101/102/103 COLLECTION1..3`.

`DesignSkillForm.MAX_EXTRA = 5` — `action`/`act_id` are 5-slot parallel arrays.

---

## 2. Targeting — fully specified by the client

`_target` is packed. From `DesignSkillRow`:

```
GetTargetGroup() => _target / 100
GetTargetRange() => _target % 100
```

`TargetGroup` (static ints, from its `.cctor`): **`Enemy 0, We 1, DeadEnemy 2, DeadWe 3`**
`TargetRange` (same): **`Self 0, One 1, All 2`** — but see the count table, range goes
well past `All`.

### Eligibility — `SkillTargetUtil.IsPossibleTarget(caster, target, group, range)`

```
if (!target.InTheField)  false
if (range == Self)       IsSelf(caster, target)
Enemy      -> different team && !IsDeath
We         -> same team      && !IsDeath
DeadEnemy  -> different team &&  IsDeath
DeadWe     -> same team      &&  IsDeath
otherwise  -> false
```

`GetSkillTargets` returns **every** eligible unit — it drives target highlighting, and
`PanelBattle.haveTarget(caster, group, range)` drives skill-button enable/disable. So the
eligibility code never limits *how many* units are struck.

### Breadth — the label table (this is the part that was missing)

The UI label ("Range 3 enemies") is **text id `23000 + _target`**. That yields a complete,
authoritative breadth spec:

| `_target` | meaning | | `_target` | meaning |
|---|---|---|---|---|
| 1 | 1 enemy | | 100 | Player (self) |
| 2 | All enemies | | 101 | 1 ally |
| 6 | 2 enemies | | 102 | All allies |
| 7 | 3 enemies | | 103 | Player+1 ally |
| 8 | 4 enemies | | 104 | Player+2 allies |
| 9 | 1 random enemy | | 105 | Player+3 allies |
| 10 | 2 random enemies | | 106 | 2 allies |
| 11 | 3 random enemies | | 107 | 3 allies |
| 12 | 4 random enemies | | 108 | 4 allies |
| 13 | Enemy with the highest HP | | 109 | 1 random ally |
| 14 | Enemy with the lowest HP | | 110 | 2 random allies |
| 17 | Enemy with the highest ATK | | 111 | 3 random allies |
| 19 | Enemy with the highest SPD | | 121/122/123 | STR / AGI / TEC ally |
| 201 | 1 enemy is defeated | | 124 | All allies except player |
| 202 | All enemies disabled | | 301 | 1 ally is defeated |
| 402 | All Enemies (alt) | | 302 | All allies are defeated |
| | | | 307 | 1 random ally is defeated |

Coverage over the 14,410 skill rows: **12,622 (87.6%) resolve to a named label.**
The remaining 1,771 are `_target == 0`, which is "no target" — 1,411 STATUS, 184 SUPPORT,
112 COLLECTION1, 60 PASSIVE. A handful of odd values (41, 42, 309, 421-423, 3102, 3502)
are all on PASSIVE/unnamed rows and can be treated as no-target.

**Note the level progression:** Frozen Inferno Thorn I–III are `_target 7` (3 enemies) and
IV–VI are `_target 8` (4 enemies), matching the Lv3 bonus line "All DMG Reduction Target
+1 Cast(s)". Breadth changes with skill level via the `group`/`lv` chain.

---

## 3. Multi-hit — a hard client contract

`BscTagKind.Damage = 5`. A skill's cinematic (`_actName`) fires one Damage tag per swing,
and `AttackBehavior.BscTag` consumes **one `DamageInfo` group per tag** (it takes
`DmgInfo[0]` and removes it). So:

* the number of groups in `data` is dictated by the ANIMATION, not by preference;
* `hit` is the design data's declaration of the same number;
* too few groups → later swings animate with nothing; too many → leftovers ignored.

Corpus distribution of `hit`:

| hit | rows | |
|---|---|---|
| 0 | 5,182 | passives/statuses |
| 1 | 5,918 | |
| 2 | 2,061 | |
| 3 | 1,136 | |
| 4 | 101 | |
| 5 | 12 | |

**3,310 skills are multi-hit.**

### The cinematics: measured, not assumed

Skill cinematics are `BscDataRes` ScriptableObjects named after `_actName`, and they live
in **`data_battle_<hash>.ab`** — not the per-character `art_character_*` bundles, which
hold only models, animation clips and face textures. Structure:

```
BscDataRes  ->  _runtime : BscRuntimeData
                  ->  _tracks[] : BscTrackData
                        ->  _timelines[] : BscTimelineData
```

**Count `BscTagTimelineData` with `_tag == BscTagKind.Damage (5)`.** Not
`BscHitTimelineData` — that is a collision record (hitter/hittee colliders, `finalHit`)
and does not correspond 1:1 with a damage number. Counting hits gave swing counts up to 12
against a `hit` column that maxes at 5; counting tags gives a distribution that maxes at
exactly 5.

`tools/skill_cinematics.py` does this. Over the 680 cinematics, joined to 8,705 skills:

| bucket | n |
|---|---|
| tagless — `DoAllDamage` path | 2,277 |
| tagged, `hit` == swings | 6,235 |
| tagged, disagree | **193** (97.0% agreement) |

**A tagless cinematic is not a mismatch — it selects the other rendering path.**
`AttackBehavior.DoAllDamage` iterates every group and every row inside it, so a cinematic
with no Damage tags consumes the whole list at once and the group count need not match
anything.

### The 193 disagreements, fully accounted for

Every one is explained, and none of them is a real conflict:

| bucket | n | why |
|---|---|---|
| **PASSIVE** skills with `hit = 0` | 136 | pure stat passives ("Base HP Stat +150") — they never attack, so `hit = 0` is right and their `_actName` is vestigial |
| **mob** cinematics (`bmo*`) | 42 | enemy skills, Chinese-only prose |
| dev placeholder rows | 13 | `_note1` is literally `不該看到這級技能` — "you should not see this skill level" — or `無` ("none") |
| `team_skill_s03` | 2 | team-skill path |

Filtering to skills that are actually player-facing attacks — not PASSIVE/STATUS, a `bch*`
cinematic that has tags, and a non-empty English description that is not a dev placeholder:

```
REAL player attack skills with a tagged cinematic and English prose: 3138
   hit == cinematic swings : 3138
   DISAGREE                :    0
```

**Perfect agreement.** So `hit` is trustworthy wherever it matters, and the residue is
entirely passives, untranslated mob rows and dev leftovers.

Practical consequences for an engine:

* ignore `_actName` entirely for `PASSIVE` (4) and `STATUS` (6) — those rows carry one but
  never render an attack;
* for **mob** skills the cinematic still wins, since it is what consumes the groups —
  those 42 rows are the one place `hit` and the animation genuinely differ on units that
  do attack.

So the complete rule for how many groups to send:

* **cinematic has N Damage tags** → send exactly **N** groups; the tag count is the
  authority and `hit` agrees with it 97% of the time
* **cinematic has none** → `DoAllDamage` flattens everything; `hit` is the intended swing
  count and the grouping is free

### The failure mode when you send too FEW groups

`AttackBehavior.BscTag`, `case 5` (Damage), decompiled:

```
DmgInfo = Args[0].DmgInfo
if (DmgInfo.Count >= 1) {          <-- guarded
    OnDamageAndNumber(DmgInfo[0])
    DmgInfo.RemoveAt(0)
}
return
```

The read is **guarded**, so running out of groups does not throw. The swing simply
animates with **no damage number** and the fight carries on. That is the exact symptom of
undersupplying `data`: "the animation plays but no number comes up on some hits". Note
how different this is from the too-many-rows-per-group case below, which throws and stalls
the fight -- one bug is silent-and-cosmetic, the other is silent-and-fatal.

### The other hard constraint on `data`

Within one group, a unit may appear **at most once** — the client reads each group into a
dictionary keyed by the target order. A duplicate throws
`An item with the same key has already been added`, which is swallowed by the generic
event handler: no stack, no animation, and the attacker never yields its turn, so the
fight silently stalls. See [[sevensins-move-gauge-and-swings]].

---

## 4. What the client does NOT provide

* **Damage numbers.** No damage formula exists client-side, at all.
* **Status semantics as data.** The `status` form (110 rows) is
  `iconId / iconIdOnCap / fx / fxOnFloatingText / param` — presentation only.
* **Which of the eligible targets actually get hit** (beyond the breadth label in §2).
* **Enemy AI.**

---

## 5. Status effects ARE specified — in the prose glossary

`note1_en` carries, after the description, a glossary of every status the skill applies,
one per line, in a consistent format:

```
Deals 108% ATK as damage four times and inflicts DEF Break UL on the enemy target.
Grants all allies Gash and Steady. If the caster's HP is lower than 50%, grants the
ally with the highest ATK All DMG Reduction.
* DEF Break UL: DEF-50%, lasting two turns.
* Gash: Final damage dealt+40%, lasting three turns.
* Steady: Gains immunity to Move Gauge Reduction effect for two turns.
* All DMG Reduction: When affected by the status, the damage taken amount turns to 1
  for one turn.
```

Measured over the corpus: **12,896 skills have English `note1`; 6,271 (49%) carry `*`
glossary lines, defining 326 distinct status names.** Most common: Daze 473, Taunt 414,
Confuse 366, Charm 354, Fracture 313, Headwind 259, All DMG Reduction 253, Freeze 242,
Iron Wrist 229, Stun 206, Injured 200, Shield 194, Power Attack Seal 166, DEF Break 146,
Fatigue 138.

This is a far better parsing target than free prose: the `* Name: effect, lasting N
turns.` lines are a mini-format with the magnitude and duration in them, and the same
status is defined identically across every skill that applies it — so definitions can be
cross-validated against each other rather than trusted from one row.

`note2_en` is the per-level bonus list (`Skill Damage +32%`, `Skill CD -1`,
`All DMG Reduction Target +1 Cast(s)`).

**But note2 is redundant for an engine, and should not be parsed.** Each level is already
its own fully self-describing row, reached via `group`/`lv` and `GetSkillByLV(orgID, lv)`:

```
2087111 lv1 coef= 92%  target=7  cd=4  hit=4
2087112 lv2 coef=100%  target=7  cd=4  hit=4
2087113 lv3 coef=108%  target=7  cd=4  hit=4
2087114 lv4 coef=116%  target=8  cd=3  hit=4     <- breadth AND cd change here
2087115 lv5 coef=124%  target=8  cd=3  hit=4
2087116 lv6 coef=132%  target=8  cd=3  hit=4
```

So `note2` only *displays* the delta the next row already encodes. Just as well: of its
17,638 bonus lines across 7,342 skills, only **68.6% match a `+N`/`-N` shape** — the rest
are source typos (`Skilll Damage`, `Skill DMG` and `Skill Damage` all coexist),
untranslated Chinese, and internal dev notes (`追加普攻判定用`, `月明慈光專用`). Reading the
per-level row avoids that entire mess.

---

## 6. `action[]` / `act_id[]` — the effect script, substantially decoded

Two 5-slot parallel int arrays on every skill row. **`DesignSkillRow` exposes no getter
for either** and nothing in the client reads them, so they are the original SERVER's
effect script, shipped inert in the pack. 21 distinct opcodes over 14,410 rows.

The slots are **(verb, operand) pairs**. Only four opcodes take an operand, and when they
do the operand is a **skill row id**:

| op | n | operand | meaning |
|---|---|---|---|
| **112** | 20,719 | STATUS row — 100% | **apply status** (guaranteed) |
| **113** | 688 | STATUS row — 100% | **apply status with a CHANCE** (resistible) |
| **114** | 4,523 | STATUS row, or a category code | **remove status** |
| **117** | 1,357 | type-7 row (1,257), else a real skill | **trigger a follow-up skill** |

Everything else carries no operand and is a trigger/timing condition (§6.3).

### 6.1 The status id space is organised in category blocks

This falls out of op 114's non-id operands, which are ten round numbers. They are
**thousands-block prefixes of the status id space**:

| block | rows | category |
|---|---|---|
| 0 | 474 | misc / named one-offs |
| 1000 | 11 | heal-over-time (Heal, Regen, Rest) |
| 2000 | 195 | buffs (Might, Iron Wrist, Harden, Gash) |
| 3000 | 72 | **stackable** buffs — `Spirit(5)`, `Rigidity(5)`, `Critical(5)` |
| 4000 | 114 | shields (Shield, Power Shield, Life Shield) |
| 5000 | 57 | damage-over-time (Wound, Poison, Shock, Bleed) |
| 6000 | 83 | debuffs (Weaken, Fracture, DEF Break, Slow) |
| 7000 | 38 | **stackable** debuffs — `Withering(5)`, `Fatigue(5)` |
| 8000 | 273 | passive stat grants (Body Strike I–IV) |
| 9000 | 18 | flat stat ups |

The trailing `(5)` in the 3000/7000 names is the **stack cap**.

The prose confirms each block independently:

* `114 + 4000` → "removes the caster's **Shield**"
* `114 + 5000` → "removes **DoT** from all allies"
* `114 + 2000` → "removes the caster's removable **buffs**"
* `114 + 3000` → "removes the caster's **stackable buffs**"
* `114 + 6000` → "**unstackable debuffs**"

### 6.2 A worked row

Skill **207** carries `ops = [(114, 2005), (114, 3000), (113, 619)]` and reads:

> "When affected by this status, **removes the caster's stackable buffs** with a certain
> chance **and Gash** as well. …"

| op | operand | resolves to | prose fragment |
|---|---|---|---|
| 114 | 2005 | `Gash` (buff block) | "and Gash as well" |
| 114 | 3000 | *stackable-buff block* | "removes the caster's stackable buffs" |
| 113 | 619 | `Charm` | "with a certain chance" |

Skill **100301/100302** are the cleanest proof of the 112/114 pair: identical operand
`3002` (`Rigidity`), prose `反擊後附加BUFF` ("after counter, **attach** buff") vs
`反擊後消除BUFF` ("after counter, **remove** buff").

### 6.3 The no-operand opcodes are trigger conditions

Isolated by looking at skills with exactly ONE non-zero action slot, which gives a clean
1:1 with the prose. **Suggestive, not proven** — unlike the four above:

| op | n | prose pattern of single-op skills |
|---|---|---|
| 116 | 2,775 | "**When affected by this status**, …" |
| 1 | 1,195 | "**While taking damage** …" / "**While dealing attack** …" |
| 5 | 1,039 | "**When taking damage**, recovers …" |
| 115 | 1,012 | "at the **beginning of each turn**" / "if total turns reaches N" |
| 118 | 146 | "**After action**, there is a N% chance …" |
| 122 | 50 | "**造成傷害後** (after dealing damage), if the target has …" |

So the script reads as *trigger* + *(verb, operand)* effects, which is exactly the shape a
status/passive system needs.

### 6.4 SkillType **7** — undocumented sub-skills

The client's `SkillType` enum defines 1,2,3,4,5,6,10,101,102,103 — there is no 7. Yet 557
rows carry type 7 and op 117 points at them 1,257 times. They are **follow-up attacks**
with their own `_target`, `hit`, `_actName` cinematic and their own opcodes: "Stars
Below", "Xmas Shooting Star", `反擊後附加BUFF`. These are the "追加普攻" (additional normal
attack) rows whose leftovers show up in `note2`.

So op 117 = *pursue / counter / follow-up*, and the sub-skill is itself a full skill spec.

### 6.5 What is still open

* the precise semantics of the rarer no-operand opcodes (111, 119, 120, 121, 2, 3, 6, 8,
  11, 12) — low volume, and §6.3 is inference from prose rather than proof;
* whether slot ORDER encodes sequencing (before/after action) or is just a list;
* where the *chance* for op 113 comes from — presumably Effect Hit vs Effect Res, which
  the client's own help text mentions but never quantifies.

## 7. Where this leaves a rewrite

Recoverable from the client, no guessing required:

* targeting group + **breadth** (§2) — replaces AoE-from-prose entirely
* swing count (§3), independently checkable against the cinematic prefabs
* cooldown, charge, skill type, upgrade chain
* status **names, magnitudes and durations** (§5), cross-validatable across skills

Must be invented by us:

* the damage formula itself
* how each status verb actually applies (the glossary says *what*, not *how it composes*)
* AI target selection among eligible units
* anything the `action[]` script encodes that prose omits

---

Related memory: [[sevensins-battle]], [[sevensins-move-gauge-and-swings]],
[[decompile-dont-guess]].

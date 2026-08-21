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

**3,310 skills are multi-hit.** `_actName` prefabs are in the shipped bundles, so the tag
count is independently checkable per skill — a validator can compare tags vs `hit` and
give ground truth for the whole corpus.

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
`All DMG Reduction Target +1 Cast(s)`) — i.e. how the numbers, cooldown and **breadth**
change up the `group`/`lv` chain.

---

## 6. `action[]` / `act_id[]` — the undecoded effect script

Two 5-slot parallel int arrays on every skill row. **`DesignSkillRow` has no getter for
either**, and nothing in the client reads them — so they are almost certainly the original
SERVER's effect script, shipped in the pack and inert client-side.

Across 14,410 skills there are only **21 distinct opcodes**, in two clusters:

```
1xx: 112 (20719), 114 (4523), 116 (2775), 117 (1357), 115 (1012), 113 (688),
     111 (269), 118 (146), 122 (50), 119 (50), 120 (30)
 1x:   1 (1195),   5 (1039),   7 (320),    3 (248),    6 (125),  11 (103),
       8 (17),    12 (9),      2 (7)
```

Some opcodes always pair with `act_id == 0` (116, 115, 111, 118, 119, 120, 122, 5, 7, 3,
6, 11); others carry ids spanning wide ranges (112: 101..122000902, 114: 133..100001655,
117: 11002..153006146) that look like references to skill/fx/status ids.

**Status: undecoded, and there is no client-side ground truth for it** — the client never
interprets these. The only validation signals are the `note1` prose and original-game
footage. A first pass at correlating opcodes with prose keywords gave weak signal, because
prose describes the whole skill rather than one slot.

Worth returning to: 21 opcodes over 14,410 rows with per-row prose is a strong alignment
problem, and success would give an exact effect system for the entire corpus.

---

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

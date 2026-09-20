# The battle client contract

**What the CLIENT knows, and what the server must invent.** Everything here is derived
from `libil2cpp.so` (2.2.7) and the shipped design pack — *not* from our server code.
This exists because parts of it have been worked out before and lost.

**See also `docs/BATTLE_RESULT_PANEL.md`** for the end-of-battle result panel: the
`IsBattleEndReady` handshake, why a lost guild fight leaves the tap dead, and what the
Il2CppDumper C# export can and cannot be trusted for.

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

### Mode 4 (move gauge) must NOT be sent as a `DamageInfo` row

`OnDamage` has a `case 4` that calls `ShowScvBar`, so a mode-4 row looks legitimate. It
is not: **including one in `data` hangs the fight.** The animation plays, the payload is
accepted, and the attacker never yields its turn.

Found on device, and the correlation is exact — every skill carrying a `modify_gauge`
(op 116) effect stalled, while the same characters' other skills played normally:

| skill | move-gauge component | result |
|---|---|---|
| Lucifer — Eclipse Slash | yes | stalls |
| Metatron — Poison Injection | yes | stalls |
| Belial — Sign of Ill Fortune | yes | stalls |
| their other three skills each | no | fine |

The gauge has its own channel and does not belong in the attack payload at all:
**`BattleCmd.sync[order][2]` is `LightBattleChar.Scv`**, which is what the client reads to
draw the bar *and* what `GetNextAction` re-runs the ATB from to place the "Next" badge
(§3.5). Apply the change to the unit and let the next `sync` carry it.

(`ShowScvBar` itself is null-guarded and cannot throw, so the hang is further along —
plausibly the cinematic's tag/group pairing, since a mode-4 row consumes a slot in a
group that the Damage tags are counting through. Not chased further: the row should not
be there in the first place.)

### A skill with no rows still needs a combo entry

Once the gauge left the payload, skills whose only effect *was* the gauge produced no
rows at all. An empty combo is not valid — the client needs an entry to drive the
animation and yield the turn — so such a skill ships a single zero-damage row.

### The other hard constraint on `data`

Within one group, a unit may appear **at most once** — the client reads each group into a
dictionary keyed by the target order. A duplicate throws
`An item with the same key has already been added`, which is swallowed by the generic
event handler: no stack, no animation, and the attacker never yields its turn, so the
fight silently stalls. See [[sevensins-move-gauge-and-swings]].

---

## 3.5 Turn order — the server picks the actor, the client predicts the NEXT one itself

Worth stating precisely, because the two halves are decided in different places and only
one of them is ours.

**Who acts now** is purely the server's. `PlayerBattle.UpdateTimeLine` (0x168C024) is a
bare assignment — `BattleData.ActionOrderList = battleCmd.tempActionOrderList` (wire key
`line`), no sorting, no filtering — and `PlayerBattle.GetFirst` reads `line[0]`. So
whatever we put in `line[0]` acts, full stop.

**The "Next" badge is NOT `line[1]`.** `AttackState.OnEnter` (0x17FAB3C) calls
`SetNextSign(PlayerBattle.GetNextUnit())`, and `GetNextUnit` delegates to
`BattleUnitManager.GetNextAction(ActionOrderList)` (0x197E100), which **re-runs the ATB
simulation client-side**:

```
L = [ActionLineData(order=o, SPD=cache[o].SPD, scv=cache[o].Scv) for o in line]
L[0].scv -= 100                 # the acting unit just spent a full gauge
head = L.pop(0)
i = first index where head.scv > cache[L[i].order].Scv, else len(L)
L.insert(i, head)               # the actor goes back into the running
return L[0].order if L[0].scv >= 100 else argmin over L of (100 - scv) / SPD
```

`cache` is `PlayerBattle`'s `Dictionary<string, LightBattleChar>`, refreshed every turn
from `BattleCmd.sync` (`BattleUnitManager.SyncData` reads `[MaxHP, HP, Scv, SPD]`).
`LightBattleChar.Scv` is at 0x38 and `SPD` at 0x3C, matching the `+14`/`+15` dword reads
in the decompilation exactly.

Three consequences:

1. **`line` supplies membership, not lookahead.** The client only takes the *set* of
   orders out of it (plus which one is at the head). Deduplicating our projection down to
   one entry per unit is therefore harmless — the client never reads `line[1]`.
2. **`sync.scv` is load-bearing.** The badge is computed from the gauge values we send,
   not from anything about `line`. Stale or zeroed `Scv` silently mispredicts.
3. **The client fully expects a unit to act twice in a row.** It puts the actor back into
   the pool at `scv - 100` and lets it win again on fill time. A unit at 4× the field's
   SPD does take ~4 turns per opponent turn, and the badge tracks that correctly.

### The one real disagreement: tie-breaks

Simulating `GetNextAction` against our own `battle_cmd_json` output, two units at SPD 2000
and 500 agree on 5 turns out of 6. The exception is an exact tie in fill time:

```
turn 3: acting=101  sync scv {101: 100, 102: 75}
        client badge = 102     we act 101      <-- disagree
```

Both fill in 0.05s — `100/2000` for the actor, `(100-75)/500` for the other. The client's
scan uses a strict `<` against the running minimum, so it keeps the **earliest entry in
`L`**, and since the actor was re-inserted at the tail, that means it prefers *the other
unit*. Our `_roll_turn_order` tie-breaks on `(-spd, team, index)` and so prefers *the
faster* one. The badge points at the wrong portrait for that turn.

To match, the engine's tie-break must be positional rather than stat-based: among units
with equal fill time, prefer the one earlier in the current `line`, with the unit that
just acted placed last. This is a phase-4 serialiser invariant, not a gameplay change —
whoever we choose still acts; only the prediction the player saw a moment earlier was
wrong.

---

## 3.6 The attribute column — `char._job`, and there are FIVE of them

Worth writing down because it is easy to get wrong twice: `_job` looks like a class
field, and its distribution (`2: 1155, 4: 1145, 3: 1092, 0: 237, 1: 9, 5: 5`) looks like
three real categories plus noise. It is the **attribute**, and the 14 outliers are real.

`UICharacterRoom.UpdateCharInfo` (0x1626C0C) draws the badge beside the character name:

```
Job = CharData.get_Job()                       // -> DesignCharRow._job
AtlasUtil.LoadAtlas(Job + 51801, _spJob, ...)
_spJob.gameObject.SetActive(Job != 0)
```

So the sprite rows at `Job + 51801` name them outright:

| `_job` | sprite | attribute | rows |
|---|---|---|---|
| 0 | `icon_charclass_void` | none — badge hidden | 237 |
| 1 | `icon_charclass_hex` | **ABYSS** | 9 (Lucifer, Satan, Mammon, Belial, Metatron) |
| 2 | `icon_charclass_strength` | **STR** | 1,155 |
| 3 | `icon_charclass_speed` | **AGI** | 1,092 |
| 4 | `icon_charclass_skill` | **TEC** | 1,145 |
| 5 | `icon_charclass_psychic` | **SOLAR** | 5 (Leviathan, Panagia, Michael, Sariel, Gabriel) |

`hex`/`psychic` are the internal names; ABYSS/SOLAR are what the game shows. Only ten
characters carry them, all 5-star, which is exactly why they read as noise beside the
~1,100-row STR/AGI/TEC blocks.

The Chinese mission text corroborates the middle three — `CommonUtil.GetJobUseText`
formats text `job + 12099`, giving 使力量型 (strength), 使速度型 (speed), 使技巧型 (skill) for
jobs 2/3/4, and "any character" for 5.

### The triangle is drawn in colour, so read the icons

The in-game diagram uses no words: **red beats yellow beats blue beats red**. Cropping
the icons out of `common/main/atlas_main_lobby` at the rects the NGUI `UIAtlas`
MonoBehaviour gives, and taking each icon's most-saturated pixel:

| attribute | sprite | vivid RGB | reads as |
|---|---|---|---|
| STR | `strength` | (254, 44, 189) | red / magenta |
| TEC | `skill` | (218, 247, 24) | yellow |
| AGI | `speed` | (115, 77, 255) | blue / violet |

so the cycle is **STR → TEC → AGI → STR**.

(Orientation check, because a flipped atlas y would invert the whole mapping silently:
the as-is crops have transparent corners — a padded 64×64 icon — while the y-flipped
crops land mid-atlas on opaque pixels.)

### What the pack does NOT say

The **magnitudes**. Text 17051 states only that the triangle exists ("Allies with
advantageous attributes deal increased damage, while damage dealt by allies with other
attributes is reduced"). The numbers in `engine/formula.py` — crit ±15%, the
strong-attack upgrade that turns a failed crit into +30% on advantage, EFF ACC ±15%, and
the 30%/-30% "miss" on disadvantage — are **community-documented, not extracted**, and
are flagged as such in that file. Likewise whether SOLAR and ABYSS interact at all.

Note "EFF ACC" is not an invention either: `BattleAttributeData` already carries `ehit`
and `eanti` alongside `cri`, `cdi`, `cdr`, `ddi`, `ddr` and `prc` — real fields the
client renders, which we have been sending as zeros.

---

## 3.7 Systems referenced by prose that never shipped

Worth recording so they are not chased twice.

**Fetish (珍寶殿, "Fetish Haven")** is referenced by skill prose -- Gabriel's Campus
Correction and Metatron's Field Hospital both say their second-tier classes "can be
unlocked at the Fetish and Consonance interface" -- and by quest text ("Complete Activate
Fetish Haven: Awakening of Red LV1"). It does not exist in the client:

* none of the 25 `Player*` subsystems is Fetish or Treasure;
* there is no panel class and no prefab bundle for it;
* `panel_localize`'s only three matches are bulletin-board quest ARTWORK
  (`BulletinQuest-PathofFetish`), not a system UI;
* in game, its section in the Relics panel reads **"Coming Soon"** (text 111223
  `敬請熱切關注!!`, or 36 "This feature will be available soon").

So it was planned and never completed before end of life. Those skill tiers were
unreachable on the live server too, which means implementing them would ADD behaviour the
real game never had -- the correct treatment is to leave them out, not to model them.

---

## 3.8 Statuses reach the client on TWO channels, with different arg layouts

Decompiled 2026-08-21, after the whole opening buff bar turned out to be invisible.

**Per-action** — `DamageInfo.status`, `List<List<int>>`, rows of `[order, skill_id,
round]`. Each row names its OWN unit, so any row can carry a change for any unit; they
all hang off the lead row. `AttackBehavior.updateStatus` walks them in order and calls
`BattleUnit.UpdateStatus`, which reads `args[1]` as the id and `args[2]` as the round.

**Battle open** — `BattleDatas.status` (field `StatusDatas`, wire key `status`),
`{order: {skill_id: args}}`. `BattleDatas.RebuildAllStatus` walks it from
`BattleDataInitializer.<InitUI>d__22.MoveNext`. This is the ONLY way a status that no
attack applied can be drawn, which is every battle-start passive aura. Sending `{}` (as
we did) meant Field Shield, both Field Angels, both Commendations, the boss's opening
seals and Lucifer's The Divine were all invisible from turn zero.

### `round` is a three-way discriminator, not a duration

`BattleUnit.UpdateStatusRound` decrements only when `round >= 1` and removes at exactly
0. So:

| `round` | meaning |
|---|---|
| `>= 1` | lasts that many turns, counted down client-side |
| `0` | **REMOVE** — `UpdateStatus` routes to `removeStatusDataByID` |
| `< 0` | permanent: never decremented, never expires |

`-1` is therefore the sentinel for "lasts the entire battle", not a large number. And a
removal has no other channel: dropping `applied=False` events left the client drawing
statuses the server had already stripped.

### The two arg layouts DIFFER — this is the trap

`StatusST` has two constructors and they do not agree:

```
.ctor(List<int> curArgs)             [1]=skill [2]=round, and [3]=lv [4]=value
                                     [5]=actOn ONLY when Count >= 4
.ctor(int skillID, List<int> args)   [0]=round [1]=value [2]=actOn [5]=lv
```

The per-action channel uses the first, so a 3-element row is safe. The battle-open
channel uses the second, which reads index 5 **unconditionally** — six entries is the
floor there or it throws `ArgumentOutOfRangeException` inside battle load.

### Every id is fed to `GetRow`, which THROWS on a miss

Both constructors do `DesignSkillForm.GetRow(skillID)` to read the row's `_statusID`.
That call throws rather than returning null, and on the battle-open channel it throws
inside the load coroutine — the coroutine dies and the loading screen hangs forever,
with nothing wrong server-side:

```
DesignException: (0x0012) DesignSkillForm row ID -175492 not found
  at Game.Player.Battle.StatusST..ctor (Int32 _skillID, List`1 serverArgs)
  at Game.Player.Char.BattleDatas.RebuildAllStatus
  at Game.Battle.Common.BattleDataInitializer+<InitUI>d__22.MoveNext
```

So an id must be *resolvable*, not merely non-null. Ours was a synthesised negative id
minted from a status name for a rule with no design row — see the engine plan.

### Duplicate rows share a name, so removal must be by ID

397 and 399 are both `The Divine`; 398 and 400 are both `The Fallen`. A status's nested
script routinely applies the duplicate and removes it again (`apply 400, remove 400`, a
no-op as written). Matching that removal by NAME deletes the real marker too.

Both stance markers also carry `_statusID: 1` and `_spriteID: 1000131` — Divine and
Fallen render with the *same icon*. That is retail data, not a bug to chase.

---

## 3.9 The preparation screen art is YOUR lead unit, not the boss

`PanelBattlePreparation.UpdateLive2d` (0x16ABAFC) never reads the stage or the mob
group. It takes `PlayerChar.Formations[_nowTeamListIndex]`, member **[0]**, and renders
that character through `FittingRoom.ShowChar`. Gabriel showing on Raphael's daily raid
is correct — she was in slot 0.

Worth writing down because the near-miss is very convincing: the seven daily challenges
have one virtue dmap each (`21001` Faith: Michael … `21006` Temperance: Raphael,
`21007` Chasity: Gabriel) and the seven bosses are adjacent too (`1000206` Raphael,
`1000207` Gabriel). "Art is one row off" fits perfectly and is wrong.

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

### 5.1 Exactly where the structured data stops

Prose is a last resort, so it is worth being precise about what forces us to it. Dumping
every column that is ever non-empty gives **26 columns on an attack row and 12 on a
status row**, and the answer is unambiguous:

| the engine needs | source | column? |
|---|---|---|
| which statuses a skill applies | `action` / `act_id` | ✅ |
| category + stackability | the status id block (§6.1) | ✅ |
| stack cap | `(N)` suffix on the name | ✅ |
| targeting breadth | `target0` | ✅ |
| swing count | `hit` | ✅ |
| cooldown / ultimate charge | `cd` / `charge` | ✅ |
| a status's own upgrade chain | `group` / `lv` on the type-6 row | ✅ |
| **statuses applied BY a status** | `action` on 510 type-6 rows | ✅ |
| icon / hidden-from-UI | `statusID`, `spriteID`, `hide` | ✅ |
| **damage coefficient** | — | ❌ prose only |
| **status magnitude** | — | ❌ prose only |
| **status duration** | — | ❌ prose only |
| **dispellability** | — | ❌ prose only |

A type-6 status row has **no magnitude or duration field of any kind**. That is not an
omission — those values do not belong to the status. They belong to the **(skill,
status) pair**, and Frozen Inferno Thorn proves it:

```
2087111 lv1   act_id [6050, 2005, 4283, 4101, 0]   * Gash: Final damage dealt+35%, lasting two turns.
2087114 lv4   act_id [6050, 2005, 4283, 4101, 0]   * Gash: Final damage dealt+40%, lasting three turns.
```

Identical opcode script, different numbers. Corpus-wide, **740 of 1,592 (status,
skill-group) pairs change their glossary body across levels** — so a single canonical
reading per status would be wrong 46% of the time. The numbers must be attached per
applying skill row.

The same row pair also kills the damage coefficient: lv1→lv4 moves `cd` 4→3 and
`target0` 7→8, but **92% → 116% appears nowhere except `note1_en`**.

### The three-tier provenance this forces

Only 9,327 of the 21,407 apply sites carry a `* Name:` line on the applying skill — the
glossary lists what the *description* names, and passives, sub-skills and untranslated
rows name nothing. So `compile_skills.py` tags every number with where it came from:

| source | sites | meaning |
|---|---|---|
| `skill` | 9,327 (44%) | the applying skill's own glossary line — authoritative |
| `corpus_default` | 3,150 (15%) | modal value across the pack, **≥80% agreement only** |
| `null` | 8,930 (42%) | nothing stated; the engine applies a policy, knowingly |

The threshold is what makes the fallback honest rather than a fabrication. Daze agrees
93% across 473 bodies, Charm 98%, Stun 95%, All DMG Reduction 98% — a default is a
reading. Iron Wrist agrees on duration only 74% and on **magnitude 31%** — there a
default would be an invented number, so it is left `null`.

**An unknown duration is `null`, never `0`.** At runtime a zero is indistinguishable from
"expires immediately", which is precisely the silent-failure class this whole effort
exists to eliminate. `test_skill_specs.py` asserts it.

### Dispellability

500-odd status rows end their own note with a fixed parenthetical — `(unremovable)`,
`(Cannot be cleansed by buff removals)`, `(Unremovable)`. This is a **marker, not free
text**, which is what makes it safe to read. It matters because op 114 removes by
*category block* ("removes the caster's removable buffs"), so an engine that ignores the
flag would strip 509 statuses it must not touch.

Likewise `(Crowd Control Debuff)` appears on 20 rows as an explicit category stated by
the data, and overrides any wording-based classification.

---

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
| **112** | 20,719 | STATUS row — 100% | **apply status** (see 6.1.1 — *not* "guaranteed") |
| **113** | 688 | STATUS row — 100% | **apply status**, marked resistible |
| **114** | 4,523 | STATUS row, or a category code | **remove status** |
| **117** | 1,357 | type-7 row (1,257), else a real skill | **trigger a follow-up skill** |

Everything else carries no operand and is a trigger/timing condition (§6.3).

### 6.1.1 112 is not "guaranteed", and the client cannot settle it

The reading above — 112 certain, 113 chancy — was inferred from the operand table alone,
and the prose disagrees with it. Measured over every apply site, restricted to the
fragment that both names the status and grants it:

| | states odds in its own fragment | states none |
|---|---|---|
| op 112 | **772** | 19,937 |
| op 113 | 409 | 279 |

`以40%的機率附加挑釁` on Wise Prediction III is an op-**112** row. So a probability is a
property of the *prose*, not of the opcode, and the compiler emits `chance_pct` for both
(`status_chance` in `tools/compile_skills.py`). 113 with nothing stated keeps a neutral
stand-in, `engine/core.UNSTATED_CHANCE`, which is a modelling choice and named as one.

**The client settles nothing here, because it never reads the opcode script.**
`DesignSkillRow` exposes no `Action`/`ActID` property at all, and the one method that
touches the array — `DesignSkillRow$$AddSkillScripts` at **0x1aacb00** — walks it looking
for opcode **4**, reads that slot's operand as a skill id, and registers that row's
`_actName` in the cinematic map. Nothing else — and **op 4 occurs zero times in the EN
2.2.7 pack**, so even that branch is dead here. `_action` was the retail *server's*
program; what the client kept of it is a preload list it never uses. (The branch does
say what op 4 would have meant — "this skill plays another row's cinematic" — but that
is read off the binary, not off any row we hold.)

What the 112/113 split does mean is still open. It is not the presence of a chance.

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
| 115 | 1,012 | **NOT a trigger — an effect: skill-CD change.** "Gain a reduction in Skill CD at the beginning of each turn" / "increases the Skill CD of the target". Confirmed by skill 235, `[(114,2000),(114,3000),(115,0)]` = "removes the caster's removable buffs when the turn starts and increase the [CD]" |
| 118 | 146 | "**After action**, there is a N% chance …" |
| 122 | 50 | "**造成傷害後** (after dealing damage), if the target has …" |

So the script reads as *trigger* + *(verb, operand)* effects, which is exactly the shape a
status/passive system needs — **but the no-operand set is a mix of triggers AND
operandless effects**, as op 115 shows. Do not assume "no operand" means "condition".

### 6.3.0 Op 6 -- an amount sized off the caster's own HP pool  *(decoded 2026-08-29)*

By prose clustering, since there is no client handler to read (§6.1.1). Of 125 rows
carrying op 6, **95 say 當前/當下/目前體力** -- "current HP", 56.5% of its rows against a
0.8% corpus baseline, a 74× lift -- and 25 more (one cast's Matcha Sundae) say 最大體力.
The verb is prose, exactly as with op 1: bonus damage (額外對目標造成自身8%當前體力的傷害)
or a heal (恢復自己的體力(相當於8%的當前體力), 以瑪門當前體力的30%恢復我方3名體力最低的血量).
The 4 mob `Slash` rows and one nameless row state no number and stay reported skips.

Compiled as an `attack_rider` with `basis: caster_current_hp | caster_max_hp` and
`opcode: 6` (`zh_hp_rider`), executed by the same `_rider` path with the basis read off
the caster *after* the skill's own swings. 114 of 125 decoded, 87 of them gated by the
若 fragment in front (敵方存活人數大於N is a shape the parser cannot yet read, and those
25 carry `requires.unparsed` so the engine rolls the conditional policy instead of
firing). One reading is a choice and is named as such in `_rider`: the bonus damage is
dealt **flat** -- no crit, no advantage roll -- because it is not a coefficient on any
stat the strike formula knows. Whether retail mitigated it by DEF is a footage question.

### 6.3.1 Joining opcodes to the prose glossary needs name normalisation

Of 9,346 statuses named in attack-skill glossaries, 6,885 (73.7%) are also named by a
112/113 opcode on the same row. Most of the 2,461 residue is **not** missing data — it is
the stack-cap suffix: the glossary says `Agony`, `Fatigue`, `Listless`, `Spirit`, while
the status ROW is `Agony(5)`, `Fatigue(5)`, `Listless(5)`, `Spirit(5)`. Normalise by
stripping a trailing `(N)` and case-folding before joining.

### 6.3.2 The operandless opcodes are NOT all triggers — three are effects

§6.3 inferred from prose that the no-operand opcodes were trigger conditions. Op 115
already contradicted that, and a corpus-wide enrichment test now settles it: for each
opcode, how much more often its rows mention a concept than the corpus baseline does.

| op | rows | concept | in these rows | baseline | lift | alone on |
|---|---|---|---|---|---|---|
| **116** | 2,775 | move gauge / 行動值 | 92% | 6.4% | **4.2×** | 414 rows, 375 say "Move Gauge" |
| **111** | 269 | revive / 復活 | 98% | 4% | **23×** | 14 rows, bare 復活 |
| **5** | 1,039 | heal / 補血 | 84% | 20% | **4.2×** | 47 rows |

The "alone on" column is what makes this decisive rather than suggestive: on hundreds of
rows these opcodes are the **only** non-zero slot, so there is nothing else in the row the
prose could be describing. `[116, 0, 0, 0, 0]` with "increase all enemies' Move Gauge with
a fixed chance after action" cannot be a trigger — if it were, the skill would do nothing
at all.

So: **116 = modify move gauge, 111 = revive, 5 = heal.** Their operand is always 0, so
the *magnitude* still comes from prose and is null when unstated — but the engine now
performs the right kind of effect instead of filing the whole skill under `unknown`.

Decoding these three moved whole-corpus coverage from **53% to 74%**, and playable cast
skills from 49% to **79%** of rows with every opcode recognised.

### 6.3.3 Op 1 — the attack rider, and why it is not one effect

1,195 sites, the largest gap after the three above. The naive read is "extra damage":
62% of its rows use extra-damage phrasing, a 6.8x lift over the 9.2% corpus baseline, and
its rows carry **two or more distinct damage percentages 55% of the time** against a 12%
baseline. That read is wrong, or at least half wrong.

**What it actually is.** Op 1 rides on an attack:

| | op 1 | op 5 | baseline |
|---|---|---|---|
| rows that are com_attack/skill/sp_skill | **87%** | 46% are PASSIVE | — |
| the skill itself deals damage | **93%** | 56% | 58% |

That is what separates the two heal-ish opcodes: op 5 is a standalone or turn-scheduled
heal that lives on passives ("Recovers N HP after action", "Before the turn starts…"),
while op 1 is always attached to a blow landing.

**But its kind is not in the opcode.** On the 137 rows where op 1 is the ONLY non-zero
slot — so nothing else in the row can be what the prose describes — the prose partitions
with **no overlap at all**:

| | rows |
|---|---|
| heal — "Deals N% ATK as damage **and recovers the caster's HP**" | 102 |
| bonus damage — "if the target is stunned, **additionally deal 100% ATK** once" | 30 |
| neither | 5 |
| **both** | **0** |

Across all 1,136 rows with prose the split holds at **422 heal / 422 bonus damage**. No
single effect explains that, and the operand is always 0, so nothing in the row encodes
which one it is.

So: **op 1 = an attack rider — an additional effect that fires when this skill's attack
connects — whose KIND is stated only in prose**, exactly as magnitudes are. 74% classify
unambiguously and are emitted as `attack_rider` with `kind: heal | bonus_damage` and the
rider's own percentage (79% of those). The ambiguous 26% stay in `unknown`: emitting a
rider with a null kind would be an effect the engine silently skips, which is the failure
class this whole pipeline exists to prevent.

Decoding it moved the corpus from 74% to **80%** fully decoded, and cast skills from 79%
to **87%** of rows with every opcode recognised.

The genuinely unresolved remainder, with what is known of each shape:

| op | rows | evidence |
|---|---|---|
| 1 (residual) | 326 | op-1 rows whose rider kind is ambiguous or unstated — see §6.3.3 |
| 7 | 320 | mixed; several rows pair with Deathblow |
| 3 | 248 | mostly "will trigger «named» effect" — a cross-skill hook |
| 118 | 146 | chance-triggered follow-up — "10% fixed chance to trigger Dead Silence 1 time" |
| 6 | 125 | conditional extra attack — "If there are still at least 4 enemies … additionally" |

118 and 6 are understood in shape but **not executable**: they name the triggered skill
only in prose, and with no operand the row does not encode which skill it is.

### 6.4 SkillType **7** — undocumented sub-skills

The client's `SkillType` enum defines 1,2,3,4,5,6,10,101,102,103 — there is no 7. Yet 557
rows carry type 7 and op 117 points at them 1,257 times. They are **follow-up attacks**
with their own `_target`, `hit`, `_actName` cinematic and their own opcodes: "Stars
Below", "Xmas Shooting Star", `反擊後附加BUFF`. These are the "追加普攻" (additional normal
attack) rows whose leftovers show up in `note2`.

So op 117 = *pursue / counter / follow-up*, and the sub-skill is itself a full skill spec.

### 6.4.1 A repeated (op, operand) slot is AMBIGUOUS

The same `(112, status)` pair often appears in two slots. It means one of two different
things, and the opcodes do not say which:

* **stacks** — Gabriel's Pure Flash is `[117, 115, 112, 112]` / `[2016181, 0, 3001, 3001]`
  and reads "grants the caster **2 stacks** of Spirit". Two slots, one trigger, two stacks.
* **the same effect under two different TRIGGERS** — `Krampus of The Undead World`
  (2002131) is `[112, 112, 112, 117, 112]` / `[3015, 3015, 2524, 2002151, 728]` and reads
  "**Before dealing any attack or while taking damage**, grants the caster **1 stack** of
  Wrath". Two slots, two triggers, one stack each. `Watergun Reload` (234) is the same
  shape: "grants the caster 1 stack of Reload **before … turn or taking damage**".

Measured over skills where a `(112/113, status)` pair repeats and the prose names a stack
count: **192 agree with the repeat count, 181 disagree.** So repeats must NOT be folded
into a stack count automatically.

Nor does slot index appear to encode the trigger: Gabriel has its follow-up in slot 0
while Belial's four statuses occupy slots 0-3, so the positions are not phase-aligned.
The trigger genuinely looks absent from the script — the original server likely carried
per-skill logic keyed by skill id, with `action[]` as the effect summary rather than a
complete program.

**Practical consequence:** compile repeats verbatim (one effect per slot, with its slot
index) and let the prose disambiguate stacks vs triggers per skill. Do not guess.

### 6.4.2 A status's own script — when it fires, and what a repeat means

The table in §5.1 records that 510 type-6 rows carry an `action` script (428 of them
survive into `statuses.json` as `nested`: 719 applies, 199 removes, 87 category
cleanses). What it did not record is the SEMANTICS, which nothing in the pack states —
the original server interpreted these, and there is no server binary to decompile. What
follows is inferred from prose, and is flagged as inference deliberately.

A status is not only a modifier; it can be a **trigger marker** whose script runs while
it is held. The prose says so directly: *"When affected by this status, Angel of Faith,
Michael will trigger Destiny UL effects."*

| question | reading | evidence |
|---|---|---|
| WHEN | every turn while held, not once on apply | Lucifer's passive spells the timing out — *"if affected by The Fallen, grants Keen (CRT+35%) for two turns at EACH START OF A TURN"* — and The Fallen's row is exactly `apply 2007 Keen, apply 2009 Teardown` |
| a REPEATED identical apply | a stack count | `Destiny UL` lists 3804 five times; `Haughty Malefics` lists 3012 twice |
| ONE-SHOT vs recurring | a row that removes ITSELF is consumed by firing | `Never Surrender` = `apply All DMG Reduction, remove Never Surrender` |

Two consequences that only show up in play:

* **Scope is every unit, not the acting one.** Durations tick once per global turn but a
  unit acts once every N turns, so refreshing a marker's grants only on the holder's own
  turn makes them flicker — Keen and Teardown were on/off every other turn with two
  units on the field, and would be up 2 turns in 6 with a full party.
* **Recursion is one level.** A status applied by a script runs its own script at the
  NEXT turn start, once the unit actually holds it. Same rule as everything else, and it
  makes an unbounded chain impossible without a depth counter to tune.

Still unproven: whether "each start of a turn" means each *global* turn (what we
implement) or each of the holder's own turns. The flicker argues for global; the prose
does not settle it.

### 6.5 What is still open

* the precise semantics of the rarer no-operand opcodes (111, 119, 120, 121, 2, 3, 6, 8,
  11, 12) — low volume, and §6.3 is inference from prose rather than proof;
* whether slot ORDER encodes sequencing (before/after action) or is just a list;
* what the 112/113 split encodes. **Not** "guaranteed vs chancy" — the prose states
  odds on 772 op-112 sites (§6.1.1). Effect Hit vs Effect RES is how the engine applies
  whatever number it has; the client's help text mentions both and quantifies neither.

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

## Addendum, 2026-08-26 -- read from the binary in IDA, and what each fact cost us

Every item below is a decompiled client function; addresses are from the EN 2.2.7
`libil2cpp.so`. Each one changed server behaviour the same day.

### The client counts status rounds down for ONE unit: the actor

`TurnEndState.OnEnter` (0x196E268) calls `BattleUnit.UpdateStatusRound` (0x196E4A8) on
`PlayerBattle.GetFirst()` (0x168B270), which is `BattleDatas.ActionOrderList[0]` -- the
unit that just acted. `HandleAttack` does NOT call `UpdateTimeLine`, so at TurnEnd the
head of the list is still the actor. Nothing else on the client ever decrements a
round. Two consequences the server must honour:

- A unit whose turn the SERVER skips (immobilised, or gauge-blocked and aged on the
  battle's clock) never has a TurnEnd on the client, so its expired statuses stay drawn
  until a round-0 row removes them. That was the "boss frozen for five turns" icon.
- The actor's own expiries must NOT get a removal row: the client already removed
  them, and `removeStatusDataByID` (0x197C3D0) on an id it no longer holds calls
  `ServerRPCReportError`. `battle.Battle._queue_expired` is wired to exactly this split.

### `sync` SPD feeds the client's own action-line prediction

`BattleUnitManager.GetNextAction` (0x197E100) predicts the "next" badge by running
`(100 - scv) / SPD` over `LightBattleChar.SPD`, which `SyncData` (0x197D2BC) sets from
`sync[3]` every turn. So the server must send the EFFECTIVE speed, and -- the engine bug
this surfaced -- must use it too: `fill_time`, `fill_gauge` and `_roll_turn_order` had
been reading raw `spd`, so every SPD status in the game was cosmetic.

`SyncData` sets exactly MaxHP / HP / Scv / SPD and marks a unit dead when HP was >= 1
and is now 0. It carries NO statuses; those arrive only via attack rows (per action) and
`BattleDatas.status` (battle open / reconnect).

### `StatusST` is six wide, and the client draws the last three

```
per-action  .ctor(List<int>)           [0]=target [1]=skillID [2]=round [3]=lv [4]=value [5]=actOn
battle-open .ctor(int skillID, List)   [0]=round  [1]=value   [2]=actOn                 [5]=lv
```

- `UICharStatus.RefreshStatuIcons` (0x2000E00) draws TWO labels per icon: `round` and
  `lv`, each only when > 0. `lv` is the stack count (an inference from the client -- it
  is the only sensible occupant, and stackable rows are the ones named "(N)").
- `UICharStatus.SyncShield` (0x2000B2C) fills the shield bar by summing `value` over the
  unit's statuses whose `actOn` is in `StatusActOn.ShielStart..ShieldEnd` = 61..63,
  divided by MaxHP. No other reader of `value`/`actOn` was found. We had sent
  `[target, id, round]` and `[round, 0, 0, 0, 0, 0]`, so no stack count and no shield
  ever rendered. `engine.wire._status_row` and `battle._status_extras` now fill them.

### `DamageMode` has floating-text-only modes

`DamageMode` (TypeDefIndex 9110): HP=1 Reborn=2 CD=3 SCV=4 Status=5 BuffText=6
DebuffText=7 **Immunity=10097 Miss=10098 Forbid=10099**. `ImmunityFTHandler.OnCondition`
(0x1970EC8) accepts the last three; `DamageInfo.HasHP` (0x168708C) is false for them, so
such a row moves no HP. The server now sends a 10097 row for a target an immunity turned
a status away from -- but only when that target has no other row in group 0, which the
client keys on the unit alone.

### Confirmed, no change needed

- `onAddStatus` (0x197B058) is pure FX/animator bookkeeping: the client applies no
  immunity, skip or stacking logic of its own. Battle is server-authoritative.
- `UpdateStatus` (0x197C680): `args[2] == 0` -> remove by id, else insert (replacing a
  same-id entry -- a re-application refreshes rather than stacks on the client).
- `DamageInfo.IsDamage` (0x16870B4): Mode 1 with `Damage < 0`. Damage rides negative.
- `HandleJudge` (0x1689260): `intargs[1..2]` gate skill slots 1..2 (`== 0` means
  enabled); `intargs[3..5]` land in the actor's CharData lists index 1.

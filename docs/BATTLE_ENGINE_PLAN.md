# Battle engine — design plan

Companion to `BATTLE_CLIENT_CONTRACT.md`, which records what the client and the shipped
data tell us. This one is about what we build.

> **Revised** after decoding `action[]`/`act_id[]` (contract doc §6). Effects are no
> longer compiled from English prose — they come from the original server's own effect
> script, with prose filling only the numeric gaps.

## What actually goes wrong today

Worth naming precisely, because it determines the architecture. Every battle bug we have
chased has the same shape:

> **an ambiguous input is interpreted at runtime into a wire payload that violates a hard
> client contract, and fails silently.**

| bug | cause | symptom |
|---|---|---|
| Gabriel stalls the fight | two damage effects → duplicate target in one group | client throws inside a swallowed handler; attacker never yields; **fight hangs** |
| swings animate with no number | fewer groups sent than the cinematic has Damage tags | guarded read; **no error at all** |
| AoE hit one target | breadth guessed from English prose | quietly wrong damage |

None produced a server-side error. That is the thing to design against — not "write better
effect code".

## The four principles

1. **Interpretation happens offline, once.** Skills compile to a checked-in artifact a
   human can read and diff. Nothing parses prose at runtime.
2. **Structured data beats inference, and the client beats both.** Targeting breadth and
   swing counts come from the client; status application/removal comes from the opcodes.
   Prose is the *last* resort, not the first.
3. **The wire contracts are enforced in exactly one place**, with assertions, covered by
   tests that transcribe the client's own consumption logic.
4. **Every layer is testable without a client.**

## What each source of truth actually covers

This is the heart of the revision.

| concern | source | confidence |
|---|---|---|
| targeting group + breadth | `_target` → `23000+t` label table | **exact** |
| swing count | cinematic Damage tags (`hit` agrees 3138/3138) | **exact** |
| cooldown, charge, skill type, level chain | design columns | **exact** |
| **which statuses a skill applies / removes** | **opcodes 112 / 113 / 114** | **exact** |
| **resisted vs guaranteed application** | **112 vs 113** | **exact** |
| **status taxonomy** (buff/debuff/shield/DoT/HoT, stackable, stack cap) | **status id block + `(N)` suffix** | **exact** |
| **follow-up / pursuit attacks** | **opcode 117 → type-7 sub-skill** | **exact** |
| skill-CD manipulation | opcode 115 | strong |
| trigger timing (on-hit, after-action, turn-start) | no-operand opcodes | **inferred from prose** |
| status magnitude + duration ("DEF-50%, two turns") | prose glossary | inferred, cross-validatable |
| damage coefficient ("108% ATK") | prose only | inferred |
| **damage formula** | nowhere | **ours to invent** |

Two consequences worth stating plainly:

* **Damage is not in the script.** 195 pure-damage attack skills carry *no* opcodes at
  all, and no opcode ever encodes a coefficient. The script covers statuses, removals,
  follow-ups and CD — damage stays a prose-derived number.
* **83.2% of skill rows carry at least one opcode**, so the structured path covers the
  overwhelming majority of non-damage behaviour.

## Layers

```
design pack ─┬─ opcodes  ──► [compile] ──► skills.json    ─┐
             ├─ cinematics ─►               statuses.json  │
             └─ prose (numbers only) ─►                    ▼
   battle state ──────────────────────────────────► engine (pure)
                                                           │ outcome
                                                           ▼
                                                   serialiser ──► AttackJsonData
                                                   (invariants)
```

### 1. Skill spec — compiled from opcodes first

`tools/compile_skills.py` → `server/battle_data/skills.json`.

Per skill: `id, group, lv, type, target_group, breadth, swings, cd, charge, effects[]`.

`effects[]` is built **from the opcode slots**, not from prose:

```json
{ "op": "apply_status",  "status": 4101, "chance": false }   // 112
{ "op": "apply_status",  "status": 619,  "chance": true  }   // 113
{ "op": "remove_status", "status": 2005 }                    // 114, specific
{ "op": "remove_status", "category": "stackable_buff" }      // 114, block code
{ "op": "follow_up",     "skill": 2010141 }                  // 117 -> type-7 sub-skill
{ "op": "modify_cd",     "...": "..." }                      // 115
{ "op": "damage",        "basis": "ATK", "coefficient": 1.08 }  // from PROSE
```

Only the `damage` entry is prose-derived. Everything else is a direct read.

Manual overrides live in a **separate** file layered on top, so regeneration never
clobbers hand-fixes.

### 2. Status registry — taxonomy from ids, numbers from prose

`tools/compile_statuses.py` → `server/battle_data/statuses.json`.

* **kind and stacking come from the id block** — 1000 HoT, 2000 buff, 3000 stackable
  buff, 4000 shield, 5000 DoT, 6000 debuff, 7000 stackable debuff, 8000 passive grant,
  9000 stat up. Stack cap from the `(N)` name suffix.
* **magnitude and duration come from the glossary** (`* DEF Break UL: DEF-50%, lasting
  two turns.`), cross-validated across the 6,271 skills that define statuses — the same
  status is defined identically wherever it appears, so require agreement and report
  conflicts rather than trusting one row.
* joining opcode → glossary needs **name normalisation**: strip a trailing `(N)` and
  case-fold, or `Agony` will not match row `Agony(5)`.

### 3. Engine — pure

```python
resolve_targets(caster, skill, units)  -> [unit]       # breadth table
execute(caster, targets, skill, state) -> Outcome      # strikes, statuses, follow-ups
```

`Outcome` carries per-swing strikes (`seq`, target, amount), status events, and any
follow-up skills to run. Knows nothing about JSON.

**Follow-ups are recursive:** a type-7 sub-skill is a full skill spec with its own
targeting, swings and cinematic, so `execute` calls itself. Needs a depth guard.

### 4. Serialiser — the single choke point

`outcome_to_attack_json(outcome, swings)` is the only thing that builds `data`, and it
asserts:

* **exactly `swings` groups** — the count the cinematic will consume
* **at most one row per unit per group** — fold duplicates, summing
* `die` only on the last row naming a unit

Each of these has already cost a debugging session. Asserting here turns a silent client
hang into a server-side test failure.

## The damage formula — the one genuine unknown

No formula exists in the client and none is documented in the text data; the
attribute-advantage multiplier is not stated either. Proposal: one small parameterised
formula with named tunables in a single file —

```
raw   = base(stat) * coefficient
mit   = raw * defence_curve(target.DEF)
final = mit * attribute_mult * status_mults * crit
```

**Calibration signal:** the stage star conditions encode the designers' intended clear
speed — `_rating_datas` rows like "clear in at most 9 turns" for a stage whose mob HP and
party level are both known. A correctly-tuned curve should let a level-appropriate party
hit the 3-star turn limits on most campaign stages and miss them on the ones meant to be
hard. Not exact, but derived from the game's own data.

## Validation harness

* **static** — every compiled skill: swings vs cinematic, breadth resolvable, every
  opcode operand resolves, every referenced status exists
* **opcode/prose agreement** — statuses named by 112/113 vs those in the glossary; a
  divergence means a compile bug or a genuinely undocumented effect. Baseline after name
  normalisation, and treat regressions as failures
* **wire invariants** — the serialiser's three rules, transcribing the client's own logic
  (as `test_battle_effects.py` already does for duplicate targets)
* **turn inspector** — freeze a live fight, decode the outgoing message, edit, step.
  Defer the reply rather than blocking the handler thread (heartbeats share the socket,
  120 s idle timeout) and lock around `send` (RC4 keystream is stateful per direction)
* **differential** — current vs new engine over scripted fights; the point is to see what
  changes, not to prove equality

## Phasing

| phase | deliverable | why |
|---|---|---|
| **0** | inspector + static harness | measure before changing anything |
| **1** | `skills.json` — targeting, swings, cd, charge, **opcode effects** | almost entirely exact data |
| **2** | `statuses.json` — taxonomy from ids, numbers from glossary | the ambiguity, isolated |
| **3** | engine core + damage formula + follow-ups | the only genuinely new design |
| **4** | serialiser with enforced invariants | closes the silent-failure class |
| **5** | cutover behind a flag, with the differential report | reversible |

Phases 0–2 change no behaviour and are useful even if the rewrite stalls.

## Coverage vs the old prose parser (measured 2026-08-20)

The old system parsed **English prose**; the new one reads the **opcode script** and
falls back to prose only where no column exists. Headline:

| | old (`skill_effects.json`) | new (`battle_data/skills/`) |
|---|---|---|
| rows in corpus | 12,893 | **14,410** |
| ...with >= 1 effect | 10,309 (80.0%) | **12,866 (89.3%)** |
| ...fully decoded | 5,865 (45.5%) | **7,631 (53.0%)** |

The share is the least interesting row. Three things matter more:

**1,517 rows the old parser could not see at all** — it keyed off `note1_en`, so a row
with no English translation did not exist. Among them: **544 sub-skills** (the type-7
follow-ups) and 515 passives, i.e. precisely the multi-hit and chain machinery the
rewrite exists to fix.

**1,159 enemy skills that silently did nothing.** Damage is the one effect with no
opcode, so its coefficient must come from prose — but *whether* a skill attacks is
structural (`hit >= 1` + an enemy target group). The old code conflated the two and
emitted no damage entry when the prose had no percentage, so every untranslated mob and
boss attack (e.g. 100201 `爆触手`) compiled to nothing. Now the effect is always emitted
and only the coefficient can be unknown:

| coefficient source | effects |
|---|---|
| English prose | 8,346 (84%) |
| **original Chinese** `95%攻擊力` | 633 (6%) |
| unknown — engine applies a policy, knowingly | 986 (10%) |

The Chinese recovery is not a guess: `_note1` is the source language and carries a
percentage in *more* rows than the English (11,041 vs 10,837). It writes the percentage
before the stat, which is why an English-shaped pattern found nothing there.

**821 multi-hit disagreements (11.2%).** Where both systems decoded the same skill, the
old prose `times` and the new measured cinematic swing count differ on 821 of 7,304 —
`Glory Slash` old=1 / new=2, `Depression II` old=1 / new=3. The cinematic is ground truth
(§3), so those are old-system errors, and they are the reported bug: *"some skills that
should hit multiple times are not doing this."*

**The failure mode changed, which matters more than the count.** Prose parsing can be
confidently wrong — it reads English riddled with typos and returns a number either way.
Opcode decoding either recognises an opcode or files it under `unknown`, and every
inferred value now carries a `source`. Nothing is silently defaulted: an unknown duration
is `null`, never `0`.

---

## Phase 3 / 4 as built

`server/engine/` — a package, not more modules beside `battle.py`, so the boundary with
the old engine is visible. Nothing in it imports the old engine and nothing in it knows
what JSON looks like except `wire`.

| module | owns |
|---|---|
| `specs` | loading the compiled skill/status artifacts |
| `formula` | damage arithmetic, the attribute triangle — every tunable named, in one file |
| `core` | target resolution, execution, recursive follow-ups with a depth guard |
| `wire` | the ONLY place `data` is built, and where the invariants are asserted |

### The wire contract, decompiled

`AttackJsonData` carries only two `[JsonProperty]` fields — `passiveID` → `pskill_id` and
`DmgInfo` → `data`. `caster` and `skill` arrive through `.ctor(string caster, int skill)`,
which Newtonsoft matches to JSON keys by **parameter name**, so they are real wire keys
with no attribute to find.

`DamageInfo`: `Order c, Mode md, ChargeType cg, Damage dmg, nCri cri, nDie die,
status status, Extra extra, passiveIconList picons, passiveID pskill_id`.

`Mode`, from `AttackBehavior.OnDamage` (0x1BE2A18):

| mode | meaning |
|---|---|
| 1 | HP change. `Damage < 0` plays the hurt voice, records `CurInjures`, calls `PlayInjured` — so **damage rides NEGATIVE, healing positive** |
| 2 | revive — calls `doRebornUnit` then returns **early**, so a mode-2 row's `status`/`extra` are never read |
| 4 | move gauge — `ShowScvBar` |
| 5 | special-cased in `OnDamageAndNumber`: the unit lookup is skipped entirely |

### The three asserted invariants, and why each is silent without the assert

* **one group per cinematic swing.** `BscTag` case 5 reads `DmgInfo[0]` behind a
  `Count >= 1` guard, so too few groups does not throw — the swing animates with no
  damage number. Silent and cosmetic.
* **at most one row per (unit, mode) per group.** The client reads each group into a
  `Dictionary<string,bool>` keyed by target order; a duplicate throws "same key has
  already been added", the generic handler swallows it, and the attacker never yields its
  turn. Silent and **fatal**. Natural duplicates are folded by summing — one number per
  target per swing is all the shape can express, and the total is unchanged.
* **`die` on exactly one row per unit.** Rows carry final state, so a unit killed on
  swing 1 reads as dead on every later row and replays its death animation.

A fourth falls out of the tag contract: a **trailing** empty group is legitimate (every
target died before the later swings landed), an **interior** one is not — it would shift
every subsequent group onto the wrong tag.

Measured: **14,410 skills execute and 9,296 serialise with zero `WireError`.**

---

## Phase 5 — the cutover, and what the differential actually showed

`SEVENSINS_BATTLE_ENGINE=new` routes skill resolution through the new engine while the
old `Battle` keeps waves, turn order, cooldowns, rewards and the socket.
`engine/bridge.py` mirrors the live field into `core.Unit`s, resolves, and writes back
**only HP** plus the damage tallies the clear-rating stars read. Anything more would be
the new engine reaching into a model it does not own. Default is `old`.

Scope, stated plainly: the new engine does not yet own persistent status state, so the
flag is for A/B work, not a finished replacement.

### Comparing shape, not numbers

`tools/diff_engines.py` runs both engines over the same field and compares only what the
client consumes: group count, rows per group, which units are struck, which statuses
land. Damage amounts are ignored by design — the formula is invented, so identical
numbers were never the goal.

Two measurement traps had to be removed before the report meant anything:

* **A rider heal is also mode 1.** Counting every mode-1 row as "struck" made Ice Slash
  (*"Deals 70% ATK as damage and recovers the caster's HP"*) look like a 1-enemy skill
  hitting two. Struck = mode 1 with `dmg < 0`.
* **A level-100 party one-shots stage-1101 mobs.** Targets died on the first swing, so
  both engines legitimately stopped emitting groups — the old one filters empties, the
  new one trims trailing ones — and two different truncations of a shape *neither* got
  wrong were reported as swing divergence. The bench now makes everything unkillable for
  the duration of one skill use. Deaths are excluded from the diff entirely: they follow
  damage magnitude.

### Result over 560 comparable skill uses

| | count |
|---|---|
| shape identical | 171 |
| status ids differ | 349 |
| targeting differs | 55 |
| swing count differs | 54 |

**Every divergence checked resolves in the new engine's favour:**

* **swings — 53 of 54 are the old engine collapsing a multi-hit skill** (old 1 → new 2:
  38, → 3: 13, → 4: 2). This is the reported bug, quantified: ~9% of sampled skills.
  The single opposite case is a `hit`-vs-cinematic disagreement of the kind already
  catalogued in the contract doc (mob and team-skill cinematics), where the cinematic is
  what the client actually consumes.
* **statuses — 283 cases where the old engine applied NONE and the new applies some**,
  against 1 the other way.
* **targeting — checked against the client's own label table.** `Abyssal Prime` is
  `Player`, a self-buff: the old engine struck an enemy. `Land Crusher` is `2 enemies`:
  the old engine struck nobody.

The existing suites still pass on the default path (`test_battle_effects`,
`test_battle_resume`, `test_battle_auto`), so the flag is additive.

---

## Damage calibration — measured, not guessed

`tools/calibrate_damage.py`. The formula is invented, so the only honest check is the
designers' own intent, and they wrote it down: `_rating_datas` rows are
`[type, item_id, count, threshold]` with **type 3 = "clear within `threshold` turns"**.

`_stagelv` is the **mob level** for the stage — confirmed against the live game, where
6-4 (stage 6104, `_stagelv` 43) fields level-43 mobs. It is NOT a party level: the same
stage exists at difficulty 1/2/3 as 6104/6204/6304 with `_stagelv` 43/113/221, and the
column runs past 600, far beyond the character cap. Only the difficulty-1 band under the
cap is sampled — outside it the harness was building level-609 characters and measuring
nothing.

Running a level-appropriate party on auto to a clear, over 24 sampled stages:

| result | n |
|---|---|
| 3-star | 11 |
| cleared, missed 3-star | 5 |
| cleared, slow | 2 |
| wipe | 6 |

**Median 0.83× the 3-star turn limit for a BARE party — within the intended band.**

Two corrections were needed before that number meant anything, both from live play:

* **Count `Battle.round`, not loop iterations.** `end_turn` increments `round` once per
  unit ACTION, and it is what feeds `coll_f[0]` -> `CollectorData.TotalRound`, the value
  the star condition is actually compared against. Counting loop passes inflated it,
  because a wave advance costs an iteration and no action.
* **The harness party is BARE** — level only, no gear, runes, soulmirrors, transcendence
  or karma rank. Against a real level-100 account on the same characters:

  | Lucifer | sim | real | ratio |
  |---|---|---|---|
  | ATK | 1719 | 2861 | **1.66×** |
  | HP | 14064 | 25351 | 1.80× |
  | DEF | 685 | 1000 | 1.46× |

  and that gap lands directly on clear time: stage 6-4 took **10 rounds** in the harness
  against **6 in the live game**. Every figure here is therefore a conservative lower
  bound; a geared account sits near **0.50×** the limit.

That is the right shape for progression — a bare party scrapes the 3-star pace, a geared
one clears comfortably inside it. It also means the curve must NOT be tuned until the
harness reads 1.0, which would make the game far too slow for anyone with equipment.

All six wipes are side content, not campaign: Kizuna "Bond of STR/TEC", the arena
"Round of 8", and the "Time House of the Souls" challenge tower. Those are designed to be
hard and to want specific team comps, so a wipe there is not evidence about the curve.
The tool prints stage names for exactly this reason.

Worked example, stage 6104 (3-star limit 20 turns):

| party | turns |
|---|---|
| level-appropriate (43) | 22 — just misses 3-star |
| level 100 | 12 — comfortable 3-star |

**Caution against a false signal:** a level-100 party on level-43 content overkills every
mob ~27x per hit, which looks like a runaway damage curve and is not one. Judging the
formula from an overlevelled fight is how this was first misread. Always calibrate at
`_stagelv`.

---

## The endgame: retiring `battle.py`

The destination is deleting the old engine, and that changes how phase 6 should be built.
The bridge exists so the two can coexist; every seam added to it is scaffolding to be
deleted later. So the ordering principle is: **prefer a move that lets old code be
deleted over one that adds bridge code.**

### `Battle` is not one thing

42 methods, 880 lines, 29 of them public. `titan_server` touches 37 distinct names on it.
Grouped by what they actually are:

| concern | rough size | status |
|---|---|---|
| combat resolution (`attack_cmd_json`, `damage`) | ~190 lines | **already replaced** |
| rewards & results (ratings, drops, collector) | ~160 lines | progression, NOT engine |
| story hooks (`avg`, the three AVG lists) | small | progression, NOT engine |
| turn & wave flow (`end_turn`, `advance_wave`, `action_order`) | ~45 lines | to move |
| skills (`spend_skill`, `usable_slots`, `judge_args`) | ~45 lines | to move |
| party & AI (`swap_units`, `auto_move`) | ~40 lines | to move |
| wire payloads (`battle_datas_json`, `battle_cmd_json`) | ~65 lines | to move |
| persistence (`to_state`) | small | to move |

**Roughly half of `battle.py` is progression bookkeeping, not a battle engine.** Ratings,
drops and AVG hooks work, are well covered by tests, and would gain nothing from a
rewrite. "Delete the old system" really means "delete the old COMBAT system", which is a
much smaller target than the line count suggests.

### The pivot: one unit model

The cheapest move that turns every later step into a deletion is to stop having two Unit
classes. `core.Unit` already carries the combat stats; the old `bt.Unit` adds 19 fields,
almost all identity or progression (`char_id`, `lv`, `star`, `uid`, `skills`, `index`,
`job`, the pact and gear fields) plus a little battle state (`scv`, `cooldowns`,
`charge`, the damage tallies).

Blast radius, measured: **2 places construct a Unit and 4 references spell it
`.defense`**, all inside `battle.py`.

Once `Battle.units` holds `core.Unit`, both engines read and write the same object, and
the bridge stops needing to mirror anything. After that each responsibility moves by
deleting the old implementation rather than by adding a sync for it — including status
state, which is why this belongs BEFORE phase 6 rather than after.

---

## Phase 6 — status STATE (built)

`engine/status.py`. Statuses are now server-side state: they land on the unit, tick at
the start of its turn, expire, and are read by the damage formula and the turn loop. A
Freeze now actually stops the target acting.

### The sign rule, which is not what it looks like

The one genuinely subtle part. A magnitude's sign is **not** "buff or debuff". Over the
3,849 sites that state a sign explicitly it agrees with the id-block category 3,508 times
and disagrees 341 — and every disagreement is real: `Aging` is a debuff whose magnitude
reads "Damage taken **+4%**". A positive number that is bad for you.

So the sign describes the direction of the QUANTITY:

1. an explicit prose sign always wins;
2. otherwise the category decides **for a named stat** — a debuff lowers ATK/DEF/SPD/HP,
   a buff raises it, which is unambiguous;
3. otherwise the direction is UNKNOWN and the modifier is not applied.

Rule 3 matters because "damage taken" and "damage dealt" are the same `kind` with
opposite subjects; guessing there would silently invert an effect.

### Other decisions worth keeping

* **Percentages stack additively.** Two ATK-35% give ×0.30, not ×0.42 — that is what
  "stacks up to N times" reads as, and what the old engine did. Floored at 0 so a stack
  of debuffs cannot invert a stat.
* **`control` is not all turn-skipping.** The registry files Taunt and Charm as control
  too, but those REDIRECT a turn rather than deleting one, so `is_immobilized` matches by
  name (Stun/Freeze/Daze/…) rather than by kind.
* **A DoT snapshots the inflicter's ATK** at apply time, so it keeps hurting for the
  caster's power after the caster's buffs expire or the caster dies.
* **Immunity is narrow by design** — only a status whose own kind is `immunity` and whose
  name names the incoming category blocks it. A broad "any immunity blocks anything" rule
  would have CC Immunity blocking buffs.

### Coexistence

A unit's `statuses` list now holds both kinds — `battle_effects.Status` from the old path
and `status.Active` from the new one — because the two engines share the unit. The legacy
readers reach for `.definition` and `.tick()`, which an Active does not have, so every
legacy call site filters through `battle._legacy_statuses`. That list shrinks to nothing
as the old engine is retired, which is the point.

Scale: **84% of playable cast attack skills (1,993 of 2,364) apply at least one status**,
across 21,407 apply sites. This is also where the new engine's biggest measured advantage
over the old one sits — 283 sampled skills where the old engine applied *none* — and none
of it is real yet.

What has to move, by share of apply sites:

| kind | sites | what it needs |
|---|---|---|
| stat_mod | 5,749 (27%) | ATK/DEF/SPD modifiers read during damage |
| control | 3,757 (18%) | turn skipping — the old engine's only status read (`is_immobilized`) |
| other | 3,248 (15%) | unclassified; needs a pass before it can be executed |
| damage_mod | 2,094 (10%) | multipliers in `formula.strike` |
| immunity | 1,733 (8%) | gate on application, plus the `unremovable` flag already in the registry |
| dot | 1,681 (8%) | per-turn tick |
| gauge / heal / block_heal / shield | 2,799 (13%) | one rule each |

The data is ready: `statuses.json` has the kind, category, stack cap and dispellability,
and each apply site carries its own duration and magnitude with provenance. The gaps are
known and visible — duration on 53% of sites, magnitude on 32% — so an unstated one is a
policy decision, not a surprise.

**Why it is bigger than the resolution move.** The old `Battle` ticks statuses inside
`end_turn` and reads them in damage, targeting and turn skipping, so this is not one
seam. The engine needs to own apply / tick / expire and every read, which means the
bridge stops being a one-way HP mirror.

### After that

* **op 1 riders (326 sites)** whose kind is ambiguous, plus ops 7/3/118/6/11/122/119
  (~1,300 sites). Ops 6 and 118 are understood in shape but name their triggered skill
  only in prose, so they are not executable at any decoding effort.
* **2,623 damage effects with no coefficient** and 1,411 magnitudes — all flagged, none
  silent.
* **Enemy AI** is still `Battle.auto_move`.

---

## Phase 7 — what device testing found (2026-08-21)

Six defects in one session, every one of which produced plausible output server-side.
The pattern is worth more than the individual fixes: **the engine was almost always
right about WHAT happened and wrong about a quantity, a direction, or a recipient** —
none of which any assert could see, because each value was individually well-formed.

### Reading a clause: the sentence matters, and so does which sentence

Lucifer's Lamenting Starlight is a toggle whose two clauses each name BOTH markers — as
the condition in one, as the grant in the other. Matching a status to "the first
sentence naming it" gated both halves on The Divine, so the stance could never return.
`_clause_for` now prefers the sentence where a **granting verb precedes the name**.

Three more of the same family, all in `tools/compile_skills.py`:

* **`heals?` had no word boundary** and matched inside "**Heal**thy Strike IV: … ATK+35%",
  dragging a heal's magnitude, recipient AND basis off a line about a stat buff.
* **A possessive names the SOURCE of a number, not the recipient.** "restores HP to all
  allies by 20% of *the caster's* Max HP" was read as a caster-only heal because the
  word "caster" occurs in it. `clause_target` strips possessive stat phrases first;
  104 heals moved off `caster`.
* **"lasts the entire battle" matched none of `PERMANENT_RE`'s patterns**, so 807 apply
  sites silently became 2-turn buffs. On the wire that shipped `round: 1`, which the
  client counts to zero and deletes.

### A percentage is a percentage OF something

Every heal was applied as a fraction of the RECIPIENT's max HP. Michael's *"restores HP
of all allies by 250% ATK"* therefore healed each ally for 250% of their own bar — a
guaranteed full-party heal on a zero-cooldown skill, which reads as a design mistake
rather than ours. (The zero cooldown is real: `_cdTurn` is 0.)

| basis | sites | shape |
|---|---|---|
| % of the caster's ATK | 8 | "by 250% ATK" |
| % of the caster's max HP | 489 | "by 20% of the caster's Max HP" |
| % of the recipient's max HP | 542 | "recovers the caster's Max HP by 15%" |

Only the last scales per recipient. The bases differ by an order of magnitude, so an
unstated basis is not a small error.

### Direction: `Return` is a debuff, not a reflect

`ON_HIT_EXTRA` (was `REFLECT`). Michael's passive reads *"Sword Draw: when a battle
starts, inflicts Return on all **enemies**"*, and Return's own line — *"While taking
damage, deals Target's 100% ATK as damage"* — names the Target, which is the holder. We
paid it to the attacker instead, so a player passive was attacking its own party for the
attacker's full ATK once per skill.

**The test asserted the old direction and passed.** A test written from the same
misreading as the code confirms the misreading; it now checks both halves (attacker
takes nothing, holder takes its own ATK).

### Synthesised ids must never reach the client

`passives.py` mints a negative id from a status name when a rule names something with no
design row. Correct for tracking it on a unit, fatal on the wire — see contract §3.8.
Seven of the eight names that were synthesising had real rows all along (the lookup only
searched the cast's own kit, never the status registry) and the eighth was a dropped
comma in "Hold On, Classmates". `SYNTHESISED_ON_PURPOSE` is now an empty set, and a
check fails if anything lands in it silently.

Name matching is punctuation-insensitive but **qualifier-preserving**: exact matching
missed the comma, and loose matching folds `Keen`, `Keen UL`, `Keen(SP)`, `Keen(SP2)`
into one and hands rules the wrong magnitude.

### Nested status scripts (contract §6.4.2)

Implemented in `status.run_nested`, fired for every living unit at turn start. Removal is
**by id, never by name** — the pack ships duplicate rows per name and markers do
`apply 400, remove 400`; matching by name deleted the real marker. Apply/remove pairs a
single run produced are collapsed so a no-op never reaches the wire.

Retired Lucifer's three hand-written rules (CC Immunity, Keen, Teardown) — keeping them
would refresh each twice a turn. Gabriel's `Hold On, Classmates` stays a rule: its gate
is an HP threshold, not holding a marker.

### Delivering out-of-band status changes

Turn-start grants have no attack to ride on. They are queued and attached to the next
payload (status rows are unit-addressed, so any group can carry them), collapsing
repeats — otherwise a held marker adds a row per turn forever. Not persisted with the
battle: on reconnect the client rebuilds everything from `BattleDatas.status`.

### Reproducing a live fight in a harness

`Battle(stage, party, team_level, None, 0, 0)` silently drops ~31% of a real account's
stats. The server passes `team_star`, `team_super_star` and `book_rank` (6 / 6 / 241 on
the test account) and the party may not be formation 0. Wrong arguments produce numbers
that look plausible and are not — it cost two wrong answers about Michael's heal before
max HP matching the live trace to the digit confirmed the right party.

---

## Phase 8 — retiring `battle_effects` (2026-08-22)

### Measured: the new engine is a strict superset

| | player-castable skills it can run |
|---|---|
| old (`fx.is_complete`) | 4,754 / 8,333 — 57% |
| new (compiled spec) | 8,318 / 8,333 — 99.8% |

Buckets: old-yes/new-yes 4,754, old-no/new-yes 3,564, **old-yes/new-no 0**. The 15
neither runs are boss phase scripts (`即死`, "HP Changed to 50%"), not castable skills.

Knowledge source: the old engine ran off a **hand-written catalog of 748 status
definitions**; the new one reads the pack — 1,685 status rows, 14,410 skill specs. The
278 names "only the old catalog knew" are not a loss: **253 are never applied by any
skill** (prose clause names its scraper mistook for statuses — `Angel Blood`,
`Heavenly Focus`), 2 are covered by qualified rows (`Revelations` →
`Revelations (ATK)/(SPD)`), and ~23 are alias spellings (`Heal Block` vs `Block Heal`).

Op counts (old 11, new 9) are not comparable: the old engine had `stat_mod`, `shield`,
`immunity` and `cleanse` as skill OPS, while the new one derives those from the status
rows (`stat_multiplier`, `absorb`, `is_immune`, `remove_category`) and adds `follow_up`,
`attack_rider`, nested scripts and the attribute triangle.

### The flags were not "still handled by the old engine" — nothing handled them

`battle_effects.has_flag` filters through `own()`, which keeps only statuses carrying a
`.definition`. An engine status has none, so on the new path **every flag check returned
False**. The statuses applied, the icons drew, the trace listed them, and they did
nothing. Fixed, and each derived from the registry's own wording rather than `kind`:

* **seals are per-SLOT**, which the old boolean could not express — Power Attack Seal
  locks slot 1, Special Move Seal the ultimate, Skill Seal both. `kind` cannot separate
  them (605/606/607 are all `control`);
* **heal-block must NOT use `kind`.** That bucket holds 18 rows of which two block
  anything; it also collects healing INCREASES (Concentrate, Blessing), healing
  REDUCTIONS (Injured, Deterioration), an immunity, and Field Shield — a shield granting
  immunity *to* Block Heal. Keying on the kind blocked every heal in the game, because
  the party carries Field Shield from turn one. Matched on "cannot be healed" instead;
* **control is THREE redirects**, not one flag: Taunt "can only attack the taunt
  caster", Charm/Enchant "will attack allies", Confuse "will attack both allies and
  enemies". Charm and Confuse outrank Taunt. Taunt needs the inflicter, so `Active`
  gained `source_order`;
* **counters** needed no new trigger — `ON_DAMAGE_TAKEN` already fires per struck target
  and a rule can DAMAGE the ATTACKER. The bug was that every caller **discarded
  `fire_all`'s return value**, so passive damage moved server HP and the client was
  never told;
* **cooldowns did not exist.** `modify_cd` was a `pass` and all 1,012 sites carried no
  delta and no recipient, so `CD Reduction Block` had nothing to block. Both are now
  extracted from the cooldown clause (810 deltas, 679 recipients), signed, with
  "increase" winning ties. The block is checked on the SIGN — it refuses a refresh while
  a delay still lands.

### Save migration, not save loss

Battles are persisted on every message, so live saves carry old-format statuses at all
times. They convert exactly, because both models store the same facts under different
names: `remaining "battle"` → `None`, `dot_atk` → `source_atk`, `taunt_source` →
`source_order`. An unresolvable name is DROPPED, never given an invented id — the client
feeds every id to `GetRow`, which throws (contract §3.8).

### The default was the whole ballgame

`SEVENSINS_BATTLE_ENGINE` defaulted to `"old"` and **nothing sets it on the device** —
`main.py` exports `SEVENSINS_ACCOUNTS` / `_DESIGN_CACHE` / `_PATCH_ROOT` and nothing
else. Every phone ran the old engine while the desktop ran the new one purely because
the flag was passed on the command line. A flag whose default nobody audits is not a
safety mechanism; it is a second product.

### Pre-flight before deletion

Wrapping `fx` in a recording proxy and driving AI-vs-AI play (four stages, 89 turns on
the raid, wave advances, a save/restore mid-fight, plus the per-turn payload builders,
ratings, drops and rewards) touches `battle_effects` **zero times**. AI selection was
never the old engine's: `auto_move` is `usable_slots` + `skill_ratio` + first live
target; `design_enemy_targets`/`aoe_damage` live in the old ATTACK path.

### Done — 2,540 lines removed (2026-08-22)

Gone: the fallback attack path, `_defend_reduce`, `_status_wire`,
`_legacy_statuses`, every `NEW_ENGINE` branch, the flag, the package and its suite.
`passives()` now reads `specs.skill(sid).type == "passive"`.

Three things changed shape rather than disappearing, and each is the interesting part:

**A skill the engine cannot run ships a ZERO-DAMAGE combo entry** instead of falling
back. The 15 rows that reach it are boss phase scripts (`即死`, "HP Changed to 50%",
`boss轉階段`), not castable skills — inventing a basic attack for a script whose effect
we cannot read is worse than doing nothing, and the client still needs an entry to drive
the animation and yield the turn (an empty combo is not valid).

**`_status_to_state` still tags its output `_engine`** although that is now the only kind
written, because `_status_from_state` must keep reading saves written before the cutover.
An untagged dict is a legacy status and takes the migration path. The tag is not
redundant; it is the format discriminator.

**Two suites were testing the deleted engine**, and were re-pointed rather than deleted:

* the shield round-trip now asserts the same PROPERTIES — per-instance `shield_hp` and
  the inflicter's snapshotted ATK surviving a save — against engine statuses. What
  mattered was the property, never the class;
* the legacy-migration test now writes the old on-disk shape BY HAND. That is stronger
  than producing it with the old engine: what has to keep working is reading a save some
  phone wrote weeks ago, so pinning the exact on-disk form beats pinning whatever a live
  object happened to serialise to — and it does not need the old engine to exist;
* `check_legacy_and_engine_statuses_coexist` asserted an invariant that no longer exists
  (two representations sharing a unit without either reader choking). It now pins the
  replacement: that no second representation comes back, which is how the
  `'Active' object has no attribute 'dot_atk'` drop started.

### The check that actually cleared it

Not "grep the call sites". `tools/battle_fuzz.py` reports **2,411 attacks, 2,411 through
the engine, 0 through the legacy builder** across 40 randomised fights, and a recording
proxy over `fx` driving AI-vs-AI play touched the package zero times. Deleting on the
strength of a proxy that never fires is a much smaller leap than deleting on the strength
of having read every branch.

---

## Phase 9 — fuzzing the engine (2026-08-22)

`tools/battle_fuzz.py`. The suites walk the whole skill corpus but always on a handful of
hand-built fields; what they never produce is **combinations** — five arbitrary casts with
all their passives live at once, statuses landing on units that already hold conflicting
ones, revives firing into a queue that has already been pruned. Sampling teams at random
walks straight into that space: `tools/ai_arena.py` hit a `ValueError` inside
`engine.core._report` within eighty teams while trying to measure something else entirely.

### The bug that justified building it

`_passives.fire()` returns a **mixed** list — `(target, effect, amount)` for a damage or
heal rule, but `(target, Active)` for one that grants a status. `_report` unpacked
everything as a triple, so any passive whose damage trigger grants a status killed the
fight mid-turn with a `ValueError`. No stage in the suite fields one. The fix reports the
2-tuple as a `StatusEvent` rather than dropping it, for the reason `_report` exists at all:
state the server applied and never reported leaves the icon missing until the next sync.

### Two classes of finding, and the second is the dangerous one

* **Crashes.** An exception mid-turn kills the fight. On a phone the player sees the battle
  stop responding and the traceback goes to a crash log nobody reads.
* **Silent wire violations** — this project's signature bug. The server applies everything
  correctly, sends a payload that breaks a client contract, and the client's generic
  handler *swallows* the error: no stack, no animation, the attacker never yields its turn,
  and the fight hangs with nothing wrong server-side. The known instance is a duplicate `c`
  inside one `DamageInfo` group — the client reads the group into a dictionary keyed by `c`
  and throws "an item with the same key has already been added." Nothing server-side would
  ever notice. So the invariants are checked on the **real payload, on every attack**,
  rather than trusting that a fight which did not raise is a fight that works.

### Fuzz the payload the client receives, not the one the bridge returns

The first version checked `bridge.attack_combo`'s return value. That is not the payload,
and the difference is exactly where a wire bug would hide. After the bridge returns,
`attack_cmd_json` still folds queued out-of-band status changes onto the lead `DamageInfo`
(`_drain_pending_status`), and for any skill the new engine has no spec for it discards the
bridge entirely and **builds the groups by hand**. A fuzzer checking the bridge passes both
of those without ever having looked at them — and the drained rows are appended
unconditionally, which is precisely the shape of thing the duplicate-key hang is made of.
Driving `attack_cmd_json` instead raised the status rows exercised by ~13% on an identical
seed, all of them rows nothing had previously checked. The envelope is checked too
(`check_cmd`): a stale order in `line` or a unit missing from `sync` is the same silent
class — the payload parses and the handler quietly renders the wrong field.

`attack_cmd_json` grew an optional `rng=None` for this. The server never passes it — crit
and variance should be live — but the function that builds the payload is the one that has
to be replayable, or a fuzzer can only report that a failing fight existed.

### What the client actually does with a status row (read, not guessed)

`AttackBehavior.updateStatus` (0x1be32d0), the consumer of `DamageInfo.status`. Each row
is `[order, skillId, rounds]`, and the shape rules are enforced by throwing:

* **Exactly three elements.** `[1]` and `[2]` are read behind `size <= 1` / `size <= 2`
  guards that raise `ArgumentOutOfRangeException`; a **1-element row is silently skipped**
  by an outer `size != 1` test, so a short row either kills the turn or drops the status
  with no sign either way.
* **Element [1] is a SKILL id, not a status id.** The client calls
  `DesignSkillForm.GetRow(id)` and then feeds *that* row's status id to
  `DesignStatusForm.GetRow`. Both throw on a miss. This is the contract
  `status.wire_status_id` exists to satisfy, and the fuzzer now checks it on the emitted
  payload rather than trusting the three call sites that build it — a synthesised negative
  id (passives mint them for statuses with no design row of their own) reaching the wire is
  the documented infinite-loading-screen hang. Verified by fault injection: forcing
  `wire_status_id` to return `-175492` makes the sweep report it immediately.
* **Element [2] gates everything.** `BattleUnit.UpdateStatus` (0x197c680) branches on it:
  non-zero inserts, **zero removes by id**. The design lookups above sit behind the same
  test, so a remove row never touches the design pack.

### Duplicates: status rows are safe, `c` in group 0 is the hang

Two different duplicates, opposite answers. First: whether `_drain_pending_status`
appending the same `[target, sid]` twice could trigger the duplicate-key hang. It cannot.
Every dictionary write in the *status* consumption path is duplicate-tolerant:

| site | write | behaviour on a repeat |
|---|---|---|
| `updateStatus` `statusDic` | `ContainsKey` → `Add` | guarded; also keyed by **unit order**, not status id |
| `BattleUnit.instantInsertStatusData` (0x197c0d4) | `ContainsKey` → remove from list → `set_Item` | replaces, re-inserts at index 0 |
| `CollectAttackData` `CurDeathDic` (0x1be13ec) | `ContainsKey` → `set_Item` | guarded |
| `OnDamage` `CurInjures` (0x1be2a18) | `ContainsKey` → `Add` | guarded |
| `updateStatus` `CurFxDataDic` | `set_Item` | replaces |

So no invariant was added for the *status-row* duplicate.

**The duplicate-`c` throw site, located.** `AttackBehavior.PlayStart` (0x1be0298), at
**0x1be0a2c** — the one unguarded `Dictionary.Add` keyed by an order anywhere on the path:

```c
MainDamagerDic.Clear();                    // once, before the loop
for (row in DmgInfo[0]) {                  // ONLY the first group
    if (row.Mode == 5) { ...; continue; }  // multi-target branch, writes no dict
    unit = GetUnit(row.Order);             // row.Order is `c`
    MainDamagerDic.Add(unit.Order, 1);     // <-- no ContainsKey guard
}
```

`Mode` is at `+0x18` (confirmed against `get_HasHP`), `Order` at `+0x10`. A second row
naming the same unit throws `An item with the same key has already been added. Key: 101`,
which is the message logged on device. `PlayStart` runs *before* any damage is applied or
animated, which is why the attacker never yields its turn: the throw lands at the very
start of the attack, inside the handler that swallows it.

So the original note was right about the mechanism — the client really does read a group
into a dictionary keyed by `c`. It is just in `PlayStart`, not in the damage-application
path; every dictionary write in `HandleAttack` -> `DoAllDamage` -> `OnDamage` is guarded,
which is what made it look absent. **The fold in `attack_cmd_json`'s `add()` stays.**

Two refinements the original note could not have:

* **Only the first group can throw.** The loop reads `DmgInfo[0]` and nothing else, and
  `PlayStart` runs once per `AttackJsonData` (we send exactly one). A duplicate in a later
  swing's group is still malformed — the row is processed twice, so `CriCount`
  double-counts and one swing draws two damage numbers — but it cannot hang the fight.
* **`md == 5` rows are exempt**, taking the multi-target branch that writes no dictionary.

The fuzzer keeps the stricter all-groups check (one number per target per swing is the
intended shape regardless) but now reports a group-0 duplicate as the hang and a later-group
one as malformedness, so a finding says which it is.

### Result

2,000 randomised fights (mirror and real multi-wave stages, levels 1–400, all four
choosers, save/restore cycled mid-fight): **144,063 attacks, 15,499 deaths, 341,121 status
rows, 565 wave advances, 489 save/restore cycles, 2,889 distinct casts — 0 findings**, in
59 s. The coverage line is printed with the result on purpose, so "no findings" reads as
evidence rather than as an absence of evidence; a fuzzer that never reaches an interesting
state finds nothing either.

Two honest limits on that number:

* **The legacy attack builder is never exercised, because it is unreachable.** Of the 1,901
  distinct skill ids reachable from all 3,121 playable casts, **zero** lack a compiled
  spec. The counter reads `0 legacy` as a fact about the data, not as a gap in the sweep —
  and it is a standing argument that the fallback path can go when the flag does.
* **A fight involving Charm or Confuse is not reproducible from its seed.**
  `battle._forced_target` scrambles the target with the unseeded module-level
  `random.choice`, so the repro line printed with such a finding would not replay it.

---

## Risks

* **Trigger timing is the weakest link.** The no-operand opcodes are prose inference, not
  proof — and op 115 shows the set mixes triggers with operandless *effects*, so "no
  operand" must not be read as "condition". Expect to iterate here.
* **Damage tuning is subjective** and cannot be finished from data. Keep it in one file
  with named constants.
* **The long tail** — 14,410 rows; the harness should list what is unverified rather than
  pretending completeness.
* **Regression during cutover** — mitigated by the flag and the differential report.

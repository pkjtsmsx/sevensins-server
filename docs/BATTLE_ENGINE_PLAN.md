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

## Risks

* **Trigger timing is the weakest link.** The no-operand opcodes are prose inference, not
  proof — and op 115 shows the set mixes triggers with operandless *effects*, so "no
  operand" must not be read as "condition". Expect to iterate here.
* **Damage tuning is subjective** and cannot be finished from data. Keep it in one file
  with named constants.
* **The long tail** — 14,410 rows; the harness should list what is unverified rather than
  pretending completeness.
* **Regression during cutover** — mitigated by the flag and the differential report.

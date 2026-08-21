# Battle engine — design plan

Companion to `BATTLE_CLIENT_CONTRACT.md`, which records what the client tells us. This
one is about what we build.

## What actually goes wrong today

Worth naming precisely, because it determines the architecture. Every battle bug we have
chased has the same shape:

> **an ambiguous input is interpreted at runtime into a wire payload that violates a hard
> client contract, and fails silently.**

Three real examples:

| bug | cause | symptom |
|---|---|---|
| Gabriel stalls the fight | two damage effects → duplicate target in one group | client throws inside a swallowed handler; attacker never yields; **fight hangs** |
| swings animate with no number | fewer groups sent than the cinematic has Damage tags | guarded read; **no error at all** |
| AoE hit one target | breadth guessed from English prose | quietly wrong damage |

None of these produced a server-side error. That is the thing to design against — not
"write better effect code".

## The four principles

1. **Interpretation happens offline, once.** Skills compile to a checked-in data artifact
   that a human can read and diff. Nothing parses prose at runtime. A wrong skill becomes
   a visible one-line data change, not a mystery at turn 7.
2. **Client-derived facts beat inference.** Targeting breadth, swing count, cooldown,
   charge and skill type are all recoverable exactly (see the contract doc). Never guess
   what we can read.
3. **The wire contracts are enforced in exactly one place**, with assertions, and that
   place is covered by tests that transcribe the client's own logic.
4. **Every layer is testable without a client.** The engine is pure functions over state;
   the serialiser is a pure function of an outcome.

## Layers

```
design pack ──► [compile] ──► skills.json      ─┐
                             statuses.json      │
                                                ▼
   battle state ──────────────────────► engine (pure)
                                                │  outcome
                                                ▼
                                        serialiser ──► AttackJsonData
                                        (invariants)
```

### 1. Skill spec — compiled, checked in

`tools/compile_skills.py` → `server/battle_data/skills.json`.

Per skill: `id, group, lv, type, target_group, breadth, swings, cd, charge, effects[]`.

* **breadth** from the `23000 + _target` table — exact, no inference
* **swings** from the cinematic Damage-tag count where the cinematic has tags, else `hit`
  (they agree on 3,138 / 3,138 real player attack skills)
* **effects** an ordered op list: `damage`, `status`, `heal`, `gauge`, `cd`, with
  coefficient, stat basis and condition

Generated offline; **hand-editable and hand-patchable**, because the long tail will need
it. Regeneration must never clobber a manual override — keep overrides in a separate file
that layers on top.

### 2. Status registry — compiled, cross-validated

`tools/compile_statuses.py` → `server/battle_data/statuses.json`.

Source: the `* Name: effect, lasting N turns.` glossary lines in `note1_en` — 6,271 skills
define 326 distinct statuses. **The same status is defined identically wherever it
appears**, so this is a cross-validation problem, not a trust-one-row problem: parse every
definition, group by name, and require agreement. Report conflicts rather than silently
picking one.

Model per status: kind (stat mod / damage-taken multiplier / DoT / immunity / gauge lock /
action denial), magnitude, duration, stacking rule, and whether it is dispellable.

### 3. Engine — pure

```python
resolve_targets(caster, skill, units)  -> [unit]        # breadth table
execute(caster, targets, skill, state) -> Outcome       # strikes, statuses, gauge, cd
```

`Outcome` carries **per-swing strikes** (`seq`, target, amount) plus status events. It
knows nothing about JSON. This is where the damage formula lives, and it is the only place
that needs tuning.

### 4. Serialiser — the single choke point

`outcome_to_attack_json(outcome, swings)` and nothing else builds `data`. It asserts:

* **exactly `swings` groups** — the count the cinematic will consume
* **at most one row per unit per group** — fold duplicates, summing
* `die` only on the last row naming a unit

These are the three rules that have each cost us a debugging session. Asserting them here
converts a silent client hang into a server-side test failure.

## The damage formula — the one genuine unknown

There is **no formula anywhere in the client** and none documented in the text data. Nor
is the attribute-advantage multiplier stated. This is ours to invent.

Proposal: a small parameterised formula with named tunables in one file, rather than
constants scattered through the code —

```
raw   = base(stat) * coefficient          # stat per the skill's basis (ATK/DEF/MaxHP)
mit   = raw * defence_curve(target.DEF)
final = mit * attribute_mult * status_mults * crit
```

**Validation signal, since we have no ground truth:** the stage star conditions encode the
designers' intended clear speed — `_rating_datas` rows like "clear in at most 9 turns" for
a stage whose mob HP and party level are both known. That gives a calibration target for
the whole curve: a correctly-tuned formula should let a level-appropriate party hit the
3-star turn limits on a decent share of campaign stages, and miss them on the ones meant
to be hard. It is not exact, but it is a real signal derived from the game's own data
rather than a guess.

## Validation harness

* **static** — every compiled skill: swings vs cinematic, breadth resolvable, effects
  well-formed, statuses referenced exist
* **wire invariants** — the serialiser's three rules, as tests that transcribe the
  client's own consumption logic (as `test_battle_effects.py` already does for the
  duplicate-target rule)
* **turn inspector** — freeze a live fight on any turn, decode the outgoing message, edit
  and step. Defer the reply rather than blocking the handler thread (heartbeats share the
  socket and a 120 s idle timeout applies), and take a lock around `send` because the RC4
  keystream is stateful per direction
* **differential** — run current and new engines over scripted fights and diff outcomes;
  the point is to see *what changes*, not to prove equality

## Phasing

| phase | deliverable | why first |
|---|---|---|
| **0** | inspector + static harness | measure before changing anything |
| **1** | `skills.json` (targeting, swings, cd, charge) + validator | pure client-derived facts, no invention |
| **2** | `statuses.json` from the glossary, with a conflict report | the biggest ambiguity, isolated |
| **3** | engine core + damage formula | the only genuinely new design |
| **4** | serialiser with enforced invariants | closes the silent-failure class |
| **5** | cutover behind a flag, with the differential report | reversible |

Phases 0–2 produce **no behaviour change at all** — they are data and tooling, and they
are independently useful even if the rewrite stalls.

## Risks

* **The long tail.** 14,410 skill rows; the top few hundred cover most play. Compile all,
  ship the common ones verified, and let the harness list what is unverified.
* **Damage tuning is subjective** and cannot be finished from data alone. Keep it in one
  file with named constants so it is tunable without touching engine logic.
* **Regression during cutover.** Mitigated by the flag and the differential report.
* **`action[]`/`act_id[]` remain undecoded** — 21 opcodes, no client-side ground truth
  (see the contract doc §6). If they are ever decoded they replace the prose-derived
  effect list, which is why effects are a separate compiled artifact rather than being
  woven through the engine.

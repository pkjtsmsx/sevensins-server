# Battle AI — tier 1 spec

Companion to `BATTLE_ENGINE_PLAN.md`. That one is about resolving a skill correctly; this
one is about *choosing* which skill, for both the enemy turn and player auto-battle.

## What exists today

`Battle.auto_move` (`server/battle.py:3105`) is two greedy heuristics and nothing else:

```python
slot   = max(self.usable_slots(attacker), key=lambda i: skill_ratio(attacker.skills[i]))
target = targets[0]                       # first live unit, dict insertion order
```

`usable_slots` is the only real logic in the decision (cooldowns, ultimate charge, per-slot
seals). One function serves the enemy AI *and* the player's auto-battle, which is why
auto-battle exists at all — `SituationJudgeState` is a stub and the client only acts when
the server pushes Attack (1201).

Three things are wrong with it, in order of how visible they are:

| problem | evidence |
|---|---|
| `targets[0]` never focus-fires or finishes a kill | the party chews left-to-right while a 3%-HP enemy keeps acting |
| `skill_ratio` greps the first `\d+%` out of localized prose | disagrees with the compiled coefficient by >0.4 on **385** damage skills; blind to basis (472 DEF-, 383 MAX_HP-, 293 HP-based effects), to swings (**3,323** skills hit 2–5×) and to breadth |
| no notion of what a skill *does* | 1,039 heal skills, 269 revives, 21,475 `apply_status` effects, all ranked by whatever number appears in their text — so heals fire at full HP, revives with nobody dead, Freeze lands on the frozen |

The irony is that `battle_data/skills/*.json` already carries coefficient, basis, swings and
target breadth, and `engine.core.execute` reads it every turn. The chooser is guessing from
text the resolver no longer needs.

## Scope, and the measured reason for it

Tier 1 is **honest one-ply evaluation**: simulate every legal move and score the result.
Tier 2 (turn-order awareness) rides along for free because `Battle.turn_order` is already
computed and exact. Deeper search is **out of scope, not deferred** — measured on a real
device (SM-S906W, Chaquopy CPython 3.12.7, arm64) against desktop:

| | desktop | phone | ratio |
|---|---|---|---|
| plain-Python reference loop | 9.9 ms | 35.3 ms | **3.6×** |
| one dry-run skill (`apply_damage=False`) | 0.014 ms | 0.060 ms | 4.3× |
| current chooser, whole decision | 0.0019 ms | 0.0095 ms | 5.1× |
| **tier 1** — 48 dry runs (every slot × target × 8 seeds) | 0.97 ms | **3.87 ms** | 4.0× |
| clone + resolve a turn — *what the server already does* | 0.23 ms | 1.07 ms | 4.7× |

Tier 1 costs **~3.9 ms of one core per enemy turn**, unoptimized; a 20-turn fight spends
~78 ms of CPU on AI in total, while the client animates each of those turns for a second or
more and renders the fight at 60 fps throughout. It is a rounding error against what the
phone is already doing.

A 2-ply search is ~225 clone-and-resolve positions ≈ **240 ms per enemy turn** on the same
device — a visible pause and ~60× the energy, sustained, for gains that don't show. The
things players actually notice (focus fire, lethal detection, not wasting a heal) come from
evaluating one ply *honestly*, not from searching two. **We are not building tier 3.**

## Architecture: the policy seam

New module `server/battle_ai.py`. Not under `engine/` — that package is documented pure (no
design pack, no JSON, no globals) and the chooser needs `Battle` for cooldowns, seals and
the turn queue.

`Battle.auto_move` keeps its signature, its return tuple and its two callers
(`titan_server.play_turn_msgs`, the tests). It becomes a delegation:

```python
def auto_move(self, target_team):
    return battle_ai.choose(self, target_team)      # -> (attacker, defender, skill, slot)
```

The split the current code cannot express:

```python
choose(battle, target_team, policy=None)
    # policy defaults by the ACTING unit's team:
    #   team 1 acting -> POLICIES["player_auto"]     the player's own auto-battle
    #   team 2 acting -> POLICIES["enemy"]           the opposition
```

A `Policy` is a frozen dataclass of weights and knobs, **not a subclass**. Difficulty is
data, so it can be tuned per stage without a code path per boss.

## The candidate set

Built once per decision, for the acting unit:

1. **Forced target first.** `battle._forced_target(attacker)` — Taunt redirects to the
   inflicter, Charm/Confuse scramble the side. If it returns a unit, the candidate target
   list is exactly `[that unit]`.
   **Trap:** `attack_cmd_json` applies this override *after* we choose (`battle.py:2496`),
   so scoring moves against a target we will never hit is silently wrong — the chooser must
   consult it up front, not discover it later.
2. **Slots**: `battle.usable_slots(attacker)`, unchanged. It already honours cooldowns, the
   ultimate's charge gate and per-slot seals, and never returns empty (falls back to `[0]`).
3. **Leads**, per slot, from `specs.skill(sid)["target"]["select"]`:
   - `count` → the lead genuinely changes the outcome; enumerate live units of the eligible
     group.
   - everything else (`all`, `all_except_self`, `random`, `self_plus`, `highest`, `lowest`,
     `by_attribute`, `none`) → `resolve_targets` **ignores `chosen`**; enumerate exactly one
     candidate, `None`.

   Step 3 is most of the cost saving: it takes the naive 48 dry runs down to ~20.

Candidate = `(slot, lead)`. Worst case 3 slots × 5 enemies = 15; typically 5–8.

## Evaluation

For each candidate, K seeded dry runs, averaged:

```python
core.execute(caster, spec, units, random.Random(seed), chosen=lead, apply_damage=False)
```

### The four dry-run divergences

`apply_damage=False` is a preview, not a replay. Each divergence flatters or penalizes a
move if the scorer ignores it, and all four are silent:

| divergence | what the dry run reports | what the scorer must do |
|---|---|---|
| HP never drops | `Strike.died` is **always False** — `_flag_deaths` reads `t.alive`, which never changes | accumulate damage per target and compare against `hp` yourself |
| shields not consumed | `_status.absorb` is `apply_damage`-gated, so damage is reported pre-shield | subtract each target's shield pool (sum of `Active.shield_hp`) once per target |
| immunities not consulted | `_status.apply_event` is gated, so a status an immunity would block is still reported | `_status.is_immune(tgt, row)` before crediting any status |
| counters/reflect don't fire | `_damage_hooks` is gated | ignore — it's a symmetric cost across candidates. Noted so nobody "fixes" it later by mutating during evaluation |

Two more raw-number traps, independent of the flag: `Strike.amount` includes **overkill**
and `heals[].amount` includes **overheal** (the engine clamps on application, not in the
record). Both must be clamped by the scorer — clamping the heal is the entire fix for
"healer heals at full HP".

### The score

One currency: **HP-equivalent points**, so every term is comparable and the weights table
has a meaning rather than a vibe.

1. **Effective damage** — per enemy target, `min(Σ strikes, hp_now + shield_pool)`. Overkill
   scores zero, which is what makes AoE-vs-single a real comparison instead of a coefficient
   contest.
2. **Kill credit, continuous** — damage is only worth something insofar as it leads to a
   kill, so each target is also paid `share × kill_horizon × threat(target)`, where
   `share = min(1, damage / pool)`. At `damage ≥ pool` the share is 1 and the full kill is
   paid, so this *subsumes* a discrete kill bonus rather than sitting beside it.

   **Continuity is what produces focus fire, and a discrete bonus does not.** With a
   threshold bonus, two targets that both survive the hit score identically and the pick
   falls to the tie-break — the AI spreads damage across the field and only converges when
   it can finish someone this turn. Scoring the share makes the same 2,000 damage worth far
   more against a target at 10% HP than a fresh one, so the finisher wins without a special
   case. `test_battle_ai.py` pins this: the sub-lethal focus check fails against the
   discrete version and passes against this one.

   `kill_horizon` defaults to 3 — a kill removes *every* turn the unit had left, not one.
   At a horizon of 1, chipping a healthy tank for 5,000 outscores finishing a 200-HP enemy.
3. **Effective heal** — per ally, `min(amount, max_hp - hp)`.
4. **Revive** — `hp_restored + revive_horizon × threat(unit)`. The engine's revive pool is
   already filtered to dead allies, so an empty `revives` list means the skill did nothing:
   score 0 and the revive stops being chosen with nobody down.
5. **Statuses**, keyed by `kind` from `battle_data/statuses.json` (1,359 of 1,685 rows at
   confidence 1.0), scaled by duration:
   - `control` — `threat(target) × turns`. It deletes that unit's turns outright; the most
     valuable debuff in the game and currently invisible.
   - `dot` — magnitude if known, else a conservative constant × turns.
   - `shield` — magnitude if known; it is already HP, so it needs no conversion.
   - `stat_mod` — `Active.signed_magnitude() × stat weight × turns`.
   - `damage_mod`, `gauge`, `heal`, `block_heal`, `immunity` — small per-kind constants.
   - `other` and `None` (**752 rows**) — **zero, deliberately**. An unquantified status must
     never be able to outrank a measured kill.
   - **Sign** comes from `category` (buff/debuff) crossed with the recipient's team. When the
     two disagree, the compiler was unsure: score 0 rather than guess a direction.
   - **Magnitude is missing at 14,357 of 21,475 `apply_status` sites**, and duration at
     9,867. Where unknown, use a small constant — enough that the AI prefers doing something
     to doing nothing, never enough to beat a real kill. This is the honest ceiling on tier
     1's precision, and it is a data limit, not an algorithm limit.
6. **Redundancy** — if the recipient already holds the status (`_status._actives`) and it is
   not stackable, or is at `stack_cap`, the term is **0**. This is what stops re-freezing a
   frozen target while a second enemy stands unfrozen.
7. **Tempo (tier 2)** — `outcome.gauge`: a gain on an ally or a loss on an enemy is worth
   `pct/100 × threat(unit)`, because a full gauge *is* one turn. `outcome.cooldowns`
   likewise, valued as a fraction of the skill it refreshes.
8. **Cooldown cost** — subtract `CD_COST × cd_turns(slot) × value_of_basic`. Small, and it
   exists only to stop the ultimate being dumped on a trash mob at 5% HP.

### threat(unit)

The quantity kill, control and tempo all lean on: what a unit is worth per turn.

```
threat(u) = effective_atk(u) × best_compiled_coefficient(u) × proximity(u)
```

Compiled coefficients, never prose. `proximity` discounts by how soon the unit acts, read
straight from `battle.turn_order` — already computed each turn, and exact, because
`_roll_turn_order` is deterministic arithmetic over SPD and scv. Tier 2 costs nothing here
beyond a dictionary lookup.

Known gap: `threat` ignores statuses on the threatening unit, so an ATK-buffed enemy is
undervalued. `stat_multiplier` makes that a one-line fix later; it is left out of v1 to keep
the weights tunable against one variable at a time.

### Sampling

Crit (5% base, ±15% by attribute), ±5% variance, the 30% attribute-miss branch on
disadvantage, and per-status chance all make a single dry run noisy.

**K = 4, with the same fixed seeds (0..3) for every candidate.** Paired seeds matter more
than K: shared noise cancels in the comparison, so paired K=4 beats independent K=16. Each
candidate gets fresh `random.Random(s)` objects, never a shared stream — a shared stream
makes the result depend on candidate evaluation order.

### Determinism

`argmax` with a stable tie-break (lowest slot, then lowest target order). No randomness in
the choice itself for `player_auto`, so a fight replays identically across the
battle-resume path.

The returned defender: for `count` skills it is the chosen lead; for the rest, the first
entry of `outcome.targets`, so the client's animation aims at a unit that was actually hit
rather than at `targets[0]` by coincidence.

## The two policies

| knob | `player_auto` | `enemy` |
|---|---|---|
| selection | `argmax` | softmax over top-k |
| temperature | 0 | tunable, default mild |
| blunder rate | 0 | tunable, default 0 |
| K samples | 4 | 4 |
| kill horizon | 3.0 | 3.0 |
| cooldown cost | small (0.12) | larger (0.20) — a boss holding its ultimate reads as deliberate |

Difficulty is **temperature and blunder rate, not a second algorithm**. At temperature 0 an
enemy plays full tier 1; raising it makes it take the 2nd or 3rd best move sometimes. One
number to tune, and far more legible than hand-written dumb heuristics. Default the enemies
to mildly imperfect — a surgical opponent that always focus-fires your healer is a worse
game, which is the same judgement that keeps tier 3 out.

## Acceptance — `test_battle_ai.py`

Each is a behaviour, not a unit test of the scorer:

1. **Lethal** — two enemies, one at 5% HP: the killing move is chosen, *and* the greedy
   chooser is asserted to get it wrong, so the test is anchored to a behaviour change
   rather than to a number. The killable enemy is deliberately NOT the one `targets[0]`
   lands on, or the test would pass on a coincidence.
2. **Sub-lethal focus** — damage that finishes neither target still goes to the weaker one.
3. **No overheal** — party at full HP: the healer attacks instead of healing.
4. **No wasted revive** — nobody dead: a revive skill is not chosen.
5. **No redundant control** — one enemy frozen, one not: the freeze goes to the unfrozen one.
6. **Breadth** — 3 enemies alive: the all-enemies skill outranks an equal-coefficient single
   target.
7. **Basis honesty** — a `MAX_HP`-basis nuke against a high-HP boss outranks an ATK skill
   with a larger printed percentage.
8. **Taunt** — a taunted attacker targets the taunter, *and* the score was computed against
   it (assert the chosen slot is the one that is best against the taunter, not against the
   free choice).
9. **Determinism** — same state, same choice, twice, and across `to_state`/`restore_battle`.
10. **Budget** — a 5v5 decision with the stage's own skills stays under 1.5 ms on desktop,
   which is the ~6 ms on-device budget at the measured 4.0× ratio. A regression guard: the
   scorer will be tempted to grow.

Measured after implementation: **0.35 ms** per decision on desktop for a real 5-unit field
— better than the 3.87 ms the pre-implementation benchmark projected, because the lead-dedup
in `_candidates` and K=4 (rather than 8) between them cut the dry-run count roughly in half.

### On the device, for real (2026-08-23)

Every phone number above is the *pre-implementation* estimate: a synthetic loop standing in
for a chooser that did not exist yet. These are `battle_ai.choose` itself, through
`Battle.auto_move`, on the same SM-S906W (Chaquopy CPython 3.12.7, arm64) against the same
desktop, same stage (1600002), same seeded roster:

| | desktop | phone | ratio |
|---|---|---|---|
| plain-Python reference loop | 9.6 ms | 37.6 ms | 3.9× |
| **player auto-battle decision** | 0.36 ms | **1.50 ms** | 4.1× |
| enemy decision (softmax policy) | 0.27 ms | 1.11 ms | 4.1× |
| greedy chooser, same field | 0.0017 ms | 0.0071 ms | 4.1× |
| tier 1 with **five** live enemies (widest candidate set) | 1.03 ms | 4.12 ms | 4.0× |
| clone + resolve a whole turn, tier 1 included | 0.63 ms | 2.49 ms | 4.0× |

**1.50 ms**, against the ~1.4 ms the 4.0× ratio projected — the projection was honest, and
the 3.87 ms pre-implementation estimate was pessimistic by 2.6×. The last row is the one to
read: a server turn on the phone went from 1.07 ms to **2.49 ms**, so the chooser is now the
majority of what resolving a turn costs and the turn is still ~2.5 ms against a client that
animates it for a second or more. The worst case the game can field — five live enemies —
is 4.12 ms, inside the 6 ms budget the acceptance test guards.

One thing the device run establishes that no desktop run could: the phone chose **the same
move** (`101 → 103`, skill 1000114, slot 1) as x86_64 CPython 3.14. Paired seeds and an
argmax with a stable tie-break mean the decision does not depend on the architecture or the
interpreter build, which is what the battle-resume path needs across a server restart.

## Rollout

- `SEVENSINS_BATTLE_AI=old` escape hatch, mirroring `SEVENSINS_BATTLE_ENGINE`, so a bad
  weight table is one env var away from the old behaviour.
- Server-side only — ships as a hot update, no APK rebuild.
- `skill_ratio` leaves the decision path but **must not be deleted**: `Battle.damage`
  (`battle.py:2742`) still uses it on the legacy path. It goes when that path does.

## Measuring it: `tools/ai_arena.py`

"Is the new chooser better?" needs an instrument, and the obvious one does not work on
this game's data. Playing real stages and comparing clear rates measures nothing, because
the fights are not contested: a seeded roster one-shots the early stages (1,784 HP against
239-HP mobs) and is hopeless against the later ones, where the enemies act first and wipe
it in five turns without it ever taking a turn. Both choosers score identically for reasons
that have nothing to do with either.

**The mirror match is the instrument.** Identical five-cast teams on both sides, one
chooser driving each, every pairing played twice with the sides swapped (same team, same
seed) so the first-mover advantage cancels. Nothing is left that can decide the fight
except the decisions. Fights run a median of ~21 turns, which is where a chooser's quality
actually lives.

Three traps it encodes, each learned by falling into it:

  * **Never scale ATK to lengthen a fight.** Recoil and damage-over-time read the
    *attacker's own* ATK (`status.tick_damage` snapshots `source_atk`), so an ATK-scaled
    sweep has the enemies killing themselves — the first attempt at this measured suicide
    rates and confidently reported the wrong winner. Stage mode scales HP only.
  * **A stalled fight is not a contested one.** Raising enemy HP always drives the win rate
    toward 50%, but past a point it does so by making the fight unfinishable, and the
    result is then decided by which fights landed just inside the turn cap. Scales where a
    quarter of fights draw, or the median runs past 80% of the cap, are rejected outright.
  * **A draw is not a loss.** Scoring turn-cap draws as losses once produced a 2:1 gap
    between two choosers whose damage per turn differed by 0.4%.

`--null` (a chooser against itself) is a *structural* check only — with the same chooser on
both sides the two orientations are the same fight, so the result is forced to exactly 50%.
It verifies the swap bookkeeping and nothing more; calling it evidence of an unbiased
harness would be a politer version of the mistake the tool exists to prevent. **The check
that carries weight is `random`** — any legal move, uniformly — kept in the table as a
scale bar. A chooser that cannot beat random decisively means the metric is too coarse for
a close A-vs-B number to mean anything either.

### Results

Mirror matches, 95% CI. Player pool: 80 teams x 8 seeds x 2 orientations = 1,280 fights
per pairing. Mob pool: 60 teams x 8 seeds x 2 = 960 per pairing.

| pairing | player casts (auto-battle) | mob casts (enemy AI) |
|---|---|---|
| tier1 vs greedy | **73.4% ± 2.4** | **74.3% ± 2.4** |
| tier1 vs random | 87.4% ± 1.8 | 74.8% ± 2.3 |
| greedy vs random | 67.5% ± 2.6 | **53.6% ± 2.8** |
| tier1 vs tier1-enemy | — | 63.3% ± 2.6 |
| tier1-enemy vs greedy | — | 69.8% ± 2.5 |

Three things worth reading off that table:

  * Tier 1 beats the old chooser about **3:1** on both pools, far outside the interval.
  * **Greedy driving mobs barely beats random** (53.6%, and the interval nearly touches
    50%). Mob skill prose is largely untranslated, so `skill_ratio` falls back to 1.0 for
    every skill and the old enemy AI was close to picking at random. That is the single
    clearest measurement of what was wrong with it.
  * The shipped enemy handicap is now a measured quantity rather than a guess: `tier1`
    beats `tier1-enemy` 63/37, which is what temperature 0.35 costs. Tuning difficulty no
    longer means guessing.

The structural null came out at 52.5% ± 7.7 rather than exactly 50%. The residual is real
and worth knowing about: `battle._forced_target` scrambles a Charm/Confuse target with the
unseeded module-level `random.choice`, so a fight involving one is not reproducible from
its seed. Harmless for a scramble, and the interval covers 50%, but it means the null is
not the pure identity the design assumes whenever those statuses are on the field.

### Campaign mode: the regime real play occupies

The mirror match measures relative strength; it cannot say whether the difference is ever
*felt*. Two things had to change to answer that:

  * **Multi-wave.** 4,575 of the 6,625 fightable stages run 2, 3 or 5 waves, and
    `wave_cleared()` only ever meant "this wave". Everything before this measured wave 1
    and called it a clear — missing the whole point of multi-wave play, which is carrying
    HP, cooldowns and statuses forward.
  * **A party that belongs there.** The default test account is deliberately overpowered
    for early content, so its fights are decided before a decision is made. Party level is
    calibrated per stage — binary search to the level where the reference chooser clears
    it about half the time. Levels are the honest knob: it is what the game gates progress
    with, it moves the party along the pack's own `_growStar` curve, and unlike scaling the
    enemies it distorts no mechanic.

Difficulty anchored on the OLD chooser (content tuned so greedy is near 50%), 29 stages
all contested, 232 fights:

| | clears | mean party HP left |
|---|---|---|
| tier1 | **90.9% ± 3.7** | 47–70% |
| greedy | 62.5% ± 6.2 | 5–24% |

Anchored on the NEW chooser instead (240 fights): tier1 71.2% ± 5.7, greedy **19.6% ± 5.0**.

**The chooser changes the win/loss outcome in 35% of fights (62% on the harder anchor).**
Against 1.1% on the over-levelled test account — same code, same stages; the difference is
entirely whether the fight was ever in doubt.

### What it is worth in party levels

The most legible unit available, since levels are what a player actually grinds. For each
stage, the level at which each chooser clears it half the time:

**Median +21 levels, mean +18.7 — about 22% of the level the old chooser needs.**

Not universal: 3 of 15 stages went the other way. The obvious hypothesis for those — tier 1
evaluates one ply, so it spends cooldowns and HP on the wave in front of it with no notion
of saving anything for wave 5 — was tested directly and **not supported**: the level gap on
5-wave stages (median +14, n=12) is in line with 3-wave stages (+18, n=12). Variance is
higher on the long ones (3 of 12 negative against 1 of 12), so there may be something
there, but it is not the systematic cross-wave weakness the one-ply design would predict.

Stage mode agrees where a stage is genuinely contested and finishable — on 1600002 at the
calibrated enemy HP ×40, tier1 scores 87.5% against greedy's 37.5% and finishes with 43%
party HP against 14%. Most stages are correctly *skipped* as unwinnable or uncontested,
which is the tool being honest rather than manufacturing a number.

## Fallout: `tools/battle_fuzz.py`

Sampling random five-cast teams to build the arena walked into a combination the stage
suites never produce and crashed the engine outright (`ValueError` in `engine.core._report`,
a passive whose damage trigger grants a status). That is a fuzzer's finding, found by
accident, so the fuzzer was built deliberately: random fields, real payloads, invariants
checked on every attack. It lives in `docs/BATTLE_ENGINE_PLAN.md` phase 9 because what it
tests is the engine and the wire, not the chooser — but it reuses this tool's team sampling
and runs all four choosers, so a bad move choice that only crashes on some field shows up
there too. Current sweep: 3,000 fights, 242k attacks, 0 findings — and it has since grown
a starvation invariant (a living unit that never acts with its gauge still blocked), which
is what catches a passive that removes a unit from the fight rather than corrupting a
payload. See `BATTLE_ENGINE_PLAN.md` phase 10.

## Where this can be wrong

- Status weights are guesses wherever magnitude is missing — two thirds of sites. They live
  in one table, and they will need tuning against real fights.
- Scoring `other`/`None` statuses at zero means a skill whose *only* effect is an
  unclassified status looks worthless and will never be chosen. That is the safe direction,
  but some authored skills will sit unused until the status compiler covers them.
- `threat` is a per-turn damage proxy. A pure support unit reads as low-threat and will be
  killed last, which is usually wrong. Revisit once the weights are calibrated.

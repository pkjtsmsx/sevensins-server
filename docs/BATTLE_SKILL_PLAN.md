# Battle skill-recreation plan

Goal: **every skill any unit or mob can use resolves through the effect engine** — no
skill silently falls back to plain damage. The battle is fully server-authoritative
(the client holds zero mechanics), so this covers **mob and boss attacks too**, not just
player casts.

This is the roadmap for that. Status as of 2026-08-10; update the metrics as they move.

## Metric: measure coverage over CHAR-REFERENCED skills, not all rows

The raw "% of all 12,893 skill rows complete" (31%) is the **wrong** number — that
denominator is mostly rank variants and dev orphans. The number that matters:

- **char-referenced skills (player + mob/boss): 909 / 2009 = 45% complete** ← track this
- player-castable alone: 217 / 606 = 35%
- (all rows incl. ranks/orphans: 4070 / 12893 = 31%)

Rank variants largely come **free**: a rank row shares its group's text and differs only
in a coefficient number, so the rules that parse the group base parse every rank.

### Untranslated == unused (not a wall)

We ARE parsing the genuine EN 2.2.7 pack (`design_pack_ff7a…155.ab`). The ~138 skills whose
`_note1_en` is Chinese are Chinese in *every* column (en/jp/sc/base) — untranslated in the
shipped EN build. **0 are player-castable; 114/138 are orphaned (no char references them);
the rest are boss-only.** Drop them from the denominator. There is no "missing EN pack".

## Architecture — how this is built (and stays decoupled)

**Data-driven, not hand-authored.** We do NOT write 2009 skills by hand. Each skill's
description text is parsed once into an ordered list of effect *ops*; the engine executes
those ops at runtime. This is what makes "all skills" tractable.

Pipeline, separated by concern (already NOT a monolith):

```
tools/parse_skills.py ─┐                         (offline)
extract_status_catalog ├─► battle_data/*.json ──► battle_effects (engine) ──► battle.py (flow/wire)
build_status_icons.py ─┘   skill_effects.json     ops -> runtime effects     turn loop, DamageInfo
                           status_catalog.json
                           status_icons.json
```

### Target structure (to keep it decoupled to 100%)

Two files WILL bloat as we add rules; get ahead of them:

- **Engine = op registry, not an `if/elif` chain.** `battle_effects` becomes a package:
  `core.py` (Status model, loaders, apply_status/immunity, stat helpers, resolve_targets,
  `execute_skill`/`run_phase`) + `ops.py` (a `dict` op-name → handler; each op family a
  small function). Adding an op = registering a handler, not editing the spine. Public API
  is re-exported so `import battle_effects as fx` and every `fx.*` call site is unchanged
  (same pattern as the player_state split).
- **Parser = a package.** `tools/skillparse/{targets,triggers,conditions,ops_*}.py` + a
  thin driver, so a new pattern lands in a focused module.
- **Overrides escape hatch.** `battle_data/skill_overrides.json`, hand-authored, for the
  residual that never parses cleanly (vague/magnitude-less text, unique one-offs). Bounded
  to dozens, merged over the parsed ops.

### Two non-negotiables

1. **Committed regression harness.** `complete` == trusted == runs live. A checked-in test
   must fuzz every `complete` skill through every trigger phase (0 crashes) on each
   regeneration, plus assert the 8 starter casts parse unchanged. Recreating all skills
   without this is how a live battle crashes on some obscure mob.
2. **Parser/engine lock-step.** Every op the parser can emit MUST have an engine handler,
   or a `complete` skill silently does nothing / deals 1. The registry makes this an
   assertion: every emitted op name has a registered handler.

## Phased plan (ROI order)

**Current status (2026-08-10): Phase 0 DONE. Next up = Phase 1.**
- Engine is the `battle_effects/` package (registry + core + ops); parser is the
  `skillparse/` package. `server/test_battle_effects.py` is the committed regression
  harness (lock-step + no-crash + starters + full battles) — run it after any change.

| Phase | Work | Effort | Coverage (char-ref) |
|---|---|---|---|
| **0. Architecture prep** ✅ | Op-registry engine package; parser package; committed regression harness. | S | 45% → 45% |
| **1. Cheap pattern sweep** | Concentrated leftovers: "ability-unlock: base `<stat>` +N" (~27+), "recovers N% HP after action", a handful more. Regex + existing ops. | S | 45% → ~52% |
| **2. Condition system** ⭐ | Condition grammar in parser + evaluator in engine + fire the deferred `conditional` effects in the turn loop. Unblocks ~195 incomplete player skills AND fixes ~667 already-`complete` skills whose conditional branch never fires. Highest value: coverage + correctness. | **L** | ~52% → ~68% |
| **3. Mechanics depth** | Make parsed skills actually act: DoT/HoT ticking, shield absorption, un-enforced status flags (heal_block, ability_seal, forced_target…). | M | (correctness) |
| **4. Long-tail grind** | The ~165 flat one-off shapes, added opportunistically. Deep diminishing returns (top 20 shapes ≈ 57 skills). | M, spread | ~68% → ~90% |
| **5. Hand-authored overrides** | `skill_overrides.json` for the un-parseable residual. Bounded manual work. | M | ~90% → 100% |

Phases 0–2 are the bulk of the value; 3–5 are a lower-intensity tail toward 100%.

## Key facts to carry forward

- Op vocabulary today: `damage` (pct_atk / pct_def / pct_target_maxhp, times), `apply_status`,
  `heal` (pct_maxhp), `cleanse`, `shield`, `stat_mod` (ATK/DEF/SPD via status, HP direct,
  unit pct|flat, CRIT skipped), `move_gauge` (signed pct), `skill_cd` (signed delta),
  `immunity` (status | "all" | "CrowdControl"), `extend_status`.
- Triggers: `on_use`/`before_action`/`after_action`/`after_attack` fire immediately;
  `battle_start`/`on_counter` are event-driven, fired via `run_phase` from a unit's
  `_type==4` PASSIVE skills; `conditional` is parsed but **not yet fired** (Phase 2).
- Wire: status icons ride `DamageInfo.status` = `[[order, skillID, round], …]` where skillID
  is a `_type==6` STATUS skill (see `status_icons.json`, `build_status_icons.py`).
- See memory `sevensins-battle` Steps 1–6 for the full reverse-engineering trail.

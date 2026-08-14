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

**Current status (2026-08-11): Phases 0–3 DONE. Next up = Phase 4 (long-tail grind).**
- Engine is the `battle_effects/` package (registry + core + ops + conditions); parser is
  the `skillparse/` package (+ `conditions.py` grammar). `server/test_battle_effects.py`
  is the committed regression harness (lock-step for ops AND condition kinds + no-crash +
  starters + evaluator semantics + mechanics-depth checks + full battles) — run it after
  any change.
- Char-referenced coverage stays **1115/2009 = 55%** (Phase 3 is a correctness pass, not
  a parser-coverage one — it makes already-`complete` skills' statuses actually DO
  something instead of sitting on a unit as an inert icon-and-stat-mod).
- Phase 2 landed the condition system: an effect may carry `{"when": cond}` and the
  engine evaluates the gate live (after the plain hits, so kill-gates see the outcome).
  Cond kinds: hp, hp_vs, status (incl. `_buff/_debuff/_dot/_hot` classes + stacks),
  cast_type (char `_job` 2/3/4 = STR/AGI/TEC, per CommonUtil.GetJobUseText →
  GetText(job+12099)), crit (dormant: no crit model yet), kill, turn_parity, turn_cmp,
  alive, all/any. `battle.py` passes `env={"turn": round}`; `Unit.job` carries the class.
  (The projected ~68% coverage didn't materialize because a conditional clause now counts
  ONLY if its gate parses — that strictness reclassified ~600 previously-complete-but-
  wrong skills as honest work; the ~2,486 gated effects that DO parse now actually fire,
  which was the real Phase 2 value.)
- Phase 3 landed mechanics depth: DoT/HoT ticking at the start of the affected unit's own
  turn, Shield absorption ahead of HP loss, and enforcement for six previously-inert
  catalog flags (heal_block, ability_seal, forced_target, confused_targeting,
  cd_reduction_block, immobilize/skip_action). See "Mechanics depth" below for the
  mechanism each one uses.

| Phase | Work | Effort | Coverage (char-ref) |
|---|---|---|---|
| **0. Architecture prep** ✅ | Op-registry engine package; parser package; committed regression harness. | S | 45% → 45% |
| **1. Cheap pattern sweep** ✅ | "% MAX HP as damage [+ chance status]" and "ability-unlock: Base `<stat>` +N". Both use existing ops. (Deferred: "recover HP by %ATK" — heal-targeting ambiguity.) | S | 45% → **50%** |
| **2. Condition system** ✅ | Condition grammar (`skillparse/conditions.py`) + evaluator (`battle_effects/conditions.py`) + gated firing inside `execute_skill`/`run_phase`. ~2,486 effects now carry machine-readable gates that actually fire. Also: `cleanse_class`, stacks, pursuit-damage, heal-by-% matchers. | L | 50% → **55%** (honest gates; see status note) |
| **3. Mechanics depth** ✅ | Make parsed skills actually act: DoT/HoT ticking, shield absorption, un-enforced status flags (heal_block, ability_seal, forced_target…). | M | (correctness — coverage % unchanged) |
| **4. Long-tail grind** | The ~165 flat one-off shapes, added opportunistically. Deep diminishing returns (top 20 shapes ≈ 57 skills). | M, spread | ~68% → ~90% |
| **5. Hand-authored overrides** | `skill_overrides.json` for the un-parseable residual. Bounded manual work. | M | ~90% → 100% |

Phases 0–3 are the bulk of the value; 4–5 are a lower-intensity tail toward 100%.

## Mechanics depth (Phase 3) — how each flag/mechanic is enforced

- **DoT/HoT ticking** — `fx.tick_dot_hot(unit)`, called from `Battle.end_turn()`'s new
  `_start_of_turn()` on whoever is now at the front of the queue (every catalog DoT/HoT
  reads "when a turn starts..."). DoT ignores the target's DEF and any Shield (a flat
  %ATK tick, per the text) but still applies `damage_taken_multiplier` (Stun's +25%,
  etc). The %ATK is **snapshotted at apply time** as `Status.dot_atk` — a DoT still hits
  for the inflicter's power even after their own buffs expire — via `apply_status(...,
  source=<inflicter>)`. HoT respects `heal_block`.
- **Shield absorption** — `Status.shield_hp` (per-instance, not shared catalog data)
  drains before HP does, via `fx.absorb_shield(unit, dmg)` called from the `damage` op.
  Sized either from the `shield` op's explicit flat amount ("open a 7000 Shield") or from
  the catalog's `shield_pct_atk` (the generic "Shield" status, 75% of the source's ATK)
  computed through `apply_status(..., source=...)`. Multiple stacked shields drain oldest
  first. Re-applying a shield resets its pool (a new shield, not a top-up).
- **heal_block** — the `heal` op and HoT ticks both skip a target carrying the flag
  (`fx.has_flag(u, "heal_block")`).
- **cd_reduction_block** — the `skill_cd` op ignores negative deltas (CD *reductions*
  only; the unit can still be delayed) for a flagged target.
- **ability_seal** (Silence/Skill Seal/...) — `Battle.usable_slots()` returns only the
  basic (`[0]`) for a sealed unit; `judge_args()` locks buttons 2/3 the same way so the
  UI matches; `titan_server.py`'s `REQ_ATTACK` handler re-checks `usable_slots()` server-
  side so a raw/replayed request can't skip the button lock.
- **immobilize / skip_action** (Stun/Freeze/Daze/Petrify/...) — `Battle._start_of_turn()`
  auto-skips a crowd-controlled unit's entire turn (ticks its cooldowns/statuses down and
  rotates past it) instead of leaving the client waiting on an attack that never comes.
  Recurses (bounded by unit count) so an all-CC'd lineup still resolves.
- **forced_target** (Taunt) and **confused_targeting** (Charm/Confuse) — both override
  the ATTACKER's chosen target in `Battle._forced_target()`, called from
  `attack_cmd_json` so it applies whether the target was auto-picked (enemy AI /
  auto-battle) or tapped by the player — a client can't route around its own status by
  picking someone else in the request. `Status.taunt_source` (the inflicter's order,
  set at apply time) drives the Taunt redirect; Confuse/Charm redirect onto the
  attacker's own team and take priority (losing control of your target trumps being
  drawn to a specific enemy).
- **unremovable** — `cleanse`/`cleanse_class` now both skip a status carrying this flag;
  previously they stripped by name/class regardless, silently violating every "(excluding
  unremovable buffs)" skill caveat.
- Deferred (still inert): `revive_block` (no revive op exists yet — nothing to gate),
  `cleanse_self`'s "removes all debuffs" clause (already reachable through ordinary
  `cleanse_class` parsing when the skill text says so explicitly), `move_gauge_mod`
  (informational only; magnitudes already ride explicit `move_gauge` ops).

## Key facts to carry forward

- Op vocabulary today: `damage` (pct_atk / pct_def / pct_target_maxhp, times), `apply_status`
  (+ optional `stacks`), `heal` (pct_maxhp | pct_caster_hp), `cleanse`, `cleanse_class`
  (`_buff/_debuff/_dot/_hot`), `shield`, `stat_mod` (ATK/DEF/SPD via status, HP direct,
  unit pct|flat, CRIT skipped), `move_gauge` (signed pct), `skill_cd` (signed delta),
  `immunity` (status | "all" | "CrowdControl"), `extend_status`.
- Triggers: `on_use`/`before_action`/`after_action`/`after_attack` fire immediately;
  `battle_start`/`on_counter` are event-driven, fired via `run_phase` from a unit's
  `_type==4` PASSIVE skills. An effect with `{"when": cond}` fires only if the gate
  evaluates True (after the ungated effects, same ctx). A legacy `conditional` trigger
  without `when` stays deferred/unfired.
- Wire: status icons ride `DamageInfo.status` = `[[order, skillID, round], …]` where skillID
  is a `_type==6` STATUS skill (see `status_icons.json`, `build_status_icons.py`).
- **HOW MANY UNITS A SKILL HITS IS DESIGN DATA, NOT PROSE.** The row states it, and the
  skill panel already renders it as the "Range 2 enemies" line:

      DesignSkillRow.GetTargetGroup()  =  _target / 100      (RVA 0x1AACCA4)
      DesignSkillRow.GetTargetRange()  =  _target % 100      (RVA 0x1AACCC4)
      the panel label                  =  text 23000 + _target

  group 0 = enemies, 1 = allies. Range: 1 → 1, **2 → ALL**, 6 → 2, 7 → 3, 8 → 4,
  9–12 → 1–4 RANDOM, 13/14 → highest/lowest HP, 15/16 → DEF, 17/18 → ATK, 19 → SPD.

  Do not try to read the count out of the description — it cannot be done. Phantom Star
  Ring III (2081113) reads "inflicts Confuse on the target" while its row says 2
  enemies. By this field 5681 enemy-group skills are multi-target (2454 at 2, 1998 at 3,
  831 at ALL); before reading it, 309 were spreading and everything else hit one unit,
  `complete` skills included. `Ctx.targets()` prefers the row whenever the parsed token
  is the singular default, and leaves an explicit `all_enemies`/`self`/`highest_hp_enemy`
  alone as the more specific answer.

  **Ally-group ranges (`_target` 1xx) are still prose-driven** — heals and buffs on
  "3 allies" have the same gap and nobody has reported it yet.

- The client holds NO mechanics: it draws whatever `DamageInfo` rows arrive and asks no
  questions. It does hold all the DESIGN data (skill ranges/CDs/text, char stats, stage
  layout, item routing), which is why a panel can show the right number over a wrong
  outcome. When in-game display and behaviour disagree, suspect a design field the
  server has not read yet before suspecting the client.
- See memory `sevensins-battle` Steps 1–6 for the full reverse-engineering trail.

# Skills & battle engine — where it stands, and what's next

2026-08-26, written at the end of the audit-and-fix pass. Companion to
`BATTLE_CLIENT_CONTRACT.md` (what the client does with our payloads, now with the
IDA addendum) and `BATTLE_ENGINE_PLAN.md` (how the engine got here). This file is the
pick-up-where-we-left-off document: current state with numbers, then the work items in
the order they're worth doing, each with why, where, and how to prove it.

## The one-sentence diagnosis

Everything **structural** comes from the game's own opcode script (`_action`/`_actID`)
and is essentially complete; everything **numeric and conditional** comes from parsing
prose, and the fix that carried this whole pass was reading the Chinese (`_note1`)
first — the English renames statuses mid-sentence, mistakes chances for amounts, and
flips cooldown signs.

## State of the artifact (cast skills, 64 files, 3,147 specs, 12,777 effects)

| piece | state |
|---|---|
| damage coefficients | 2,758/2,758 |
| riders / gauge / CD / heal | 366/366 · 823/823 · 351/353 · 336/359 |
| status magnitudes (kinds that need one) | 2,608/3,512 (74%) |
| status durations known | 4,445/6,205 (72%) |
| shields sized (and absorbing at all) | 125/151 — `shield_hp` was never set before 904a24d |
| conditions gating (holds / HP / round / crit / kill / stacks) | 1,844 effects |
| status applications rolling their STATED odds | 1,214 (1,181 from the Chinese) |
| still firing unconditionally | see item 2 — 545 fewer than at the last count |
| specs with undecoded opcodes (1/3/7/118…) | 293 — op 6 decoded; 107 distinct mechanics behind the number |

Every number above is recomputed by the census in `test_skill_specs.py` (ratchets) and
the runtime audit (execute all 2,364 active cast skills, tally `out.skipped`). Run the
audit ad hoc — it is ten lines against `ai_arena.stage_battle`; consider promoting it
to `tools/skill_audit.py`.

## The standard verification loop (use it for every item below)

1. `python3 tools/compile_skills.py` — then diff the artifact against a copy and
   **blind-sample ~12 changed effects against the Chinese prose**. Counts lie;
   samples caught every parser trap this pass (threshold-as-magnitude, 相當於
   matching 當, the predecessor-clause coefficient, chance-as-heal).
2. `python3 tools/run_tests.py` (lints first; 32 suites).
3. `tools/battle_fuzz.py --fights 2000` after anything touching engine or artifact.
4. adb snapshot to the phone (recipe in the memory file / CLAUDE.md §9) and play the
   named check. The device is the gate; nothing here counts as done without it.

---

## Work items, in order

### 1. Status-application chance from prose  ✅ **done 2026-08-29**

`status_chance` in compile_skills reads the odds out of the fragment that grants the
status; `_status_event` rolls them through `formula.effect_lands`. 1,214 sites, 1,181
of them answered by the Chinese. Pre-fix, a stated 10% landed **73.5%** of the time and
a stated 90% on an op-112 row landed **100%**; `test_engine.check_stated_chance`
measures the rate over 2,000 casts and fails on the old gate.

Three things it turned up that were not in the plan:

* **112 is not "guaranteed".** 772 op-112 sites state odds in the very fragment that
  grants the status, so `chance_pct` is emitted for both opcodes and the contract doc's
  §6.1.1 now records the correction. What the 112/113 split *does* encode is open.
* **The client never reads the opcode script**, so it cannot arbitrate: `DesignSkillRow`
  exposes no Action property, and `AddSkillScripts` (0x1aacb00) walks `_action` only for
  opcode 4 — which occurs zero times in this pack.
* **The English states a different probability from the Chinese on 40 sites** (Freeze at
  40% in English, 25% in the original). Chances now join recipients and cooldown signs
  on the §3 list. The reader is ZH-first as a result, and the ratchet in
  `test_skill_specs` pins ≥95% Chinese provenance so an English-first regression fails.

Still English-only, and the next thing to fix in this area: `annotate_passive` reads
`_note1_en` and nothing else. It no longer overwrites a Chinese-sourced chance, but 23
passive chances and every passive trigger, recipient and magnitude still come from the
translation.

**Not covered:** 每段傷害都有50%固定機率 is a roll PER SWING and the engine applies
statuses once per cast, so a per-swing chance comes out weaker than retail on a
multi-hit skill. Recorded in `status_chance`; needs the swing loop, not the parser.

### 2. The remaining condition shapes  *(five of them done 2026-08-29)*

`_condition_met` is now **tri-state**: True, False, or None for "this engine cannot
answer that here". None takes CONDITIONAL_POLICY, the same treatment an unparsed
condition gets. That distinction is the load-bearing part — returning False for an
unanswerable gate deletes the effect from the game, which is a worse bug than the
unconditional firing the gate was added to stop, and two shapes below were doing
exactly that before this pass.

Done, with the effects each one gained (`test_engine.check_condition_shapes` proves
every one of them both opens AND shuts):

| shape | effects | how it is answered |
|---|---|---|
| round parity / comparison (奇數・偶數回合, 總回合數不高於N) | 254 | `round_no` kwarg on `execute`, passed by `bridge` from `battle.round`. The AI's dry runs and the fuzzer pass nothing, so it reads unevaluatable there rather than guessing round 1 |
| this-cast crit (若本次攻擊暴擊) | 149 | `ctx["strikes"]` — the swing loop finishes before the non-damage effects run, so `detail["crit"]` is already final |
| stack count (若…擁有N層X, 達到N層, 至少N層) | 193 | `requires.count` vs live `Active.stacks`; fixes the approximation shipped in af911ee, where "5 stacks of Reload" passed on the first stack |
| this-cast kill (若擊倒/未擊倒敵人) | 55 | `ctx["targets"]` — HP is mutated by the swing loop, so `not alive` is settled |
| unresolvable gate names | 108 | now unevaluatable instead of a gate that could never open |

Still to do:

| shape | ~count | engine needs |
|---|---|---|
| target attribute (目標為力/速/技屬性, 弱點屬性) | 37 | `requires_attr: {on, attr \| "weak"}`; units carry `attribute`, `formula.advantage` answers 弱點 |
| named ally on field (若「傲慢之魔王 路西法」在場) | 34 | `requires_ally: char_id`; map the quoted name via `char._name`. Also gates several passives |
| stat-down / category gates (能力下降, 可解除, 不可堆疊能力下降) | 83 | `requires_category` — 能力下降 is "a stat-down", a CATEGORY, not a status name |
| leftover/compound (~350 fragments) | — | triage: some are two conditions ANDed, some are 次-counts, some are OR-of-two-statuses (金剛或超 •金剛) |

**Stack caps from prose — done 2026-08-29.** `compile_statuses.prose_caps` reads
最多可疊加N次 / "stacks up to N times" off the glossary lines: 51 statuses gained a cap
(46 from the Chinese, 5 from English where the Chinese is silent), 29 existing `(N)`
suffixes were confirmed by prose, 0 disagreed. Count gates reachable: **31 → 120 of
193**. Two rules that matter: a **split vote is not resolved by majority** — Prime Crown
"accumulates up to 6 / 9 / 12 times" by skill level, so the number belongs to the
(skill, status) pair and the status keeps no cap; and the English join uses the bare
name, not `norm_name`, which strips `(SP)` and was handing `Spirit(SP)` the base
Spirit's cap. `test_engine.check_prose_stack_caps` proves Wrath reaches 5 and stops.

Still unreachable, and deliberately so: **Reload (50 gates), Charge, Footing, 疲勞**
state no ceiling in either language — they are counters ("gain 1 per action, at N
stacks X fires"). Their id block says *stackable* from a column, but so do 18 stat-mods
with no stated cap (DEF UP(SP), Berserk), and letting "stackable" mean unbounded would
buff those on no evidence. Those gates stay unevaluatable. If it ever matters, the fix
is a per-status curated cap for the counters, not a rule.

After each shape: the gated-effect count in `test_skill_specs.py` goes UP and gets
pinned so it cannot silently fall back.

### 3. Follow-ups named in prose (opcode 118 and friends)

118 = chance-triggered follow-up, but its operand is 0 — the sub-skill is named in
text: 使用「渦流穿」進行追擊, 10%固定機率觸發「死寂」1次. Resolve the quoted name
against the CAST'S OWN skill set (`char._skills` + `_group` levels — the sub-skill is
always in-kit), emit `{"op": "follow_up", "skill": id, "chance_pct": N, "requires":…}`.
Converts 47 runtime skips (and gates some of the 320 ungated). The engine follow_up
path needs a `chance_pct` roll — same three lines as modify_gauge got.

### 4. Opcodes 3, 7 (and 11 / 119 / 121)  *(research; op 6 is done)*

**Op 6 decoded 2026-08-29** — an amount sized off the caster's own HP pool (current HP
in 95 rows, max HP in 25), dealt as bonus damage or a heal; contract doc §6.3.0. 114 of
125 rows, 12 skill groups on 6 casts.

Sized before deciding, by the lift test (how much more often an opcode's rows mention a
concept than the corpus does): the 338 "undecoded" specs are **107 distinct mechanics**
once level variants collapse, no cast skill is *entirely* undecoded, and two of the
opcodes are not decode problems at all — op 1 (11 groups) has known semantics and no
number in either language (item 5), op 118 (19 groups) is item 3. Of the rest:
**op 3** (26 groups, 4 casts) does NOT show the "trigger named effect" concept the old
reading claimed — its only signal is 護盾 at 4.4×, so that reading is unverified;
**op 7** (30 groups, 7 casts) has *empty prose on every solo row* — nothing to cluster;
**op 119** (6 groups, all passives) is 技能加速 at 3.8× and its one solo row reads
戰鬥開始時，使技能加速2回合 — cheapest remaining. Contract doc §opcodes: 3 (248 rows) was
read as "will trigger «named» effect" — a cross-skill hook, probably the trigger half of
what 118 fires; 7 (320) is mixed, several rows pair with Deathblow. Approach that worked before: dump every
(opcode, operand, ZH prose) triple, cluster by prose shape. **Do not go looking for a
client handler** — item 1 established there is none for any opcode (contract §6.1.1),
so prose clustering is the only tool here. Do NOT guess semantics into the engine — a
reported skip beats a wrong effect.

### 5. The ~900 statuses that need a magnitude nobody states

Prose is exhausted (both languages). Options, in preference order: (a) a **curated**
per-status table — id, magnitude, duration, the prose line it was read from — like the
contribution's `status_overrides.json` idea but hand-checked (machine extraction was
rejected for good reason: thresholds and second clauses); (b) leave inert — the honest
default. Never a corpus average for these; that was already rejected once.

### 6. Hand-verify the status registry's `kind`

`statuses.json` kinds are prose-derived and they gate everything: immunity blocking,
stacking, DoT ticks, shield sizing. `Major Demerit` filed as `heal` nearly became a
90%-max-HP-per-turn heal. Priority subset: the ~2,600 statuses that now HAVE a
magnitude (a wrong kind there does damage; a wrong kind on an inert status doesn't).
Mark verified rows so the compiler can ratchet on it.

### 7. Boss rules from `_hint_id`  *(new subsystem, event/raid content)*

2,242 stages in books 7 and 17 attach explicit rules as tips text: BOSS immune to
immobilisation at battle start, BOSS cleanses CC/stat-downs before each turn, enrage
countdown (狂暴: stacking ATK/DEF/SPD per turn + self-heal), party-wide EFF-hit +100%,
HP loss for holding Daze/Taunt at boss turn. None implemented. This is where "hard
boss" lives in this game (Guild Weekly Gabriel is chain-freezable BY DESIGN — her
stage has `_hint_id 0`; do not "fix" her). Design: parse the tips into a per-stage
rule list; hook battle start / boss turn-start. This is the biggest gameplay feature
on the list.

### 8. Mob damage coefficients (979 missing)

Untranslated rows with no numeric text in ANY language. First: measure how often live
stages actually invoke these (fuzzer coverage per skill id) — it may be rare. If it
matters: a per-tier default coefficient as a **named design choice** in settings
(1.0 = "the retail value is unknown"; document loudly), or leave the skip.

### 9. Smaller / polish

- `heal` remaining 23 blanks + `cd` 2 — read the leftover clauses, probably odd phrasings.
- Unresolved gate names (16) — 能力下降 ("a stat-down") is a CATEGORY condition, not a
  status name: needs `requires_category`.
- Passive icons: `picons`/`pskill_id` are always empty from us; the client has
  `UICharStatusMgr.ShowPassive`. Cosmetic, cheap to wire from passive fires.
- `Miss`/`Forbid` floating texts (10098/10099) — find what triggers them in retail
  semantics (dodge? forbidden action?) before using.
- Promote the runtime audit to `tools/skill_audit.py` with a ratchet file.

## Standing cautions

- **Blind-sample every parser change.** Every regression this pass was caught by
  reading 12 random changed effects against the prose, never by the counts.
- The artifact is untracked but SHIPPED (CLAUDE.md §10): recompile after pulling, and
  remember `build_hostapp_update.py` reads the working tree.
- Fuzz proves not-crashing, not correctness (§13). The phone is the gate (§8).
- Chinese wins on any disagreement (§3) — this is now true of *numbers*, not just
  recipients; keep new parsers ZH-first, EN fallback, provenance recorded.

## The clause ledger

Vocabulary, since the compiler now has a concept worth naming.

A **clause** is one fragment of `_note1`, cut on `_ZH_SPLIT` (`，。；、\n`). Its
**identity** is its index in the note: every reader splits the same way, so clause 2
means the same fragment to all of them. `_zh_clauses_with` returns it.

A clause is **claimed** when an opcode has consumed it. `effects()` fills a `claimed`
set with `(kind, index)` as its queues pop — one clause per opcode, in order. The
`uncovered_*` pass then emits exactly the clauses nothing claimed. Keyed on kind as
well as index because `_ZH_SPLIT` does not cut on 並, so one fragment can state two
families (224 skills; 復活…並恢復其50%體力 is seen by both the revive and heal readers).

The rule, and what a compile run prints a line about: **every clause is paid exactly
once.** Before the ledger the two sides reconciled by comparing the values they had
produced, which is wrong in both directions and was wrong in shipped data — one clause
paid twice when the readers spelled a heal differently, two clauses paid once when a
note states the same number deliberately.

### What is still off the ledger

Both are the same shape: readers that `re.search` the whole note, so they have no clause
to claim with. Moving them onto `_zh_clauses_with` is one piece of work.

- **`zh_rider` / `zh_hp_rider`.** Until they move, `drop_rider_duplicates` folds a heal
  a rider already pays, by value. That is the last of the value-keyed dedupe and it is
  load-bearing: it is the Gate of Judgement fix, verified on a device. The same
  double-read affects `bonus_damage` riders and is not handled.
- **`uncovered_removes`.** Bails entirely if any `remove_status` exists, because
  `OP_REMOVE` carries a status id or a category and never a clause index. Matching the
  two sides needs a category comparison, not a queue. **726 of the 2,177 skills with an
  opcode remove also state a cleanse in prose that this drops** — e.g. 153002001 clears
  the target's 持續回復 by opcode and states 行動後清除敵方攻擊力最高2名的能力上升狀態,
  which never compiles.

### Measured gaps the ledger makes visible

The compile line reports these every run; they are not fixed, they are now counted.

- **111 cd clauses** no opcode claimed. There is no `uncovered_cd`, so they compile as
  nothing. Emitting them is a behaviour change and wants its own commit.
- **788 value conflicts** (heal 252, gauge 416, cd 120, revive 25) where an opcode and
  the clause it claimed state different numbers. The opcode wins, per CLAUDE.md §1.
  Nobody has read a sample of these.

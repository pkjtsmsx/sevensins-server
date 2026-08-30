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
| conditions gating (holds / HP) | 344 + 33; follow-ups gated 79/399 |
| status applications rolling their STATED odds | 1,214 (1,181 from the Chinese) |
| still firing unconditionally | 655 status applications, 320 follow-ups |
| specs with undecoded opcodes (1/3/6/7/118) | 338 (~290 runtime skips) |

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

### 2. The remaining condition shapes  *(the big one — 655 + 320 effects)*

Engine gains one capability per shape; the ZH parser (`_zh_parse_condition`) gains a
branch per shape. Sizes from the census:

| shape | ~count | engine needs | note |
|---|---|---|---|
| turn number / parity (奇數/偶數回合, 總回合數不高於N) | 93 | the ROUND at execute time — pass `battle.round` into `execute()` (new kwarg or a context obj); `requires_turn: {parity/cmp,n}` | Battle owns `round`; threading it through `attack_cmd_json → bridge → execute` is mechanical |
| target attribute (目標為力/速/技屬性, 弱點屬性) | 93 | `requires_attr: {on, attr | "weak"}`; units carry `attribute`, `formula.advantage` answers 弱點 | cheap |
| this-hit-crit (若本次攻擊暴擊) | 29 | statuses resolve after strikes in the same `execute` — check `any(s.detail.get("crit") for s in out.strikes)` at `_status_event` time; needs event ordering, strikes already precede statuses | verify ordering first |
| target killed / not killed (若擊倒/未擊倒) | 44 | same pattern via `Strike.died` | |
| stack count (若…擁有N層X) | 20 + fixes the approximation shipped in af911ee | `requires.count`; `_holds_status` compares `Active.stacks` | the parser already sees `\d+層`, it just drops it |
| named ally on field (若「傲慢之魔王 路西法」在場) | 34 | `requires_ally: char_id`; map the quoted name via `char._name` | also gates several passives |
| leftover/compound (~130) | — | triage: print every unparsed 若-fragment, classify, decide | some are two conditions ANDed |

After each shape: flattened-count ratchet in `test_skill_specs.py` goes DOWN and gets
pinned so it cannot silently climb back.

### 3. Follow-ups named in prose (opcode 118 and friends)

118 = chance-triggered follow-up, but its operand is 0 — the sub-skill is named in
text: 使用「渦流穿」進行追擊, 10%固定機率觸發「死寂」1次. Resolve the quoted name
against the CAST'S OWN skill set (`char._skills` + `_group` levels — the sub-skill is
always in-kit), emit `{"op": "follow_up", "skill": id, "chance_pct": N, "requires":…}`.
Converts 47 runtime skips (and gates some of the 320 ungated). The engine follow_up
path needs a `chance_pct` roll — same three lines as modify_gauge got.

### 4. Opcodes 3, 6, 7  *(research, then implement what falls out)*

Contract doc §opcodes: 3 (248 rows) is "will trigger «named» effect" — a cross-skill
hook, probably the trigger half of what 118 fires; 7 (320) is mixed, several rows pair
with Deathblow; 6 (46 on casts) unknown. Approach that worked before: dump every
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

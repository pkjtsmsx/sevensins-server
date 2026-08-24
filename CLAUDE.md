# How to work in this repo

A private server for Seven Sins (EN 2.2.7), plus the tooling that reverse-engineers the
game's own data to drive it. The client is unmodified except for a few documented patches;
almost everything we do is server-side, and almost every question is answerable from data
the game already ships.

`docs/` holds the subsystem knowledge — read the relevant one before touching a subsystem,
they are written to be read. This file is about *how to operate*, not what exists.

---

## 1. The evidence hierarchy

Rank every claim by where it came from, and say which tier you are on:

1. **Live footage** of the real game. The strongest evidence there is. `STAGE_DROPS` and
   the karma payout tables are calibrated on it.
2. **The design pack** (`design_cache/`, read via `design_data`). Stage rows, item rows,
   `_rating_datas`, dmap links. This is the game's own data and it is usually decisive.
3. **The client binary** (IDA). Use it for anything about what the client *does* with a
   payload — panel behaviour, which fields it reads, what throws. `docs/` cites addresses;
   add yours when you find them.
4. **Item and skill prose** (`_note1*`, `_itemName*`). Frequently states mechanics outright
   — the Lading Bills literally list their own drop stages.
5. **Inference.** Label it as such, out loud, every time.

**Reconstruction and design choice are different things and must never be blurred.** If
the game did X, reproducing X is a reconstruction. If the pack is silent and we picked
something, that is a design choice: name it in a comment, give it one named constant or a
`settings.RATES` knob, and make 1.0 mean "what retail paid". A reader must be able to tell
which they are looking at a year later.

## 2. Verify claims — contributions, users' reports, and your own

Users contribute code and reports. Both are frequently right about the *symptom* and wrong
about the *cause* or the *fix*. Check the claim against the data before acting on it.

Recent examples, all from one session:

- A contributed `battle.py` argued the Evolution Abyss should scale rewards with depth
  because "the deep run is pure waste". The pack disagreed: stamina is **1 on every rung**,
  so nothing is wasted, and the Trainers Gym next door *does* scale — proving the designers
  scaled where they wanted and left this flat. Rejected as a fidelity fix; shipped instead
  as an off-by-default knob.
- The same file's Trainers Gym rewrite rested on a real observation (the tiers are
  1,725–60,000 xp, so a count ladder jumps 2.5× at a tier switch — verified true) but its
  implementation broke a documented wire invariant and clobbered three stages that don't
  pay trainers at all.
- A contributed `soulbook.py` claimed login reported Soul Link rank 0 while battle applied
  the real bonus. Every claim checked out — five other call sites, the battle path, the
  5N/2N/35N table — so it was taken.

**Do the same to yourself.** In that session I called flat Abyss drops a defect (wrong —
it is faithful), said the coin items were nameless rows (wrong — I filtered on `_name_en`
where the form uses `_itemName_en`), and said the Gym rate was 6× low (wrong — that stage
is hand-recorded and `STAGE_DROPS` wins). Correct plainly, state what changes as a result,
move on.

## 3. The English is a translation, and it has real errors

`_note1` is the **original**; `_note1_en` is a translation. When they disagree, the
Chinese wins. This is not theoretical: Beelzebub's passive reads "inflict Headwind on all
**allies**" in English and 對**敵方**全體附加逆風 — "on all **enemies**" — in Chinese.
Believing the English gave her a self-inflicted move-gauge block and she took **zero turns
in a 62-attack fight** on a real device. A corpus sweep found 20 such disagreements.

Useful markers: 自身 self, 我方 allies, 敵方 enemies, 戰鬥開始時 at battle start,
行動前/行動後 before/after action, 受到傷害後 after taking damage.

## 4. One observation does not generalise

The most common bug class in this codebase is a single true observation applied to a whole
family:

- The Starshard Temple showed 2 candidates on a live panel → hardcoded 2 for all 41 floors
  → the 31 three-wave floors were a candidate short.
- The Transcend Corridor paid 3 pieces → one stack of 3 on all 80 stages, where the rule is
  one drop per wave.
- The Trainers Gym's rung 1 pays ★2 → the table says in as many words *do not extrapolate*.

When you learn something from one clear, ask what population you are about to apply it to
and whether anything else constrains it.

## 5. Invariants beat observations

A rule that holds game-wide is worth more than another data point, because it can be
audited across everything at once. "One drop per wave" — volunteered by the user — found
**111 stages** in two subsystems in a single sweep, including one nobody had reported.

When the user states a rule like that, turn it into a script over all 6,600 stages
immediately. Then turn it into a test.

## 6. Tests must be anchored to behaviour

A test that asserts a constant keeps passing while the behaviour rots. The Temple suite
asserted `STARSHARD_DROPS_PER_CLEAR == 2` — true, and useless, because the bug was that 2
was the wrong number for 31 floors. It now asserts one candidate per wave, per floor, read
from the pack's own wave count.

Where a fix changes behaviour, prove the test would have failed before it. For the engine's
`_report` crash the test asserts `fire()` really returns both row shapes, and the pre-fix
unpack was demonstrated to raise on the same rows.

**`tools/battle_fuzz.py` is the regression net** — thousands of randomised fights checking
wire invariants, state and save/restore. Run it after anything touching battle. When a bug
escapes it, add the invariant that would have caught it: a unit sitting out a whole fight
produced no exception and no malformed payload, so nothing noticed until a human watched a
phone. It now checks for that.

## 7. Know your blast radius, and attribute findings

Before shipping, ask how many things the change touches. Deriving passives from prose went
from 6 hand-written casts to **936 groups / 1,953 rules** — every fight in the game — and
that deserved saying out loud *before* the push, not after.

When the fuzzer reports something after a change, **A/B it**: disable your change, re-run
the same sweep, and see whether the finding survives. That is how two client-hanging
payloads were attributed to the passive work in minutes rather than argued about.

## 8. The device is the gate; desktop is not

Every defect in that session was found by the user playing on a phone. None were found by
2,000 fuzzed fights, 8 green suites, or any amount of desktop reasoning. Treat "tests pass"
as *necessary*, never as *sufficient*, and say so plainly rather than implying a build is
ready.

Do not publish a build whose headline change has never been played.

## 9. How changes ship

- `hostapp/server_files.json` is the single list of what goes on the phone, shared by
  Gradle and `tools/build_hostapp_update.py`. **A new server module must be added there**
  or the phone gets a server that cannot import.
- `python3 tools/build_hostapp_update.py` produces a deterministic zip; the sha256 is the
  version identity. It refuses to build if shipped code imports a module the zip lacks.
- Two delivery paths: `tools/publish_hostapp_update.py` (GitHub release — **outward-facing,
  ask first**), or push a snapshot directly over adb into
  `files/sevensins/server_update/<sha[:16]>/` and point `active.txt` at it. Prefer adb for
  work-in-progress; it reaches one device and publishes nothing.
- Restart the app process afterwards (Chaquopy caches imports), then **read
  `titan_server.log` in the active snapshot dir** to confirm a clean start.
- Old snapshots stay on disk and are one `active.txt` edit from a rollback. An escape-hatch
  env var is *not* a substitute for fixing the bug — the user was right to push back on
  that.

## 10. Comments and commits are the record

House style, and it is load-bearing: comments explain **why**, cite the evidence, and name
the bug that motivated the code. `battle.py` and `engine/` are full of "this used to do X
and here is the report that killed it" — that is deliberate, and it is why a stranger can
pick up a subsystem.

Commit messages are the design record. State what was wrong, what the evidence was, what
was decided and what was deliberately *not* done. Long is fine.

Do not delete a comment that records a trap because the code moved. Update it.

## 11. Scope, and saying what you did not do

Do what was asked. If the work turns out to be bigger than the ask, say so at the point you
notice, not in the summary afterwards. If you leave part of a task undone, name it.

When you report results, give the numbers and their limits. "3,000 fights, 0 findings" is
evidence of not-crashing; it is not evidence of being right, and the difference matters
when 935 derived rule sets have never been read by a human.

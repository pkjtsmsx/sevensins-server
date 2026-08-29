# Seven Sins — private server

A from-scratch server for the mobile game *Seven Sins* (EN 2.2.7), plus the tooling used
to work out what the client expects. The client is unmodified apart from a few documented
patches; essentially everything happens server-side.

It runs on a desktop for development, and on the phone itself — the `hostapp/` Android app
embeds the same Python server via Chaquopy, so the game and its server sit on one device
with no network in between.

## What this is not

- **It ships no game content.** No assets, no bundles, no design pack, no APK. Those are
  the game's, not this project's. You need your own copy of the game to get them, and
  nothing here will fetch them for you.
- **It is not affiliated with, endorsed by, or connected to** the developers or publishers
  of *Seven Sins*. All game names, data and trademarks belong to their owners.
- **It is not a live service.** There is no hosted instance, and this is not a tool for
  playing on, interfering with, or connecting to anyone's official servers.

The code in this repository is MIT-licensed (see `LICENSE`). That covers this project's own
source only — it says nothing about the game's data, which stays the property of its
owners. A handful of tracked files (`server/battle_data/skill_effects.json`,
`server/battle_data/status_catalog.json`, `docs/avg_decisions.json`) are *derived from* the
game's own tables and are here as working notes rather than redistributable content.

## Requirements

- **Python 3.12 or newer** (the phone build pins 3.12; desktop development runs ahead of
  it). The server is stdlib-only, deliberately — no `pip install` to run it.
  `UnityPy` is needed *only* to build the design cache from the game's bundles, and is
  imported lazily, so a warm cache never touches it.
- **A copy of the game's assets** in `server/patch_root/` (override with
  `SEVENSINS_PATCH_ROOT`). Roughly 2.5 GB extracted, and the single biggest thing standing
  between a fresh clone and a working server.
- For the phone build: the Android SDK, and JDK 17 — what Gradle 8.7 and the Android
  plugin expect.

## Quickstart

A fresh clone will not run a battle until the two compilers have been run. Their output
(`server/battle_data/skills/`, `statuses.json`, `cinematic_swings.json`) is generated from
the game's own tables and is deliberately untracked, so it does not exist yet:

```sh
python3 tools/compile_skills.py      # per-cast battle specs
python3 tools/compile_statuses.py    # status catalogue
python3 tools/run_tests.py           # pyflakes over server/, then every server/test_*.py
python3 server/titan_server.py       # listens on 22110
```

`tools/run_tests.py` is the gate rather than a shell loop over the suites: most of them
print nothing on success, so only its exit code tells you anything. For anything touching
battle, also run `python3 tools/battle_fuzz.py`, which throws thousands of randomised
fights at the engine checking wire invariants and save/restore.

The client reaches the server through its design pack's server row rather than a binary
patch — see `docs/GAME_SERVER.md`, and `tools/patch_server_row.py`.

### A note on exposure

This is a single-player local server. The game socket has **no authentication** — it was
never designed to face a network — so keep it on loopback and do not expose the port. The
phone build binds `127.0.0.1` for that reason; the desktop default is `0.0.0.0` only
because an emulator has to reach the host as `10.0.2.2`. `SEVENSINS_BIND` overrides either.

Player saves live in `server/accounts/` and are never committed. The test suites point
`SEVENSINS_ACCOUNTS` at their own temp directory so they cannot touch a real one.

## Layout

| Path | What's in it |
| --- | --- |
| `server/` | The server. `titan_server.py` is the wire/RPC layer, `player_state/` a core spine plus per-subsystem modules, `engine/` the battle engine, `battle.py` the battle flow. |
| `tools/` | Compilers, the test runner, the battle fuzzer, the hostapp update builder, and assorted RE utilities. |
| `hostapp/` | The Android host app that runs the server on the phone, including the in-app hot-updater. |
| `docs/` | Subsystem knowledge — protocol, battle contract, quests, economy, and more. Written to be read. |
| `re/` | Reverse-engineering notes: RPC maps, command tables, dispatch listings. |

## Working in this repo

`CLAUDE.md` is the contributor guide, and it is worth reading before changing anything. It
covers the evidence hierarchy the project ranks claims by (live footage > the game's own
design pack > the client binary > item/skill prose > inference), why the game's original
Chinese text beats its English translation whenever prose drives behaviour, and the habits
that keep this codebase honest — chiefly that a change is not proven by green tests. Nearly
every real defect here was found by playing the game on a phone, not on a desktop.

`docs/` holds the per-subsystem detail. Read the relevant one first; they exist so a
stranger can pick up a subsystem without excavating it.

## Status

Playable end to end: login and the full state sync, campaign stages and battle, gacha and
the shops, gear, runes, soul mirrors, bloodpacts, quests and daily missions, mail, the
login-bonus ladder, guild, and story cutscene branching. The battle engine derives its
rules from the game's own skill prose rather than hand-written cases.

Known gaps are tracked in `docs/` — `SKILLS_ROADMAP.md` and `ECONOMY_GAPS.md` are the two
most honest about what does not work yet.
</content>

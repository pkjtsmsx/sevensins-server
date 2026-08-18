# Goals / quests: how a step becomes claimable

Written 2026-08-13 while getting Lucifer's and Beelzebub's Netherworld Notes to advance.
Companion to `docs/ECONOMY_GAPS.md` (which records the goods-id alignment) and
`docs/rpc_coverage.md`.

## The division of labour

The server owns **counters**. The client owns **claiming**.

`PlayerQuest.AnalysisQuest` (0x19635A4) rebuilds `normalWillCompletedQuestList` from the
counters on every quest payload; when that list GROWS it launches the "Mission
Complete!" toast. The player then taps Collect Reward and the client sends Quest cmd
257, which is the only thing that pays out.

Two consequences that cost real debugging time:

* **Bumping a counter without pushing a quest sync does nothing visible.** The client
  re-derives its claimable list only from a payload it receives. Every bump site must
  be followed by `quest_sync_msg(state)`.
* **Marking a quest complete server-side skips the claim**, so no banner appears and no
  reward is paid. Bump only. (`complete_stage_quests` is the deliberate exception and
  is, as of this writing, dead code — case-1002 stage-clear goals are derived
  client-side from stage ratings.)

## Where progress is stored

Decided by **`_case_type`** (design row +0xA8), **not** `_type` (+0x94). The two columns
disagree on 459 rows.

| `_case_type` | store | key |
|---|---|---|
| 1 | `quest_db` — a SHARED counter | `case_key(row)` = `"{_case_id}_{_case_v1}"`, plus `_{_case_v2}` when v2 >= 1 |
| 2 | `sp_quests[id]` — PER QUEST | `.cnt` is progress, `.status == 1` is claimed |

`PlayerQuest.GetQuestValue` (0x1963020) returns `SPQuestDB_Data + 0x18` = `cnt` for a
`_case_type` 2 quest. Filing a completion in the wrong store is silently ignored.

## `_case_v1` is sometimes a parameter and sometimes a discriminator

This is the trap. `bump_quest_counter(state, case_id)` advances **every** row sharing
the case id. That is right when `_case_v1` is a parameter to one shared counter, and
catastrophically wrong when it identifies *which* thing you did.

Always pass `case_v1=` for a discriminating case. Known cases:

| `_case_id` | meaning | `_case_v1` |
|---|---|---|
| 5 | clear a stage of a dungeon family | **discriminator** — the family (below) |
| 13 | gacha pulls | parameter (banner id; 0 = any) |
| 16 | complete a Skill-up | unused |
| 19 | perform a Rank Up | unused |
| 20 | perform a Cast Power-up | unused |
| 1002 | clear a specific stage | the stage id |
| 2003 | buy specific goods | **discriminator** — the goods id |

### Case 5 — dungeon families

| v1 | family | dmap root(s) |
|---|---|---|
| 0 | main story | 1001–1006 |
| 1 | **Kizuna Quests** | 22001–22076 (one per cast, 564 stages) |
| 2 | Starshard Temple | 40011 |
| 7 | ANY stage | — |
| 8 | Guild Boss | 1000001–1000037 (7 bosses × 4 difficulties) |
| 21 | Trainers Gym | 30004, 31004 |
| 22 | Rank Up / Evolution Abyss | 30002, 31002 |
| 23 | Transcend Corridor | 30014 |
| 24 | Treasure Raiders | 30003 |

A stage's family is its dmap's **root**: `dmap._link`, or the dmap itself when `_link`
is 0. Daily dungeons are their own roots; story chapters and tower floors point up.

**A "Kizuna Quest" is not the Kizuna Tower.** The in-game panel titled *Kizuna Quests*
lists one entry per cast ("Cupid's Envoy: Ravinia", 0/4 CLEAR). Those are dmaps
22001–22076. The 24 dmaps literally NAMED "Kizuna Tower" (41001, 41101, … 43501) hold a
separate 1440-stage stat grind ("Bond of TEC") that **does not appear in the EN build**.
Match the towers by NAME, never by id range — 41401/41404/41407/41410 sit in the same
span but are event maps ("The Deathblow", "Beauty Pageant").

Only **2 of the 46** case-5 rows are supported quest types; the rest are `_type` 7/8/9.

## Quest types we never arm

`UNSUPPORTED_QUEST_TYPES = {4, 6, 7, 8, 9}` — event, OFA, battle-pass, achievement and
"platinum mission" rows. Arming them writes `sp_quests` entries the client renders,
which puts the goal list in the wrong order and shows steps from chains the player never
started. `load()` purges any that an older build already wrote; a real device save
carried **40 orphans against 4 real entries**.

## Rewards

`complete_quests` defers to `shop.grant_goods`, the single place that knows how to turn
an item id into what it is actually worth. Two shapes need care:

* **A cast box.** `_action 2` whose Chinese `_itemName` is identical to an `_action 1`
  character item — that name match is the only link (the EN names differ). Grant the
  cast and report the `_action 1` item, then push Char `create` (529) BEFORE reply 513
  so the reveal leads and the item popup follows.
* **A bundle.** Also `_action 2`, and it hits the same double wall: `GetItemSpace`
  (0x18EE554) has no case for `_action 2` so no inventory tab holds it, and
  `EnqueItemPopupInfo` (0x15ABEC0) strips it from the popup. Contents are written down
  **only in the Chinese `_note1`** — the EN string is usually just the item name again.
  Reply 513 carries one (item, count) pair, so a multi-line bundle needs a follow-up
  `BackpackRpcCmd.DropItemRply` (24) listing every line.

  Worked example, quest 31019 "Evolution TUT Bundle" (item 1200006):
  `內含【★4機械之隸魔 賈桂琳x1】、【進化石x1200】` = ★4 Jacqueline ×1 + Evolution Gem
  ×1200. Corroborated by the next step paying 1200 Evolution Gems on its own.

## Navigation: the GO! button

A goal that sends you shopping carries the target in `_case_v1` and the button sends
Shop cmd **261** with `[goodsID]`. Answer with **517** `[shopID, tabID]`; the handler
bails unless both are present. See `docs/ECONOMY_GAPS.md` §6 — those goods ids are the
ORIGINAL server's and are not ours to choose.

## Debugging recipe

The device log is faster than reading IDA. An inert button, a goal that will not tick:

    grep -oE "no handler for index=0x[0-9a-f]+ cmd=[0-9]+" server/titan_server.log \
      | sort | uniq -c | sort -rn

That is how the GO! button was solved in one step — `cmd=261 int=[3305]` named both the
missing RPC and the goods id it wanted.

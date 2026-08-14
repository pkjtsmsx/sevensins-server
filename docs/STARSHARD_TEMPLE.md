# Starshard Temple: the pick-one flow, and our drop ladder

Written 2026-08-13 after a Temple clear froze the client. Two halves: a protocol that
was recovered, and a drop ladder that is our design.

## The freeze, and why the obvious fix was wrong

Clearing a Temple stage left the player on an empty altar room with no UI and no way out
but a restart. `PanelBattleRuneResult` is driven entirely by its rune list:

* `OnPanelEnable` (0x16BAF7C) skips its fill loop when the list is empty and parks at
  `UpdateResultState(0)`;
* `OnBattleEnd` (0x16BAB78) then does `if (EventArg.Count < 1) return;`, so it never
  reaches the `UpdateResultState(7)` that ends the sequence.

The first fix put shards in the end-reward `item_list`. **That is the wrong list.** The
panel reads `CurRuneList`, which only cmd 1507 fills — so it still opened with nothing.
Worth remembering: two different lists carry "what you just won", and the results panel
and the rune panel do not share one.

## The protocol

From `PlayerBattle.OnClientCmdReceived`:

| dir | cmd | what |
|---|---|---|
| S→C | **1507** `HandleRuneListCmd` (0x168ABAC) | `intargs=[curRuneIndex]`, `strargs=[List<BackpackItemData> candidates, List<int> priceList]`. **It launches the panel itself** — nothing opens until this is sent. |
| C→S | **508** `ServerRPCRuneSelect` (0x1687E50) | `intargs=[selectIdx]` |
| S→C | **1508** `HandleSelectRune` (0x168A5A8) | `intargs=[itemID, itemCount]` — re-raised as BattleEvent 12, which `OnBattleEnd` reads as `[0]=ItemID, [1]=ItemCount` before finally calling `UpdateResultState(7)`. |

**1508 carries the item, not the index.** An earlier handler echoed `[idx]` back, which
would have hung the panel a second time — `OnBattleEnd` needs `Count >= 1` and reads two
elements.

The candidate list is `List<BackpackItemData>` — the same `{sid, iid, amount, uid, attr}`
shape the backpack sync already emits, converted through
`BackpackItemDataToItemStructGem`.

### Three things that are easy to get wrong

**`CurRuneIndex` (1507's `intargs[0]`) is 1-BASED.** `OnPanelEnable` computes
`_curSelectRune = base[count-1] + CurRuneIndex - 1`, where base is 1/2/4 for 1/2/3
candidates (`dword_37037B4`). Sending 0 with two candidates selects slot 1 — the
*single*-rune layout's slot, which is not active in a two-rune panel. The countdown then
writes its label to a dead object (the button reads `(-s)`) and Claim operates on a slot
that is not there. The index the client sends BACK is 0-based over the candidates
(`TransIdx` / `dword_37037A0`).

**508 arrives TWICE.** The countdown auto-fires `ServerRPCRuneSelect` on expiry
(`GetRuneTimer`'s `MoveNext`, 0x16BB658), so a manual Claim followed by the timer
running out sends it again. Answering `[0, 0]` hands the panel item id 0 to render.
Replay the same grant instead — idempotent, and truthful.

**The item list and the item COUNT are different messages.** Cmd 84-87 carries the list
and raises BackpackEvent 4; the "Inventory n/999" figure comes from the backpack INFO
rows, which only cmd 83 and `BACKPACK_CHANGE` (145) carry. Pushing the list alone drew
the new shard over a stale counter. `BACKPACK_CHANGE` sends both and raises event 1, the
one an already-open panel refreshes on.

The panel lays out **1, 2 or 3** candidates (`_rune1_Middle_Info`,
`_rune2_Left_Info`/`_rune2_Right_Info`, `_rune3_*`), chosen by a `count-1` lookup. We
send 2, which is what live footage shows.

Because the player *picks* one, candidates are rolled at battle end but **not stored**
(`roll_rune` / `store_rune`, split out of `grant_rune`) — the panel prints their
attributes, so rolling again at selection time would hand over a different shard from the
one on screen.

## Identifying a Temple stage

`_book == 2` — exactly the 41 stages under dmap root 40011 and nothing else in the pack.
Use that, not a stage list.

## The drop ladder — OUR design

Not recovered. No form maps a Temple stage to shard drops, `ItemsRank` is empty on all
41, and `DesignStageRow` has **no accessor for `_itemrank_str`** — the client never reads
it, and asks the server for drop previews (`GetDrops` 8 → 25). Temple drop tables were
live-ops like every other stage's.

`_itemrank_str` (31, 32, … 60, ascending with difficulty) was tried as a ladder and
abandoned: unverifiable, and read literally it puts ★6 **plain** on the final stage.

What we ship instead — both axes rolled, nothing granted outright:

* **Star** slides with depth: a triangular window centred on `1 + depth*(MAX_STAR-1)`,
  `STARSHARD_STAR_SPREAD` wide. Expected star rises at **every one of the 41 stages**
  (1.33 → 4.67, no plateau), which is the property a hard band lacked — it made all 8
  stages inside a band identical.
* **Rarity** is a flat table applied everywhere — N 40 / R 30 / SR 20 / UR 8 / LR 2. A
  lucky ST-1 can pay an LR; ST-41 still sees plains. Depth buys the star, not the rank.
* **Sets** are the four on today's rotation. The in-game banner states it: Fortitude,
  Nightshade, Mystery, Devotee on Mon/Wed/Fri; Defender, Chaos, Hawkeye, Slayer on
  Tue/Thu/Sat/Sun. **"Fortitude" is the current EN name for the set the pack still calls
  Endearment (201)** — the banner reads Set(2) HP+19% and `equip_suit` row 1 Endearment
  is Set(2) HP 190.
* **Slots**: the two candidates take distinct slots, so the choice is a real one.

Shard ids encode `ELEMENT*1000 + slot*100 + rank*10 + star`, rank 0..4 = N/R/SR/UR/LR
(and `_param2` grade = rank + 1); `_action` = 110 + slot. All 8 sets carry the full
6 × 5 × 6 grid.

Tuning knobs: `STARSHARD_STAR_SPREAD` (window width), `STARSHARD_RANK_CHANCE` (rarity
table), `STARSHARD_DROPS_PER_CLEAR`.

## Drop Info

One icon per **(star, set)** the stage can actually roll — 8–12 of the display-only
`Random ★N <set>` items. Naming a single star under-reports a stage that rolls three;
the raw pool (4 sets × 6 slots × 3 stars × 5 ranks) is not a preview. Rank is not split
out because every rank drops on every stage, so the base icon stands for the set.

**The preview and the payout must be verified against each other, not merely written
together.** They drifted once already — `drops()` learned to pay shards while
`stage_drop_preview` still advertised coins, and the old docstring had predicted exactly
that. `test_battle_effects.py` now rolls all 41 stages and asserts the advertised
(set, star) pairs and the rolled ones are the same set, both directions.

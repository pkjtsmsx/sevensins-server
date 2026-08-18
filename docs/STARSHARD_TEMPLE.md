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

## The drop ladder — from `box_rank`

**Corrected 2026-08-18** after a report from play that our layout did not match the live
game's. The previous version of this section is preserved below the rule, because the
reasoning that led it astray is worth not repeating.

`_itemrank_str` is really **`box_rank`** — the constant it is read through is
`_FIELD_ITEMRANK_STR = "box_rank"` (dump.cs:497328). It is `"<max>,<min>"`, each a
two-digit `star*10 + rank` code, and the two digits bound their axes independently.
Across the pack it is a generic pair (book 0 `21,11`, book 1 `10,4`, the dailies `2,1`);
only the Temple varies it per floor:

| floors | box_rank | stars | ranks |
|---|---|---|---|
| ST-1–5 | `31,11` | ★1–★3 | R |
| ST-6–10 | `32,11` | ★1–★3 | R–SR |
| ST-11–15 | `33,11` | ★1–★3 | R–UR |
| ST-16–20 | `34,11` | ★1–★3 | R–LR |
| ST-21–25 | `41,11` | ★1–★4 | R |
| ST-26–28 | `42,11` | ★1–★4 | R–SR |
| ST-29–31 | `43,11` | ★1–★4 | R–UR |
| ST-32–37 | `44,11` | ★1–★4 | R–LR |
| ST-38–40 | `52,11`–`54,11` | ★1–★5 | SR–LR |
| ST-41 | `60,11` | ★1–★6 | any |

The star ceiling climbs ★3 → ★6 while the rank ceiling **re-walks R → LR inside each
star band**; the floor stays ★1 R everywhere. That reset is what makes the decode
believable — as two unrelated numbers it is incoherent, as "each star band re-walks the
rarity ladder" it is exactly right. Read as a lexicographic `(star, rank)` ceiling it
never regresses across all 41 floors.

* **Star** is weighted toward the ceiling of the floor's band (linear), so the band's
  top star is the likely outcome and ★1 stays possible but rare.
* **Rarity** is weighted toward the floor of the band, so a rising ceiling widens what
  is possible without making LR routine. **It is tied to depth**, which the previous
  design explicitly rejected.
* **Sets** are the four on today's rotation — see the banner note below. Unchanged and
  **verified against a live log 2026-08-18**: one clean group switch across the whole
  file, MWF sets all Monday evening and TTSS from Tuesday on. A report of "only
  Chaos/Hawkeye/Slayer/Defender" is that half doing its job. The rotation shares the
  **4AM** boundary with every other daily reset — it used to flip at midnight, so for
  four hours a night the Temple ran a day ahead of the rest of the game.
* **Slots**: the two candidates take distinct slots, so the choice is a real one.

ST-41's max rank digit is `0`, out of range for a ceiling. It is the one anomaly (a
lv457, 1-AP stage) and is read as **no rank cap**: the top floor offering every rank is
the only reading that is not a downgrade from ST-40.

Shard ids encode `ELEMENT*1000 + slot*100 + rank*10 + star`, rank 0..4 = N/R/SR/UR/LR
(and `_param2` grade = rank + 1); `_action` = 110 + slot. All 8 sets carry the full
6 × 5 × 6 grid, and every combination the bands can roll exists as an item (asserted).

Tuning knob: `STARSHARD_DROPS_PER_CLEAR`. The ladder itself is data now, not a knob.

---

### What was here before, and why it was wrong

> `_itemrank_str` (31, 32, … 60, ascending with difficulty) was tried as a ladder and
> abandoned: unverifiable, and read literally it puts ★6 **plain** on the final stage.

The client not reading a field is exactly what you expect of a **server-side** drop
table — it makes `box_rank` the surviving record of what live dropped, not junk. The one
anomaly (ST-41) was allowed to discredit an otherwise perfectly monotone 41-row ladder.
The invented replacement — star sliding ★1–★5, rarity a flat N/R/SR/UR/LR table
identical on every floor — is what the player noticed: ★6 could never drop at all, and
depth bought nothing but the star.

## Drop Info

One icon per **(star, set)** the stage can actually roll — the display-only
`Random ★N <set>` items. The **whole** band is listed: up to 6 stars × 4 sets = 24 icons
on the deepest floors. Truncating it to the top few stars was tried and reverted — the
floor stays ★1 all the way down, so a deep floor really can pay a ★1 and a truncated
preview under-reports it. The preview/payout cross-check below caught exactly that.
Rank is not split out because every rank in the band can drop, so the base icon stands
for the set.

**The preview and the payout must be verified against each other, not merely written
together.** They drifted once already — `drops()` learned to pay shards while
`stage_drop_preview` still advertised coins, and the old docstring had predicted exactly
that. `test_battle_effects.py` now rolls all 41 stages and asserts the advertised
(set, star) pairs and the rolled ones are the same set, both directions.

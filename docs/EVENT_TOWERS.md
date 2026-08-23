# Event towers: what is still in the pack, and what it would take to open them

Two tower events ship in the 2.2.7 EN pack and neither is reachable in game: the **Kizuna
Tower** (1,440 stages) and the **Skyscraper Department Store** (132 stages). Both are far
more complete than "absent content" suggests, and the reasons they are dark are different
and specific.

Written 2026-08-23 while answering an unrelated question about Kizuna gift economics. The
expensive part was locating the client machinery; that is recorded here so it does not
have to be found twice.

## The client machinery is generic

`Game.Gui.Panel.PanelUnlimitedTower` and `PanelUnlimitedTowerSpec` are **not** written for
one tower. Nothing in either is hardcoded to a chapter, a book or an item.

`PanelUnlimitedTower$$GetBrowsableChapterList` (0x17a50a0) is the whole selection rule:

1. `DesignStageForm.GetChapterFirstStageList(bookIndex, 1, 0)` — the first stage of every
   chapter in a **book**;
2. for each, look its `_dmap_id` up in `DesignStageChapterForm`;
3. **keep the rows whose dmap `_link` equals the panel's `_chapterLink`**;
4. sort by chapter order.

So a tower is defined entirely by *(book index, link root)*, and `SetChapterLink`
(0x17a431c) is how the caller picks one. Both towers' stages are `_book 17`, and both
hang their floors off a single root by `_link` — exactly the shape the panel expects.

`PanelUnlimitedTowerSpec` is the richer variant and reads like a spec sheet for this kind
of event:

| method | addr | what it does |
|---|---|---|
| `InitShowEventToken` | 0x17a9264 | draws the currency icons in the header |
| `OnGoShopClick` | 0x17aa54c | the "go to the store" button |
| `InitItemDrop` / `OnGetItemDrops` | 0x17a9be8 / 0x17aa76c | the drop-info browser |
| `InitBonusCharList` / `OnBonusCharClick` | 0x17a9110 / 0x17aa17c | event pick-up casts |
| `UpdateCost`, `GetBrowsableStageList` | 0x17a9738 / 0x17a95e8 | stamina and floor list |

`InitShowEventToken` is worth reading in full, because it is the join between the tower
and its currency: it calls `DesignEventListForm.GetActiveEvents(0)`, finds the row whose
**`_v1` equals the tower root's `_link`**, and then draws one `UIItemIcon` per id in that
row's **`EventItems`**, hiding the unused slots. No event row, no tokens.

## What decides "active" — three branches, and the third is the useful one

`Game.Design.DesignEventListForm$$GetActiveEvents(bool)` (0x19cbe50) walks every event row
and keeps it when:

* the row's `_type` is one of **7 / 17 / 27** and matches the `isNormal` argument — both
  towers are `_type 17`, and the panel passes 0, which selects exactly this family; **and**
* one of:
  1. the row carries a non-empty **stage list**, and the player's current stage value is in
     it (an unlock gate); or
  2. `BeginTime != 0`, and **now is between BeginTime and EndTime**; or
  3. **`BeginTime == 0` — always active, no window checked at all.**

The time it compares against comes from `PlayerSession`, not from `DateTime.Now`. The
struct naming in the decompile is mis-typed (fields present as `PointerEventData`), so
read that as the shape rather than as proof — but the *branching* is unambiguous, and
branch 3 is what makes both towers tractable.

## What each tower still has

| | Kizuna Tower | Skyscraper Department Store |
|---|---|---|
| root dmaps | **24** (41001, 41101 … 43501), `_link 0` | **1** (41416) |
| child dmaps | 240 — "1st Floor" … "10th Floor", 24 each | 100 — Tower Base ×10, Tower Body ×10, Shopping Street ×2, High Floors ×8, Preliminary ×6, Ranking ×64 |
| stages | **1,440** (24 × 10 × 6) | **132** |
| stage levels | 30–630 | 30–630 |
| mob groups | 180 referenced, **180 resolve** | 144 referenced, **144 resolve** |
| clear reward | **Soul Gem ×1** on all 1,440 | 10 category coins + 2 lottery tickets |
| event rows | **24, fully populated** | 1 (id 87), `_event_items` blanked to `0,0,0` |
| event window | real monthly rotation, 2024-01-30 → **2026-05-01** | `0 → 0` |
| currency sink | Soul Gem already levels Kizuna Skills | **none — no form prices anything in the coins** |

### The Kizuna Tower is dark because its events EXPIRED

24 rows (ids 2005, 2006, 2007 …), one per stat tower — "Kizuna Tower **Tower of TEC**",
"**Tower of AGI**", "**Tower of STR**", "Tower of Light" — each with banner 5071,
`_sp_prefab 1`, titles in four languages, and a real monthly rotation whose last window
closed **2026-05-01**. Today is later than that, so branch 2 rejects every row,
`GetBrowsableChapterList` finds no chapter, and the tower never opens.

Nothing else is missing. The reward is item 36, *"A common material. Used to level up
Kizuna Skills"* — a real item with an existing sink in our server. **No currency to
design, no shop to author, no blank field to fill.**

### The Department Store is dark for the opposite reason

Its row 87 has `BeginTime == 0`, so by branch 3 it is **always active**. What it lacks is
content:

* `_event_items` is `0,0,0` — three token slots with the ids stripped, which is the one
  field `InitShowEventToken` reads;
* `_shop_id` is unset, so `OnGoShopClick` has nothing to open;
* **nothing anywhere prices anything in the coins.** All 52 design forms were swept: the
  only literal mentions of 9511–9520 are the coin items themselves and unrelated ids in
  `equipment` (a separate id namespace).

The coins do say what they are for. Item 9511 `_note1_en`: *"Please exchange drink items
at the Department Store"*, and 請至**百貨塔商店**兌換**飲料類別**的道具 in the original —
"exchange for drink-CATEGORY items at the Department Tower shop". Ten coins, one per gift
category: Drink, Restaurant, Cosmetic, Sex Toy, Music, Festival, Gaming, Sports,
Bookstore, Weapon. Items 9521–9530 are the matching **Lading Bills**, and 9531/9532 are
**Shopping Street Lottery A/B**, which draw them.

**The Lading Bills carry their own drop table in prose.** Every one reads
`(Drop: 0-1, 1-6, 11-6)` — and all 132 of those labels resolve to real stages, with the
themes matching: Lading Bill (Drink) drops on **Drink Store** (1-6) and **Luxury Cocktail
Bar** (11-6); Weapon on **Weapon Store** and **Premium Defense Equipment Store**.

What is genuinely gone is the **Ranking half**: 64 Ranking dmaps and 6 Preliminary, all
with **zero stages**. That cannot be recovered, only reinvented.

## What it would take

**Kizuna Tower — a data edit, no design work.** Move the event windows into the present,
or zero `BeginTime` to take branch 3 and leave the towers permanently open. We already
patch the design pack (`tools/patch_design.py`), so this is the same class of change as
the server-row repoint. Open first: confirm what `PlayerSession` compares against and
whether the server supplies it, since if it does we can rotate the towers monthly the way
the pack intends instead of pinning them open.

**Department Store — design work.** Fill `_event_items`, stand up a shop the `_shop_id`
can point at, and decide what a coin and a Lading Bill buy. Gift items carry **no category
column** — across all 148 the only distinguishing field is `_param2` (10/20/100, the karma
value) — so the coin→category mapping has to be derived from names/icons or authored. That
is the same kind of decision as the drop pools, and should be labelled a design choice
rather than a reconstruction.

Both would render through the existing panels with no client change.

## What the Kizuna Tower's reward actually feeds, and why it does nothing yet

Soul Gem is *"A common material. Used to level up Kizuna Skills"* — so it is worth knowing
what that sink is before restoring a 1,440-stage faucet for it.

**Kizuna Skills are the "Active Effects 0/5 / Master" panel on a cast**, not a battle
skill. Two pieces of per-CHARACTER state (not per copy) ride on `CharIDData`:

* `BookKizunaSetData` — the equipped buff rows, **max 5** (`CharDefine.SoulBookKizunaMax
  EquipCount`)
* `BookKizunaLvData` — row id -> level

Each row is a `DesignSoulbookKizunaRow` carrying a list of **Partners**.
`SoulbookKizuna.Buff$$RefreshPartners` (0x1610b5c) walks that list and draws one character
icon per partner, hiding the empty slots, so a buff is gated on specific OTHER casts.
`LockEquip`/`UnlockEquip`, `PlayLevelUp` and `BuffModel$$get_ReachMaxLevel` are the rest of
it. Own and level the partners, equip up to five per cast, spend Soul Gems to level them.

This is almost certainly the system a contributor reported as **"Consonance — the stat
increase / new passives don't apply in battle"**: "buffs as you own and level up more
characters" describes this and not the `act_collection` type-101 collection skills, which
are a separate unimplemented system.

**We already do the wire half.** `book_kizuna_set` (313 -> 569) and `book_kizuna_lvup`
(320 -> 576) both work, and `kset`/`klv` are in every `charIDDic` we send.

**Two things are missing, and they are the same thing twice:**

1. `server/player_state/soulbook.py:130` records that level-up **costs and unlock
   conditions are NOT VALIDATED**, because the `soulbook_kizuna` design form defeats our
   type-tree generator -- `read_str out of bounds`, the row having nested
   `UnlockCondition[]` / `LevelVariable[]` arrays under a generic base. Confirmed still
   true 2026-08-23: the form is present in the pack and still will not parse.
2. **Nothing applies the buffs in battle.** `kizuna_set` / `kizuna_levels` have exactly one
   consumer, the char sync that echoes them back. No path reads them when a unit's stats
   are built, so a player can equip and level five buffs per cast, watch them in the menu,
   and get nothing in a fight.

The second follows from the first: the magnitudes live in the form we cannot read. So the
blocker is **not** a battle-engine gap -- it is a type-tree parsing problem, and the fix is
either teaching the generator those nested arrays or reading the row shape out of the
client (`Game.Design.DesignSoulbookKizunaForm`, and the
`MultipleSoulbbokKizunaSkillException` at 0x1aad7d4 suggests the form is keyed in a way
that can collide, which is a hint about its shape).

Worth settling before the Kizuna Tower goes in: a faucet whose currency levels a buff that
does nothing in combat is a grind for a menu number.

## A misattribution to clean up while here

`server/battle.py`'s Kizuna block calls items 9511–9520 "the Karma coin a bond dungeon
pays" and routes the 120 stages that pay them through `kizuna_drops`. **They are not bond
dungeon rewards.** They are Department Store tower currency, and the stages are Tower Base
/ Tower Body floors under root 41416.

The actual bond dungeons are dmaps **22001–22076** — one per cast, 12–24 stages across
Normal/Hard/Nightmare — and they pay the cast's **favourite gift** as a one-time Stage
Clear reward (Ravinia: 4 / 6 / 8 Roses per difficulty, 72 in total). That is a different
subsystem with a different economy, and the current naming sends a reader to the wrong one.

For the record, since it was measured on the way here: a full clear of a cast's bond
dungeon covers **9–15% of that cast's climb to Karma 30** (14.8% at ★3, 11.0% at ★4, 9.0%
at ★5), and the remainder is an intended Popular Poster grind — by design, not a gap.

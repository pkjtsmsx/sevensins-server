# Economy gaps — currencies the game charges for that nothing provides

Audited 2026-08-13 against commit `9c1cb00`. §7 retracted and §3 corrected the same
day — see both. The count is now **15**: 22 first reported, minus the two crystals (§3) and the
five Gremlin Pieces (§2). Method: diff every cost id our build
charges (the four storefronts' `CostID`, plus every gacha banner's `cost_tbl`) against
every item id our build can actually hand out (stage drops, quest `_item_id`,
login-bonus mail, `char_decompose` unsummon refunds, shop payouts).

Re-run the audit after adding any source or storefront card — the script is short
enough to rewrite, and the numbers below will drift.

**The rule this document exists to enforce:** never price a card in a currency with no
source. It renders correctly and cannot be bought, which looks like a bug and isn't
one. Three cards currently violate it (see "Self-inflicted" below).

## 1. Blocked on a whole subsystem

| id | name | spent at | needs |
|---|---|---|---|
| 4 | Guild Pt. | Belphe's, 8 cards | guild system |
| 9 | Medal of Pride | Belphe's PVP tab, 9 cards | arena / PvP |
| 11 | Paid Diamond | Mammon's, 2 cards | real-money IAP |

Paid Diamond is a *separate balance* from Diamond (CurrencyType 32 vs 1), so it cannot
be papered over by granting Diamonds. These three are the honest long-term items.

## 2. Blocked on content we do not run — CLEARED 2026-08-13

| id | name | status |
|---|---|---|
| 116–120 | ★1–★5 Pieces of Transcender Gremlin | **now drop from Transcend Corridor** |

The store card said "(Daily Dungeon Reward)" and the dungeon is the Transcender one —
`_book == 23`, 48 Transcend Corridor stages plus the 32 of its "[Double] Transcender
Hunt" variant, and nothing else in the pack. Each daily dungeon owns an exclusive book
(21 Trainers Gym, 22 Evolution Abyss, 23 here, 24 Treasure Raiders), the same way the
Starshard Temple owns book 2 — so the marker is the book, never a stage list.

Tier is rolled on a sliding window like the Temple's star, so every one of the 48 stages
pays better than the one before it (expected tier 1.25 → 4.75, no plateau). The window
is **narrower** than the Temple's on purpose: a Piece buys its Gremlin one-for-one, so a
wide spread would let Trans-1 mint ★5 Gremlins. 3 Pieces a clear, doubled in the
"[Double]" variant.

The other three dailies (Gym, Abyss, Raiders) still pay the generic coin fallback and
are the obvious next targets — their books are already identified above.

## 3. Live-ops data that never shipped in the pack — ours to invent

These have no source anywhere in the design pack. The tables were filled in piecemeal
upstream, which is why each group has holes rather than being wholly absent.

| id | name | spent at | note |
|---|---|---|---|
| 300001 | ★3 Minion Summon Orb Fragment | Orbs tab | 300002 (★4) HAS a quest source |
| 300003 | ★5 Awaker Summon Orb Fragment | Orbs tab | " |
| 300011–300015 | Inherit Gem Fragments (Sin/Virtue/Rider/★5/★4 Awaker) | Skill Up | all five missing |
| — | *(Grimoire of ★4/★5 Awaker Fragment, 545/544, are FINE — 1,250 and 5,830 from quests)* | | |
| 543 | Grimoire of Rider Fragment | Skill Up | 541/542 HAVE quest sources |
| 546 | Grimoire of ★3 Minion Fragment | Skill Up | " |
| 304 | ★4 LR Starshard Ticket | Star Shards | 301/302/303 HAVE quest sources |
| 1400414 | Virtue Soulmirror Scroll (Revisited) | gacha 1005 | banner unrollable |
| 1400430 | Sin Soulmirror Scroll (Revisited) | gacha 1004 | banner unrollable |

**CORRECTED 2026-08-13.** An earlier version of this section claimed Mana Crystal (501)
and Prime Mana Crystal (502) had no source, on the grounds that `char_decompose` only
pays Ex Grimoires. That is true of DECOMPOSE and irrelevant: **unsummon is a different
operation** — Char cmd **291** (`sell_chars`), not 292 (`char_decompose`) — and it pays
`char._sellId` × `char._sellNumber`, which is exactly the crystals. Confirmed in game and
in the log: `unsummon ['10000010142'] -> [(2, 3000), (502, 6)]`.

Supply, by the unsummoned cast's rarity:

| rarity | pays | casts |
|---|---|---|
| 3 | Mana Crystal (501) | 15 |
| 4–5 | Prime Mana Crystal (502) | 107 |

So the ★5 Awaker Orb (70 Mana Crystal) and both Bunrei Selector Boxes (Prime Mana
Crystal) are buyable, and always were. **The lesson: check the operation the player
actually performed.** Decompose and unsummon are different commands with different
payout tables.

## 4. Obtainable but finite

Sources exist, but every one is a one-shot quest reward — no repeatable tap, so these
run dry and then behave exactly like section 3.

| id | name | lifetime supply |
|---|---|---|
| 205 | Premium Awaker Scroll | 10, from 1 quest — and it is gacha 1001's ONLY currency |
| 302 | ★2 UR Starshard Ticket | 2 |
| 303 | ★3 LR Starshard Ticket | 2 |
| 301 | ★1 SR Starshard Ticket | 6 |
| 5829 | Soulmirror Scroll | 175, from 20 quests |
| 202 | Awaker Scroll | 282, from 81 quests |

Gacha 1001 is the sharpest edge here: 10 pulls total, ever.

Genuinely healthy: Diamond, Coin, Holy Blood of Saint (79,250 across 36 quests),
Grimoire of Sin/Virtue Fragment (1,580 each).

## 5. Priced in crystals — buyable, but the prices are placeholders

* goods 3601 — ★5 Awaker Orb, 70 **Mana Crystal**
* goods 3107 — ★5 Sin Bunrei Selector Box, **Prime Mana Crystal**
* goods 3108 — Virtue Bunrei Selector Box, **Prime Mana Crystal**

These were listed here as unbuyable while §3 wrongly said the crystals had no source.
They are fine — unsummon pays both. What IS still provisional is the price on 3107/3108
(1 each); the footage crops them. The ids, the currency and the contents are correct.

## 6. Goods ids are not always ours to choose

Separate from supply, and worth knowing before renumbering any card. Quests with
`_case_id 2003` are "go buy goods N" steps, and `_case_v1` holds the **original
server's goods id**. The GO! button sends it verbatim as cmd 261 and the server
answers with the shop and tab to jump to, so our card has to carry that exact id.

Recovered so far:

| goods | card |
|---|---|
| 3305 | Grimoire of ★4 Awaker — Soul Altar, Skill Up (quests 31017, 31034) |
| 3304 | Grimoire of ★5 Awaker — Soul Altar, Skill Up (quest 31041) |
| 1101 / 1201 / 1301 | Mammon's daily / weekly / monthly free (quests 10033, 10041, 10051) |
| 121 | 30 Days Monthly Pass (quest 44183) |
| 12011–12014 | Step Gift Boxes 1–4 (quests 50002–50005) |
| 1100935 | SoulMirror Pass (quest 44179) |
| 52745 | Angel Prefect Gabriel Pack (quest 80960) |

The Soul Altar's Summoning Orbs tab was squatting on 3301–3309 and had to move to
3601–3609. Check this table before assigning a goods id.

## 7. Gifts / karma — RETRACTED, this was wrong

An earlier note here claimed gifts had no source and karma rank was unreachable. That
is **false**, checked 2026-08-13 against a live account:

* Five gift items (`_action 3`) were already held — Popular Manga, Electric Generator,
  Wine, Ramen, Popular Poster (~140 items total).
* Quest 31020 pays Popular Poster ×10 and it filed correctly into `_gift_List`.
* `give_gifts` works end to end: 5 Popular Posters = +500 karma xp, ranking Lucifer
  7 → 8.

Karma is fully functional. Nothing to do here.

## Suggested order

1. Drop tables for the other three dailies — Trainers Gym (book 21), Evolution Abyss
   (22) and Treasure Raiders (24), following the Transcend Corridor pattern.
2. Fragment sources (300001/300003, 300011–300015, 543, 546, 304). These are all
   "some dungeon or daily pays N of these"; pick a consistent home for them rather
   than scattering.
3. Soulmirror Scrolls (Revisited) — without them two of the three Soulmirror banners
   cannot be rolled at all.
4. Guild / Arena / IAP when those subsystems land.

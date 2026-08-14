# Economy gaps — currencies the game charges for that nothing provides

Audited 2026-08-13 against commit `9c1cb00`. Method: diff every cost id our build
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

## 2. Blocked on content we do not run

| id | name |
|---|---|
| 116–120 | ★1–★5 Pieces of Transcender Gremlin |

The store card's own description says **"(Daily Dungeon Reward)"** — the client is
telling us the source. The five Gremlin cards are otherwise complete and uncapped.
Cheapest real fix in this whole document: give the daily dungeon a drop table.

## 3. Live-ops data that never shipped in the pack — ours to invent

These have no source anywhere in the design pack. The tables were filled in piecemeal
upstream, which is why each group has holes rather than being wholly absent.

| id | name | spent at | note |
|---|---|---|---|
| 501 | Mana Crystal | Soul Altar ★5 Awaker Orb | see below |
| 502 | Prime Mana Crystal | Soul Altar, both Bunrei selectors | see below |
| 300001 | ★3 Minion Summon Orb Fragment | Orbs tab | 300002 (★4) HAS a quest source |
| 300003 | ★5 Awaker Summon Orb Fragment | Orbs tab | " |
| 300011–300015 | Inherit Gem Fragments (Sin/Virtue/Rider/★5/★4 Awaker) | Skill Up | all five missing |
| — | *(Grimoire of ★4/★5 Awaker Fragment, 545/544, are FINE — 1,250 and 5,830 from quests)* | | |
| 543 | Grimoire of Rider Fragment | Skill Up | 541/542 HAVE quest sources |
| 546 | Grimoire of ★3 Minion Fragment | Skill Up | " |
| 304 | ★4 LR Starshard Ticket | Star Shards | 301/302/303 HAVE quest sources |
| 1400414 | Virtue Soulmirror Scroll (Revisited) | gacha 1005 | banner unrollable |
| 1400430 | Sin Soulmirror Scroll (Revisited) | gacha 1004 | banner unrollable |

**Mana Crystal is NOT an unsummon refund in the pack.** `char_decompose` only ever pays
Ex Grimoires — Gust of Agility, Fist of Strength, Book of Technique, Book of Shadows —
and neither 501 nor 502 appears as an item id in `char_decompose`, `formula`,
`transmute`, `quest`, `mail` or `shop`. In the live game unsummoning did pay them, so
that payout was server-side and is ours to write. Wiring it unblocks three cards at
once and is probably the best first move in this section.

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

## 5. Self-inflicted — fix before anything else

Three Soul Altar cards added on 2026-08-13 are priced in currencies from section 3, so
they render and cannot be bought:

* goods 3301 — ★5 Awaker Orb, 70 **Mana Crystal**
* goods 3107 — ★5 Sin Bunrei Selector Box, **Prime Mana Crystal**
* goods 3108 — Virtue Bunrei Selector Box, **Prime Mana Crystal**

The prices on 3107/3108 are placeholders (1 each); the footage crops them. The ids,
the currency and the contents are correct.

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

## 7. Adjacent, same shape

Not currencies, but the identical failure — the sink exists and the source does not:

* **Gift items.** Karma/affection rank needs gifts; nothing in the game drops one, so
  rank is unreachable. Diagnosed 2026-08-12, not implemented. The rank formula itself
  was verified correct.

## Suggested order

1. Fix section 5 (data only, minutes).
2. Mana Crystal + Prime Mana Crystal on unsummon — unblocks three cards, and it is the
   payout the live game had.
3. Daily-dungeon drop table for Gremlin Pieces — the client already names the source.
4. Fragment sources (300001/300003, 300011–300015, 543, 546, 304). These are all
   "some dungeon or daily pays N of these"; pick a consistent home for them rather
   than scattering.
5. Soulmirror Scrolls (Revisited) — without them two of the three Soulmirror banners
   cannot be rolled at all.
6. Gift drops, then Guild / Arena / IAP when those subsystems land.

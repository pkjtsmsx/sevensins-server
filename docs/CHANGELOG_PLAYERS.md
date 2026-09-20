# Server Update — 20 September

**How to update:** open the app, tap **Check for updates**, then restart the app when it
asks. Your account is not touched by an update.

**Heads-up:** this is a big battle update and most of it is fresh — if a fight behaves
strangely, that report is exactly what we need.

---

## The guild defeat screen works

Losing a Guild Weekly fight left you stuck on "BATTLE ENDS" with a Tap to End that did
nothing — your score was banked, but the only way out was killing the app. Every tap
was actually crashing inside the results screen because of one missing value in the
defeat payload. Fixed and verified on a device; win or lose, the fight now exits
cleanly to the Record page. (The "BATTLE ENDS" banner on a guild *victory* is what the
real game shows too — that part was never a bug.)

## [Elite] finally means something

185 skills in the game say things like "if the target is Elite…" — bonus damage,
crit buffs, whole kits built around boss-killing. None of them ever worked, twice over:
the bosses that ARE Elite never actually held the marker, and the "is the target
Elite?" check was being asked about the wrong unit. Both fixed. Daily dungeon bosses,
tower bosses, raid bosses and each story chapter's final boss now count as Elite, and
your Elite-killer kits read it correctly.

## Passives you could never see — or feel

- **"Can only trigger once per battle" now means once.** Some limited passives (life
  steals, one-time saves) could re-trigger on every hit.
- **Turn-start and after-action passives now show up.** Buffs a passive granted
  mid-fight were applied silently — no icon, no popup. Kills from passive damage are
  now announced instead of leaving the victim standing on your screen forever.
- **Revive passives reach the screen.** A passive that raised an ally used to change
  the server's numbers and tell nobody — the "revived" unit looked dead until relog.
- **Deaths trigger everything they should.** A counter-kill, a poison death or a
  stray rider kill now fires on-death passives (revives, cleanses) like a direct kill.

## Counterattacks, round two

Last update made 8 casts counter. This one fixes the rest: about **100 counter
passives** now fire, including Eternal Dream/Freeze, Knight's Spirit, Sky Devil,
Solidarity, Royal Flush and the Vengeance set bonus. Two subtleties from the Chinese
text: Asmodeus (Royal Flush) and the Vengeance set counter with **the attacker's**
ATK, not their own — and Sky Devil's early ranks counter at 100%, not 110%.

## Buffs carry their real numbers

Around **420 status effects** that applied, drew an icon and moved nothing now carry
the value their description states — including the Guard/Power/Speed/Critical/Health
Surge families, whose strength now follows the SKILL that granted them (an enhanced
bloodpact passive grants a stronger Surge than the base version, as its text says).

## Starshard luckybags (hotfix, later the same day)

Three user reports, all real, all fixed — thank you:

- **★4 bags paid ★1–★6 shards.** The bag's star and the shard's rank are two
  different axes and the pool was filtered on the wrong one. A ★4 (UR-LR) bag now
  pays exactly ★4 shards at UR or LR, and the ★3 slot-6 bag pays ★3, slot 6.
- **Bulk buys looked like ten copies of one shard.** They never were — the rolls
  were always different, but the popup could only name one item and reported the
  last roll ×10, and the Starshards list didn't refresh to disprove it. Multi-buys
  now show an itemized drop list, and the panel refreshes immediately.
- **"Test" starshards.** The game data carries 18 leftover early-development shard
  rows that look shard-shaped to a filter; they were in the any-element bag's pool
  and are now excluded.

## Rewards go where you can see them

- Casts, Bunrei and skill books from **mail, roulette, guild payouts and event
  exchanges** now land in your Cast List instead of vanishing (the shop already
  worked).
- **LR Soulmirror Sets and Selectors** open into real mirrors (about half the boxes in
  the game — the rest, mostly outfit-series, still land in the bag while we work out
  their mapping).
- The **★5 Awaker Orb** now always pays an SSR cast. It could pay a ★4-grade beginner
  cast before.
- Casts from quest rewards and exchanges arrive at the **right star** (an SSR from a
  shard shop was arriving one rung low).
- Items split across two bag stacks now **actually get consumed** — some purchases
  and karma gifts were effectively free, which also meant some players got karma for
  gifts that never left their bag.

---

# Server Update — 12 September

**How to update:** open the app, tap **Check for updates**, then restart the app when it
asks. Your account is not touched by an update.

This one is almost entirely battle. A lot of what a skill's description promises was
being read out of the game's data and then quietly dropped on the way to the fight — so
the tooltip said one thing and the numbers did another. Roughly 1,600 skill clauses that
did nothing now do something.

---

## Bosses fight back

**Raid bosses were hitting for about a third of their real damage.** 150 skills in the
game deal "a percentage of the target's max HP" on top of their normal hit, and that
half of the attack was missing entirely. The Guild Weekly boss's big move is the clearest
case: it now does roughly **10,600** to a 35,000 HP cast where it used to do about 3,800.

If the weekly raid felt like an unkillable punching bag that could not hurt you back —
that was this. It is a real fight now.

Your own "% of max HP" skills still will not melt a raid boss: the game marks those
enemies **Elite**, and the skill text has always said the effect does not trigger on
them. That exemption is now honoured, so the rule works in both directions.

## Counterattacks exist

**Eight casts counter when they are struck, and not one of them ever did.** Leviathan,
Raphael, Belphegor, Asmodeus, Caillen, Leon, Zoe and the Vengeance set bonus.

- **Leviathan** (Jealousy Vortex) counters for a share of her **ATK** — 225% at rank 2.
- **Raphael** (Serene Way of Harmony) counters for a share of his **DEF** — and his own
  battle-start DEF buff now feeds it, so his counter is noticeably harder in the opening
  turns.

A counter fires once per enemy skill, not once per hit, so a three-target sweep gets one
counter and not three.

## Defensive buffs actually defend

Statuses that change how much damage you take — **Fortitude, Fragile, Wide Defense,
Kitty Bell, Legion Aegis, Death Beacon** and 140 others — were being applied to the
wrong side of the exchange, and often with the sign inverted. A defensive buff could end
up making you **hit harder** instead of take less. Tanks should feel like tanks now, and
Fragile should hurt.

## Healing and cleansing

- **Around 190 heal clauses did nothing at all.** Any skill whose healing rides on its
  attack, and most passives that heal, simply never paid out.
- **Michael's Gate of Judgement** healed only himself with the party's share. It heals
  the whole party now — and its attack animation no longer plays on the allies it is
  healing.
- **Triggered cleanses work.** "At the start of the turn, clear your own damage-over-
  time", "when you attack, strip the target's buffs" — none of these fired. Mammon
  shrugging off his own poison, Beelzebub stripping Harden off what she hits.
- **Revives and move-gauge clauses** stated in a skill's text but with nothing behind
  them now run, including Metatron's raise-a-fallen-ally and the gauge swings on
  Ocean Strike (which was applying its enemy debuff twice and skipping its own buff).

## Your casts got stronger (again)

Two more sources of power that the panels promised and nothing paid:

- **Skill Up stat rewards.** Every cast has a ladder of flat HP/ATK/DEF/SPD bonuses that
  unlock with Skill Up rank — the panel lists them beside each rank. The rank itself
  always worked; the stats never did. 291 casts carry one.
- **Starshard and Soulmirror crit sub-stats.** CRI, CRIT DMG, Effect Hit and Effect RES
  rolled on your gear showed in the cast menu and did nothing in battle. They count now.
  (Very old shards carrying an impossible CRI roll are capped rather than honoured.)
- **Crit statuses work.** Critical Surge, Execute Critical and Critical Injection were
  inert in both directions.
- **Skill ranks stopped sharing numbers.** A passive's rank I through VI could all end up
  using whichever rank the server happened to look at first — so a maxed passive might
  quietly pay rank I's figure. Each rank uses its own numbers now.

## Enemies got their kit back

- **68 mob heal passives and 28 life-steal attacks** were doing nothing. Ordinary trash
  can now heal itself and drain a share of the damage it deals, so some fights run a
  little longer.

## Guild Weekly is playable again

**The three daily challenge passes never came back.** Once you spent them the mode was
finished permanently — and because the game has no "you are out of attempts" message,
the Challenge button simply did nothing when you pressed it. Passes now refill each day
at the usual 4AM reset, and the counter on the panel tells the truth.

## Smaller things

- **Roulette wins appear immediately.** Stamina prizes were credited to your account but
  not shown until you relogged.
- **A unit killed by a counterattack is properly reported** — previously the fight could
  sit waiting for a turn from someone who was already dead.

---

## Known issues

- The Guild Raid boss's info panel shows a placeholder line instead of its weekday /
  advantage / tips text. The text for it is not present in the game's data files, so
  there is nothing to display; the fight itself is unaffected.
- The raid member leaderboard fills in when you switch to the **Guild** tab and back to
  **Member**. (The original game behaved the same way.)
- Ultimate cut-in animations do not play during raid battles. This matches the original
  game.

---

# Server Update — 26 August

**How to update:** open the app, tap **Check for updates**, then restart the app when it
asks. Your account is not touched by an update.

---

## Your casts got stronger — for real this time

Three whole sources of power existed in the game's data and were never being paid.

- **Consonance (Karma) rewards now apply.** Every cast has a ladder of stat bonuses that
  unlock as Karma climbs — the panel always promised them, but nothing ever added them.
  A maxed ladder is roughly **+40% HP, +60% ATK, +100% DEF and +25% SPD** on a level-100
  cast, plus its crit rate and crit damage rungs. If you have been gifting, you will feel
  this immediately.
- **Crit exists now.** Crit rate and crit damage were modelled in the battle engine but
  nothing ever fed them, so every cast in the game fought at a flat base crit. Consonance
  crit rungs now reach the fight, and the stat popup shows the real numbers instead of
  zeros.
- **DEF-scaling casts respond to their own DEF buffs.** Kits that deal damage from DEF
  (Belphegor and about a hundred other skills) ignored Harden, Tough, Defense Tips and
  every DEF set bonus on their own damage. They count now.
- **Soul Link rank shows at login.** The fight already applied it; the menu said rank 0.

## Buffs land on the right side

Attacks that grant something to *your* team — Kamilah's Scorpion Kiss speeding up your
fastest allies, Belphegor's Shark Shark Attack shielding the whole party — were often
handing that buff to the enemy you just hit instead. The rules are now read from the
game's original text rather than the English translation, which has real errors.
Hundreds of skills changed hands; if a buff icon used to appear on the wrong side, it
should not any more.

The same fix reached passives: the Guild Raid's Gabriel was **dazing herself** at the
start of every fight.

## Immunities actually work

Every named immunity in the game — Freeze Immunity, Daze Immunity, Charm/Confuse/…
Immunity, 82 of them — was decoration. Only the "CC Immunity" family ever blocked
anything. They all block what they name now, on your casts and on bosses.

## Status icons stay honest

A debuff that had already worn off on the server could stay drawn on the enemy with
"1 turn remaining" for the rest of the fight (you may have seen a boss sitting "frozen"
long after she was not). Expired statuses are now cleared on your screen too.

## Battles

- **The move gauge restarts with every wave**, for both sides — your fast cast no longer
  gets a free opening turn on wave 2 while enemies start from zero.
- **Enemy passives and traits are fuller.** Hundreds of passive effects the game
  describes by effect rather than by name were being dropped; they fire now. Bosses keep
  their built-in gauge protection, for one.
- **No empty clears** in the Trainers Gym; the Transcend Corridor and Starshard Temple
  pay one drop per wave like everything else.

**A note on the Guild Raid:** Gabriel can still be chain-frozen. That is how the game's
data has her — the other raid bosses carry full CC immunity and she deliberately does
not — so it is left as designed rather than "fixed".

## Under the hood

- **Reconnecting can no longer lose progress.** If the game reconnects mid-session, the
  newest login owns the account; an older connection can no longer overwrite it.
- Saves are half the size on disk, the server log no longer grows without limit, and a
  handler crash now names the command it was answering so bugs get found faster.
- The whole request-handling layer was rebuilt as a table; nothing should behave
  differently, but if anything does, report it with what you tapped.

**Optional new APK:** the host app now binds its servers to your phone only (127.0.0.1)
instead of every network interface. This is a security improvement and needs the new APK
(installed over the old one — your account stays). The hot update alone keeps working
either way.

---

# Server Update — 18 August

**How to update:** open the app, tap **Check for updates**, then restart the app when it
asks. Your account is not touched by an update.

---

## New: Guilds & the Guild Raid

Guilds are in. You can found one, name it, set its badge and advert, and check in daily.

- **Daily check-in** pays **Guild Pt.** on a five-step bonus ladder.
- **Guild Pt.** now actually has a source — the shop has always priced nine items in it
  and there was previously no way to earn any.
- **Daily Raid (Virtue's Challenge)** is open: seven bosses, one for each day of the
  week, each on four difficulties from Easy to Nightmare.
  - **3 attempts a day.** Your best run is your score; higher difficulties add a bonus
    on top of the damage you deal.
  - Damage milestones pay out as you climb them, so a better run tops up rather than
    re-paying the ladder.
  - The raid has its **own saved team per boss** — set it with the Edit button on the
    preparation screen, and it will not disturb your campaign teams.
  - Guild Ranking, My Record (1st/2nd/3rd Try + Best Score) and Raid pt. all track your
    runs.

---

## Fixes

### Starshards
- **Starshard stats and substats now actually apply in battle.** Previously a fully
  geared cast fought at base stats — the lobby showed the bonuses, but the fight ignored
  them. This is a straight power increase for anyone using starshards.
- Fixed impossible substat values (a ★1 shard rolling *CRI +999.0%*). Rolls are now
  drawn from the correct pool for each slot.
- **Starshard Temple** now rotates its set drops correctly. Previously the same four
  sets — Defender, Chaos, Hawkeye, Slayer — appeared every day. Drop quality now scales
  with the floor you are on.
- The daily rotation now rolls over at **4 AM**, in line with everything else.

### Rewards
- **Gems and Stamina rewards are now actually credited.** The reward popup was correct,
  but the balance never moved — from goals, karma, stage clears, anywhere.
- Starshard rewards from goals (Stage 29's in particular) now hand over a real shard
  instead of nothing.
- **Karma rank-ups** now pay their Rank Bonus — and pay for **every** rank you cross if
  you gain several at once.
- **Achievements** now track and complete.

### Accounts
- New accounts start with the correct **3,000 gems**.
- **Stamina cap** is now correct and grows as you rank up, along with the stamina
  refunded on rank-up.

### Battle
- Class auras and Bloodpact auras now display under your casts.
- Multi-hit skills play every swing instead of only the first.

---

## Known issues

- The Guild Raid boss's info panel shows a placeholder line instead of its weekday /
  advantage / tips text. The text for it is not present in the game's data files, so
  there is nothing to display; the fight itself is unaffected.
- The raid member leaderboard fills in when you switch to the **Guild** tab and back to
  **Member**. (The original game behaved the same way.)
- Ultimate cut-in animations do not play during raid battles. This matches the original
  game.

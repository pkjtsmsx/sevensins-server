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

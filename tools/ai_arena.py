#!/usr/bin/env python3
"""AI arena: settle which move chooser actually plays better.

    python3 tools/ai_arena.py                            # round-robin, mirror teams
    python3 tools/ai_arena.py --pool mob --teams 60      # the pool enemy AI drives
    python3 tools/ai_arena.py --mode mirror --a tier1 --b greedy
    python3 tools/ai_arena.py --mode stage               # asymmetric: real stages
    python3 tools/ai_arena.py --null tier1               # swap-bookkeeping check

## Why a mirror match

The obvious experiment -- play real stages with each chooser and compare clear rates --
does not work on this game's data, and the first attempt at it produced a confidently
wrong answer. Two reasons:

  * **The fights are not contested.** A seeded roster one-shots the early stages (party
    at 1,784 HP against 239-HP mobs) and is hopeless against the later ones, where the
    enemies act first and wipe it in five turns without it ever taking a turn. Either way
    the chooser never touches the outcome, and both choosers score identically.
  * **Making them contested by scaling the enemies is a trap.** Scaling enemy ATK to
    lengthen fights is what broke the first harness: recoil and damage-over-time read the
    ATTACKER'S OWN ATK (`status.tick_damage` snapshots `source_atk`), so at 300x ATK the
    enemies killed themselves, and the sweep was measuring which chooser picked the more
    suicidal skill. **Never scale ATK.** Stage mode below scales HP only, for that reason.

A mirror match sidesteps both: identical teams on both sides, one chooser driving each.
Nothing is left that could decide the fight except the decisions. It is the same
instrument a chess engine match uses, and it comes with a free validity check.

## Two validity checks, and what each one is actually worth

Identical teams still give the side that acts first an advantage -- `rank_key` breaks a
SPD tie by team, so team 1 leads. Every match is therefore played TWICE, once in each
orientation, with the same team and the same seed, and the bias cancels.

**`--null` (a chooser against itself) is mostly a STRUCTURAL check.** Be clear about
this: with A on both sides the two orientations are usually the same fight, so one counts
as a win and one as a loss and the result is pinned near 50% by construction rather than by
evidence. It verifies the swap bookkeeping. Reporting it as proof of an unbiased harness
would be a nicer-sounding version of the mistake this tool exists to avoid.

It is not a pure identity, though: `battle._forced_target` scrambles a Charm/Confuse target
with the unseeded module-level `random.choice`, so any fight involving one diverges between
the two orientations. Measured at 52.5% +/- 7.7 over 160 mirror fights -- the interval
covers 50%, and the spread is exactly that.

**The check that carries weight is sensitivity.** `random` -- any legal move, uniformly --
is in the table as an anchor. A chooser that cannot beat random decisively means the metric
is too coarse to resolve anything, and then a close A-vs-B number says nothing either. Read
the tournament table as a whole: the gap to `random` is the scale bar for the gap between
the two real choosers.
"""
import argparse
import math
import os
import random
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(os.path.dirname(HERE), "server")
sys.path.insert(0, SERVER)

import battle as bt                                            # noqa: E402
import battle_ai                                               # noqa: E402
import design_data as dd                                       # noqa: E402
from engine import bridge as _bridge                           # noqa: E402
from engine import specs as _specs                             # noqa: E402

PLAYER_TYPES = (1, 2, 3, 5)          # `char._type`; 7 is a mob
MOB_TYPE = 7
SHELL_STAGE = 1101                   # any stage with enemies; only its shell is used
TURN_CAP = 400


# --- the choosers under test ---------------------------------------------------------
#
# A chooser is `(battle, target_team) -> move tuple`. Adding one is a line here, which is
# the point of the policy seam: an experiment should not need a branch in battle.py.

def _greedy(b, target_team):
    return b._auto_move_greedy(target_team)


def _tier1(b, target_team):
    """Argmax, no softmax. Both sides play their best so the match measures the SCORER
    rather than the difficulty handicap -- see `tier1-enemy` for the shipped enemy knob."""
    return battle_ai.choose(b, target_team, policy=battle_ai.POLICIES["player_auto"])


def _tier1_enemy(b, target_team):
    """The policy enemies actually ship with: softmax at temperature 0.35."""
    return battle_ai.choose(b, target_team, policy=battle_ai.POLICIES["enemy"])


def _random_move(b, target_team):
    """A floor to calibrate against: any legal move, uniformly. If a chooser cannot beat
    this convincingly, the metric is not sensitive enough to trust."""
    attacker = b.acting_unit()
    if attacker is None:
        return None
    foes = [u for u in b.units.values() if u.team == target_team and u.alive]
    if not foes:
        return None
    rng = random.Random(f"{b.round}:{attacker.order}")
    slot = rng.choice(b.usable_slots(attacker))
    return attacker.order, rng.choice(foes).order, attacker.skills[slot], slot


CHOOSERS = {
    "greedy": _greedy,
    "tier1": _tier1,
    "tier1-enemy": _tier1_enemy,
    "random": _random_move,
}


# --- building a fight ----------------------------------------------------------------

def playable(pool):
    """-> char ids usable as combatants, from the design pack."""
    want = {"player": PLAYER_TYPES, "mob": (MOB_TYPE,),
            "mixed": PLAYER_TYPES + (MOB_TYPE,)}[pool]
    out = []
    for cid, row in dd.rows("char").items():
        if row.get("_type") not in want:
            continue
        if len([s for s in (row.get("_skills") or []) if s]) < 3:
            continue
        out.append(cid)
    return sorted(out)


def stage_level(stage_id):
    """-> the level the PACK says a player should be at for this stage.

    `_stagelv` is the recommendation shown to the player and is the right knob: it is
    frequently well above the stage's own mob level for side content (stage 1513616 is
    `_stagelv` 180 against level-30 mobs), and the two agree for main-campaign stages. The
    max mob level is the fallback where `_stagelv` is unset.
    """
    row = dd.row("stage", stage_id) or {}
    lv = int(row.get("_stagelv") or 0)
    if lv:
        return lv
    levels = [int((dd.row("mob_group", g) or {}).get("_mob_level") or 0)
              for g in dd.csv_ints(row.get("_mobGroup_datas"))]
    return max(levels or [1]) or 1


# What a player plausibly FIELDS at a given point, by rarity. The pack states no such
# progression curve, so this is a judgement, kept in one place and stated as one: a level-20
# account is not running a team of 5-star casts, and testing with one is what made the
# default test roster useless as a bench.
RARITY_BANDS = ((25, (1, 2)), (60, (1, 2, 3, 4)), (10 ** 9, (2, 3, 4, 5)))


def level_appropriate_team(stage_id, seed=0):
    """-> (five char ids a player at this stage would plausibly field, party level)."""
    level = stage_level(stage_id)
    band = next(r for cap, r in RARITY_BANDS if level <= cap)
    rows = dd.rows("char")
    pool = [cid for cid in playable("player")
            if (rows.get(cid) or {}).get("_rarity") in band]
    if len(pool) < 5:
        pool = playable("player")
    return tuple(random.Random(f"{stage_id}:{seed}").sample(pool, 5)), level


def multiwave_stages(count, seed=0, min_waves=2):
    """-> fightable stages with at least `min_waves`, spread across the level range."""
    out = []
    for sid, row in dd.rows("stage").items():
        groups = dd.csv_ints(row.get("_mobGroup_datas"))
        if len(groups) >= min_waves:
            out.append(sid)
    out.sort(key=stage_level)
    if len(out) <= count:
        return out
    step = len(out) / count                    # even spread over the difficulty curve
    return [out[int(i * step)] for i in range(count)]


def mirror_battle(team, level):
    """A fight with the SAME five casts on both sides.

    Built on a real stage's shell so every code path the server uses is the one exercised
    here -- then both sides are replaced, so nothing about the stage's own difficulty
    survives into the result.
    """
    b = bt.Battle(SHELL_STAGE, list(team), level, None, 0, 0)
    b.units = {}
    for slot, char_id in enumerate(team):
        for team_no, base in ((bt.TEAM_PLAYER, 101), (bt.TEAM_ENEMY, 201)):
            order = str(base + slot)
            b.units[order] = bt.Unit(order, char_id, team_no, slot, lv=level)
    b.enemy_order_base = 201
    # One wave, and no next one: `advance_wave` would respawn the stage's own mobs and
    # quietly end the mirror.
    b.wave_groups, b.wave_max, b.wave = [], 1, 1
    b._apply_battle_start(list(b.units.values()))
    b._roll_turn_order()
    return b


def stage_battle(stage_id, roster, level, hp_scale):
    """A real stage, with the enemies' HP (and ONLY their HP) scaled.

    ATK is never touched: see the module docstring for the self-inflicted-damage trap
    that makes an ATK-scaled sweep measure nothing.
    """
    b = bt.Battle(stage_id, list(roster), level, None, 0, 0)
    if hp_scale != 1:
        for u in b.units.values():
            if u.team == bt.TEAM_ENEMY:
                u.max_hp = u.hp = int(u.max_hp * hp_scale)
    return b


# --- playing it ----------------------------------------------------------------------

def run_fight(b, chooser_by_team, seed, cap=TURN_CAP):
    """-> (winning team, turns, hp fraction left for each team).

    Uses the SERVER's own turn flow -- forced-target override, `bridge.attack_combo` with
    its write-back of gauge/cooldowns/tallies, `spend_skill`, `end_turn` -- so a result
    here is a result about the real game and not about a simplified model of it. The only
    thing skipped is building the wire JSON, which is presentation.
    """
    rng = random.Random(seed)
    start_hp = {t: sum(u.max_hp for u in b.units.values() if u.team == t) for t in (1, 2)}
    for turn in range(cap):
        if not b.team_alive(bt.TEAM_PLAYER):
            return bt.TEAM_ENEMY, turn, _hp_left(b, start_hp)
        if not b.team_alive(bt.TEAM_ENEMY):
            # Clearing the field is not clearing the STAGE. 4,575 of the 6,625 fightable
            # stages run 2, 3 or 5 waves, and `wave_cleared()` only ever meant "this
            # wave". Stopping here counted a wave-1 kill as a stage clear -- so the
            # earlier stage runs never measured the thing multi-wave play is actually
            # about: carrying HP, cooldowns and statuses forward into the next wave.
            if not b.has_next_wave():
                return bt.TEAM_PLAYER, turn, _hp_left(b, start_hp)
            b.advance_wave()
            start_hp[2] += sum(u.max_hp for u in b.units.values()
                               if u.team == bt.TEAM_ENEMY)
            continue
        actor = b.acting_unit()
        if actor is None:
            break
        target_team = bt.TEAM_ENEMY if actor.team == bt.TEAM_PLAYER else bt.TEAM_PLAYER
        move = chooser_by_team[actor.team](b, target_team)
        if not move:
            break
        att, dfn, skill_id, slot = move
        attacker, target = b.units.get(att), b.units.get(dfn)
        if attacker is not None and target is not None:
            # The same override `attack_cmd_json` applies, applied here for the same
            # reason: a taunted unit does not get to pick.
            target = b._forced_target(attacker) or target
        _bridge.attack_combo(b, att, target.order if target else dfn, skill_id, rng=rng)
        b.spend_skill(att, slot)
        b.end_turn()
    return 0, cap, _hp_left(b, start_hp)          # 0 = draw on the turn cap


def _hp_left(b, start_hp):
    return {t: sum(max(0, u.hp) for u in b.units.values() if u.team == t)
            / max(1, start_hp[t]) for t in (1, 2)}


# --- matches -------------------------------------------------------------------------

def _score(winner, side):
    """Chess scoring: a win is 1, a draw 0.5, a loss 0. Draws are real here -- two equal
    teams under two equal choosers can hit the turn cap -- and discarding them would
    inflate whichever chooser happens to stall more."""
    if winner == 0:
        return 0.5
    return 1.0 if winner == side else 0.0


def mirror_match(a, b_, teams, seeds, level, verbose=False):
    """Play A against B on every (team, seed), in BOTH orientations. -> A's scores."""
    scores, turns, draws = [], [], 0
    for team in teams:
        for seed in seeds:
            for a_side in (1, 2):
                # Same team, same seed, sides swapped: whatever advantage acting first
                # confers is handed to each chooser exactly once.
                chooser = {a_side: CHOOSERS[a], 3 - a_side: CHOOSERS[b_]}
                fight = mirror_battle(team, level)
                winner, n, _hp = run_fight(fight, chooser, seed)
                scores.append(_score(winner, a_side))
                turns.append(n)
                draws += (winner == 0)
    if verbose:
        print(f"    {len(scores)} fights, {draws} draws, "
              f"median {statistics.median(turns):.0f} turns")
    return scores


def stage_match(a, b_, stages, roster, seeds, level, hp_scale):
    """Asymmetric check: the PARTY chooser varies, the enemy chooser is held fixed.

    Mirror mode measures the scorer on a symmetric field; this measures what players
    actually meet -- a party of casts against a stage's mobs. It is the weaker instrument
    (no orientation swap can cancel a stage's own balance) and it is only meaningful where
    the fight is both contested AND finishable, which `calibrate` is what enforces.
    """
    out = {}
    for stage in stages:
        rows = {}
        for name in (a, b_):
            fights = []
            for seed in seeds:
                fight = stage_battle(stage, roster, level, hp_scale)
                chooser = {bt.TEAM_PLAYER: CHOOSERS[name],
                           bt.TEAM_ENEMY: CHOOSERS["tier1-enemy"]}
                winner, turns, hp = run_fight(fight, chooser, seed)
                # Draws score 0.5, exactly as in mirror mode. Counting a turn-cap draw as
                # a LOSS is what made the first version of this report a difference that
                # was not there: at a scale where every fight stalls, the winner is
                # whoever happened to finish just inside the cap, while the two choosers'
                # damage per turn differed by 0.4%.
                fights.append((_score(winner, bt.TEAM_PLAYER), winner, turns, hp[1]))
            rows[name] = fights
        out[stage] = rows
    return out


def calibrate(stage, roster, level, seeds, reference="greedy", lo=1, hi=4096,
              max_draws=0.25, max_turn_frac=0.8):
    """-> (enemy HP multiplier where `reference` is near 50%, or None with the reason).

    A stage the party always wins or always loses cannot tell two choosers apart; the
    measurement is only sensitive near 50%. Binary search on HP, the one knob that
    lengthens a fight without changing what anybody's skills DO.

    Two rejections, both learned the hard way:

      * **A stalled fight is not a contested one.** Pushing HP up far enough always drives
        the win rate toward 50%, but it gets there by making the fight unfinishable -- and
        then the result is decided by which fights happened to land just inside the turn
        cap. That is how this tool first reported a 2:1 gap between choosers whose damage
        per turn was within half a percent. A scale is rejected if a quarter of its fights
        draw, or if the median fight runs past `max_turn_frac` of the cap.
      * **The last midpoint is not the best midpoint.** Every acceptable scale is kept and
        the one CLOSEST to 50% is returned; taking whatever the search happened to end on
        once produced a "calibrated" scale at which the reference lost every fight.
    """
    def measure(scale):
        wins = draws = 0
        turns = []
        for seed in seeds:
            fight = stage_battle(stage, roster, level, scale)
            chooser = {bt.TEAM_PLAYER: CHOOSERS[reference],
                       bt.TEAM_ENEMY: CHOOSERS["tier1-enemy"]}
            winner, n, _hp = run_fight(fight, chooser, seed)
            wins += (winner == bt.TEAM_PLAYER)
            draws += (winner == 0)
            turns.append(n)
        return wins / len(seeds), draws / len(seeds), statistics.median(turns)

    def usable(draws, med):
        return draws <= max_draws and med <= TURN_CAP * max_turn_frac

    lo_rate, lo_draws, lo_med = measure(lo)
    if lo_rate < 0.5 and usable(lo_draws, lo_med):
        return None, "unwinnable even against the weakest enemies"

    candidates = []
    if usable(lo_draws, lo_med):
        candidates.append((lo, lo_rate))
    for _ in range(12):
        mid = (lo + hi) // 2
        if mid in (lo, hi):
            break
        rate, draws, med = measure(mid)
        if not usable(draws, med):
            hi = mid                  # too tanky to finish: search DOWN, never accept it
            continue
        candidates.append((mid, rate))
        if rate > 0.5:
            lo = mid
        else:
            hi = mid
    if not candidates:
        return None, "no scale is both contested and finishable inside the turn cap"
    scale, rate = min(candidates, key=lambda c: abs(c[1] - 0.5))
    if rate in (0.0, 1.0):
        return None, (f"the closest scale still gives the reference "
                      f"{rate*100:.0f}% -- nothing here is contested")
    return scale, None


def calibrate_party_level(stage, team, seeds, reference="greedy", lo=1, hi=400,
                         max_turn_frac=0.8):
    """-> the party level at which `reference` clears this stage about half the time.

    Party level is the honest difficulty knob here, and better than the enemy-HP one used
    by stage mode: it is what the game itself gates progress with, it moves the party along
    the same growth curve the design pack defines (`char._growStar`), and it distorts no
    mechanic. Scaling the ENEMIES instead changes the shape of the fight; levelling the
    party changes only how ready it is for it.

    It also answers what "a party that would actually be playing at this level" means
    operationally: not a roster the pack recommends on paper, but one for which the stage
    is a real question. Players push content until it is hard; this finds that point.

    Levels alone under-represent a real account -- no gear, no limit breaks, no soul book
    -- so the calibrated level lands above what a player of that stage would show on their
    profile. That is fine for a CHOOSER comparison, which needs the fight contested and the
    two sides identical, not the roster screen to be plausible.
    """
    def rate(level):
        wins = 0
        turns = []
        for seed in seeds:
            fight = stage_battle(stage, list(team), level, 1)
            chooser = {bt.TEAM_PLAYER: CHOOSERS[reference],
                       bt.TEAM_ENEMY: CHOOSERS["tier1-enemy"]}
            winner, n, _hp = run_fight(fight, chooser, seed)
            wins += (winner == bt.TEAM_PLAYER)
            turns.append(n)
        return wins / len(seeds), statistics.median(turns)

    top, top_med = rate(hi)
    if top < 0.5 or top_med > TURN_CAP * max_turn_frac:
        return None                      # unclearable even at the level ceiling
    if rate(lo)[0] > 0.5:
        return lo                        # trivial at level 1; nothing to calibrate
    for _ in range(10):
        mid = (lo + hi) // 2
        if mid in (lo, hi):
            break
        if rate(mid)[0] > 0.5:
            hi = mid
        else:
            lo = mid
    return hi


def campaign_match(a, b_, stages, seeds, verbose=True, reference=None):
    """Multi-wave stages, each at the party level that makes it a genuine challenge.

    This is the regime real play occupies and the one the mirror match cannot speak to:
    several waves on one HP bar, with cooldowns, statuses and damage carried forward.

    Reported alongside win rate is WAVES REACHED, which is the more sensitive measure here
    -- two choosers that both lose a stage have not necessarily played it equally, and the
    one that died in wave 3 outplayed the one that died in wave 1.
    """
    rows = []
    for stage in stages:
        team, _packlv = level_appropriate_team(stage)
        waves = len(dd.csv_ints((dd.row("stage", stage) or {}).get("_mobGroup_datas")))
        level = calibrate_party_level(stage, team, seeds,
                                      reference=reference or b_)
        if level is None:
            if verbose:
                print(f"  {stage:>8} SKIPPED -- unclearable at any party level")
            continue
        per = {}
        for name in (a, b_):
            fights = []
            for seed in seeds:
                fight = stage_battle(stage, list(team), level, 1)
                chooser = {bt.TEAM_PLAYER: CHOOSERS[name],
                           bt.TEAM_ENEMY: CHOOSERS["tier1-enemy"]}
                winner, turns, hp = run_fight(fight, chooser, seed)
                fights.append((_score(winner, bt.TEAM_PLAYER), winner, turns, hp[1],
                               fight.wave))
            per[name] = fights
        changed = sum(1 for i in range(len(seeds))
                      if (per[a][i][1] == bt.TEAM_PLAYER)
                      != (per[b_][i][1] == bt.TEAM_PLAYER))
        rows.append((stage, level, waves, per, changed))
        if verbose:
            line = f"  {stage:>8} lv{level:<4} {waves}w"
            for name in (a, b_):
                f = per[name]
                line += (f"   {name}: {statistics.mean(x[0] for x in f)*100:5.1f}% "
                         f"(wave {statistics.mean(x[4] for x in f):.1f} "
                         f"hp{statistics.mean(x[3] for x in f)*100:4.0f}%)")
            line += f"   changed {changed}/{len(seeds)}"
            print(line)
    return rows


# --- statistics ----------------------------------------------------------------------

def summarise(scores):
    """-> (mean, half-width of the 95% CI). Normal approximation on the mean score, which
    handles draws at 0.5 without pretending the outcome is binomial."""
    n = len(scores)
    mean = statistics.mean(scores)
    if n < 2:
        return mean, float("inf")
    sd = statistics.pstdev(scores)
    return mean, 1.96 * sd / math.sqrt(n)


def verdict(mean, half):
    if half == float("inf"):
        return "not enough fights"
    if mean - half > 0.5:
        return "STRONGER"
    if mean + half < 0.5:
        return "WEAKER"
    return "no measurable difference"


# --- driver --------------------------------------------------------------------------

def sample_teams(pool, count, seed=0):
    ids = playable(pool)
    rng = random.Random(seed)
    return [tuple(rng.sample(ids, 5)) for _ in range(count)]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--mode", choices=("tournament", "mirror", "stage", "campaign"),
                    default="tournament")
    ap.add_argument("--entrants", default="tier1,greedy,random",
                    help="tournament mode: comma-separated choosers, round-robin")
    ap.add_argument("--a", default="tier1", choices=sorted(CHOOSERS))
    ap.add_argument("--b", default="greedy", choices=sorted(CHOOSERS))
    ap.add_argument("--null", metavar="CHOOSER",
                    help="only run the harness self-check: this chooser against itself")
    ap.add_argument("--teams", type=int, default=24)
    ap.add_argument("--seeds", type=int, default=6)
    ap.add_argument("--level", type=int, default=60)
    ap.add_argument("--pool", choices=("player", "mob", "mixed"), default="player")
    ap.add_argument("--stages", default="1101,1201,1600002,1000032,1000002")
    ap.add_argument("--reference", choices=sorted(CHOOSERS), default=None,
                    help="campaign mode: whose 50%% point sets the difficulty "
                         "(default: --b)")
    ap.add_argument("--campaign-stages", type=int, default=40,
                    help="campaign mode: how many multi-wave stages to sample")
    ap.add_argument("--skip-null", action="store_true",
                    help="report a comparison without checking the harness first")
    args = ap.parse_args()

    seeds = list(range(args.seeds))
    teams = sample_teams(args.pool, args.teams)
    print(f"pool={args.pool}  teams={len(teams)}  seeds={len(seeds)}  level={args.level}")

    if args.null:
        print(f"\nnull test: {args.null} vs itself")
        scores = mirror_match(args.null, args.null, teams, seeds, args.level, verbose=True)
        mean, half = summarise(scores)
        ok = abs(mean - 0.5) <= half
        print(f"  {mean*100:5.1f}% +/- {half*100:.1f}  "
              f"({'OK -- no side bias' if ok else 'BIASED -- do not trust comparisons'})")
        return 0 if ok else 1

    if args.mode == "campaign":
        stages = multiwave_stages(args.campaign_stages)
        print(f"\ncampaign mode: {len(stages)} multi-wave stages, party level from the "
              f"pack's own `_stagelv`,\n  enemies fixed at tier1-enemy, full wave "
              f"progression on one HP bar")
        print(f"  difficulty calibrated so that `{args.reference or args.b}` is near 50%")
        rows = campaign_match(args.a, args.b, stages, seeds,
                              reference=args.reference)
        for name in (args.a, args.b):
            scores = [f[0] for r in rows for f in r[3][name]]
            mean, half = summarise(scores)
            print(f"\n  {name:12s} clears {mean*100:5.1f}% +/- {half*100:4.1f}  "
                  f"({len(scores)} fights)")
        a_s = [f[0] for r in rows for f in r[3][args.a]]
        b_s = [f[0] for r in rows for f in r[3][args.b]]
        diff = statistics.mean(a_s) - statistics.mean(b_s)
        changed = sum(r[4] for r in rows)
        total = sum(len(seeds) for _ in rows)
        print(f"  {args.a} - {args.b} = {diff*100:+.1f} points")
        print(f"  the chooser changed the outcome in {changed}/{total} fights "
              f"({changed/max(1,total)*100:.1f}%)")
        for name in (args.a, args.b):
            w = [f[4] for r in rows for f in r[3][name]]
            print(f"  {name:12s} mean wave reached {statistics.mean(w):.2f}")
        contested = sum(1 for r in rows
                        if 0 < statistics.mean(f[0] for f in r[3][args.b]) < 1)
        print(f"  stages that were actually contested (reference neither 0% nor 100%): "
              f"{contested}/{len(rows)}")
        return 0

    if args.mode == "tournament":
        entrants = [e.strip() for e in args.entrants.split(",") if e.strip()]
        bad = [e for e in entrants if e not in CHOOSERS]
        if bad:
            print(f"unknown chooser(s): {bad}; known: {sorted(CHOOSERS)}")
            return 2
        print(f"\nround-robin, mirror teams, both orientations")
        table = {}
        for i, a in enumerate(entrants):
            for b_ in entrants[i + 1:]:
                scores = mirror_match(a, b_, teams, seeds, args.level)
                table[(a, b_)] = summarise(scores)
        width = max(len(e) for e in entrants) + 1
        print(f"\n  {'':{width}} {'vs':{width}}   score        verdict")
        for (a, b_), (mean, half) in table.items():
            print(f"  {a:{width}} {b_:{width}}  {mean*100:5.1f}% +/- {half*100:4.1f}   "
                  f"{verdict(mean, half)}")
        anchored = [e for e in entrants if e != "random" and ("random" in entrants)]
        if anchored:
            print("\n  Read the gap to `random` as the scale bar: if a chooser barely "
                  "beats it,\n  the metric cannot resolve the gap between the real ones "
                  "either.")
        return 0

    if args.mode == "mirror":
        if not args.skip_null:
            print(f"\nnull test: {args.a} vs itself (the harness must not favour a side)")
            null = mirror_match(args.a, args.a, teams, seeds, args.level, verbose=True)
            nmean, nhalf = summarise(null)
            print(f"  {nmean*100:5.1f}% +/- {nhalf*100:.1f}")
            if abs(nmean - 0.5) > nhalf:
                print("  BIASED. A comparison from this suite would be an artifact, "
                      "which is exactly the mistake this check exists to catch.")
                return 1
            print("  OK -- the orientation swap cancels the first-mover advantage.")

        print(f"\nmirror match: {args.a} vs {args.b}")
        scores = mirror_match(args.a, args.b, teams, seeds, args.level, verbose=True)
        mean, half = summarise(scores)
        print(f"  {args.a} scores {mean*100:5.1f}% +/- {half*100:.1f}  "
              f"-> {verdict(mean, half)}")
        return 0

    # stage mode
    roster = list(sample_teams("player", 1, seed=7)[0])
    stages = [int(x) for x in args.stages.split(",") if x.strip()]
    print("\nstage mode: party chooser varies, enemies fixed at tier1-enemy")
    print(f"  (draws at the {TURN_CAP}-turn cap score 0.5, and a scale that stalls is "
          f"rejected outright)")
    for stage in stages:
        scale, why = calibrate(stage, roster, args.level, seeds)
        if scale is None:
            print(f"  {stage}: SKIPPED -- {why}")
            continue
        rows = stage_match(args.a, args.b, [stage], roster, seeds, args.level,
                           scale)[stage]
        print(f"  {stage}: enemy HP x{scale}")
        for name in (args.a, args.b):
            fights = rows[name]
            mean, half = summarise([f[0] for f in fights])
            wins = sum(f[1] == bt.TEAM_PLAYER for f in fights)
            draws = sum(f[1] == 0 for f in fights)
            turns = statistics.median([f[2] for f in fights])
            hp = statistics.mean([f[3] for f in fights])
            print(f"      {name:12s} score {mean*100:5.1f}% +/- {half*100:4.1f}   "
                  f"W{wins}/D{draws}/L{len(fights)-wins-draws}   "
                  f"median {turns:3.0f} turns   {hp*100:4.1f}% party HP left")
    return 0


if __name__ == "__main__":
    sys.exit(main())

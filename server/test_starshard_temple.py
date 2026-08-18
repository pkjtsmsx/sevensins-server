#!/usr/bin/env python3
"""Starshard Temple drops: the day rotation and the per-floor drop ladder.

Reported from play 2026-08-18: "not getting certain starshards because the drops are
only chaos, hawkeye, slayer and defender", and separately that our drop table layout
does not match the live game's.

The first turned out NOT to be a bug. The in-game "Starshard Set" banner states the
rotation outright -- MON/WED/FRI Fortitude/Nightshade/Mystery/Devotee, TUE/THU/SAT/SUN
Defender/Chaos/Hawkeye/Slayer -- and those four ARE the Tue-Sun half. Pinned here so
the correct behaviour is not "fixed" away.

The second was real. `_itemrank_str` is read through `_FIELD_ITEMRANK_STR = "box_rank"`
(dump.cs:497328) and carries a per-floor `<max>,<min>` band as `star*10 + rank` codes.
An earlier version dismissed it as dead data -- `DesignStageRow` has no accessor -- and
invented a ladder instead. But the client not reading it is exactly what you expect of a
SERVER-side drop table: it is the surviving record, not junk. Consequences of the old
ladder that these checks now forbid: ★6 shards could never drop at all, and rarity was
one flat table on all 41 floors.

    python3 test_starshard_temple.py
"""
import collections
import datetime
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import battle as bt                                     # noqa: E402

_fail = 0
MON = datetime.date(2026, 8, 17)
TUE = datetime.date(2026, 8, 18)
WED = datetime.date(2026, 8, 19)
SUN = datetime.date(2026, 8, 23)


def check(name, cond, detail=""):
    global _fail
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        _fail += 1


def names(elements):
    return [bt.STARSHARD_SET_NAMES[e] for e in elements]


def check_day_rotation():
    """Straight off the banner. This is NOT the bug -- do not 'fix' it."""
    mwf = {"Endearment", "Nightshade", "Mystery", "Devotee"}   # Endearment = Fortitude
    ttss = {"Defender", "Chaos", "Hawkeye", "Slayer"}
    for day, want, label in ((MON, mwf, "Mon"), (WED, mwf, "Wed"),
                             (TUE, ttss, "Tue"), (SUN, ttss, "Sun")):
        got = set(names(bt.starshard_sets_for_day(day)))
        check(f"{label} offers the right four", got == want, str(sorted(got)))
    check("the two halves are disjoint", not (mwf & ttss))
    check("and together cover all eight sets",
          mwf | ttss == set(bt.STARSHARD_SET_NAMES.values()),
          str(sorted(set(bt.STARSHARD_SET_NAMES.values()) - (mwf | ttss))))
    # The reported symptom, restated as the expected behaviour.
    check("the reported four ARE the Tue/Thu/Sat/Sun half",
          set(names(bt.starshard_sets_for_day(TUE)))
          == {"Chaos", "Hawkeye", "Slayer", "Defender"})


def check_box_rank_decode():
    rows = {r.get("_title_en"): r for r in bt.dd.rows("stage").values()
            if r.get("_book") == bt.STARSHARD_BOOK}
    check("the Temple is 41 floors", len(rows) == 41, str(len(rows)))
    check("every floor carries a box_rank",
          all(r.get("_itemrank_str") for r in rows.values()))

    # The ladder the data spells out, spot-checked at each band edge.
    want = {"ST-1": ((1, 3), (1, 1)), "ST-16": ((1, 3), (1, 4)),
            "ST-21": ((1, 4), (1, 1)), "ST-32": ((1, 4), (1, 4)),
            "ST-38": ((1, 5), (1, 2)), "ST-41": ((1, 6), (1, 4))}
    for title, expect in want.items():
        sid = rows[title]["_id"]
        check(f"{title} decodes to stars {expect[0]} ranks {expect[1]}",
              bt._box_rank_bounds(sid) == expect, str(bt._box_rank_bounds(sid)))

    # ST-41's max rank digit is 0, which is out of range; it must read as "no cap",
    # never as "rank 0 only" -- that would make the last floor worse than ST-40.
    st41, st40 = rows["ST-41"]["_id"], rows["ST-40"]["_id"]
    check("ST-41's rank digit 0 means no cap, not rank 0",
          bt._box_rank_bounds(st41)[1][1] == bt.STARSHARD_MAX_RANK,
          str(bt._box_rank_bounds(st41)))
    check("  ...so the last floor is not a downgrade from ST-40",
          bt._box_rank_bounds(st41)[0][1] > bt._box_rank_bounds(st40)[0][1])


def check_ladder_is_monotone():
    """The ceiling (star, rank) read lexicographically must never go backwards.

    That ordering is the whole reason the decode is believable: the rank digit RESETS
    to 1 each time the star digit steps up (ST-16 is ★3/LR, ST-21 is ★4/R), which is
    incoherent as two independent numbers and exactly right as "each star band re-walks
    the rarity ladder".
    """
    floors = sorted((r.get("_sort"), r["_id"]) for r in bt.dd.rows("stage").values()
                    if r.get("_book") == bt.STARSHARD_BOOK)
    ceilings = [(bt._box_rank_bounds(sid)[0][1], bt._box_rank_bounds(sid)[1][1])
                for _sort, sid in floors]
    regressions = [(i + 1, ceilings[i], ceilings[i + 1])
                   for i in range(len(ceilings) - 1) if ceilings[i + 1] < ceilings[i]]
    check("the ceiling never regresses across all 41 floors",
          not regressions, str(regressions[:3]))
    check("  ...and it does actually climb", ceilings[0] < ceilings[-1],
          f"{ceilings[0]} -> {ceilings[-1]}")

    first, last = bt._box_rank_bounds(floors[0][1]), bt._box_rank_bounds(floors[-1][1])
    check("ST-1's star ceiling is ★3, not ★1", first[0][1] == 3, str(first))
    check("ST-41's star ceiling is ★6", last[0][1] == 6, str(last))
    check("★6 is reachable at all (the old ladder capped at ★5)",
          bt.STARSHARD_MAX_STAR >= 6, str(bt.STARSHARD_MAX_STAR))


def check_drops_respect_the_band():
    """Nothing may drop outside its floor's band, and nothing may vanish."""
    bad, empty = [], 0
    for sort in range(1, 42):
        sid = 1600000 + sort
        (ls, hs), (lr, hr) = bt._box_rank_bounds(sid)
        rng = random.Random(sort)
        for day in (MON, TUE):
            allowed = set(bt.starshard_sets_for_day(day))
            for _ in range(120):
                drops = bt.starshard_temple_drops(sid, rng=rng, when=day)
                if not drops:
                    empty += 1
                for d in drops:
                    i = d.item_id
                    star, rank, slot, el = i % 10, (i // 10) % 10, (i // 100) % 10, i // 1000
                    if not (ls <= star <= hs and lr <= rank <= hr
                            and el in allowed and slot in bt.STARSHARD_TEMPLE_SLOTS):
                        bad.append((sort, i))
    check("no drop escapes its floor's star/rank band or the day's sets",
          not bad, str(bad[:5]))
    # A Temple clear with zero shards HANGS the client (PanelBattleRuneResult is driven
    # entirely by the rune list), so this is not a cosmetic check.
    check("no clear ever pays zero shards", empty == 0, str(empty))
    check("a clear pays the live count", bt.STARSHARD_DROPS_PER_CLEAR == 2)


def check_depth_actually_matters():
    """The old table gave the same rarity odds on every floor -- the specific thing
    reported as not matching the live game."""
    def dist(sid, n=6000):
        rng = random.Random(9)
        stars, ranks = collections.Counter(), collections.Counter()
        for _ in range(n):
            for d in bt.starshard_temple_drops(sid, rng=rng, when=TUE):
                stars[d.item_id % 10] += 1
                ranks[(d.item_id // 10) % 10] += 1
        return stars, ranks

    s1, r1 = dist(1600001)
    s41, r41 = dist(1600041)
    check("a deep floor pays better stars on average",
          sum(k * v for k, v in s41.items()) / sum(s41.values())
          > sum(k * v for k, v in s1.items()) / sum(s1.values()))
    check("  ...and better rarity, which used to be flat",
          sum(k * v for k, v in r41.items()) / sum(r41.values())
          > sum(k * v for k, v in r1.items()) / sum(r1.values()),
          f"{r1} vs {r41}")
    check("ST-1 cannot pay ★4+", max(s1) <= 3, str(sorted(s1)))
    check("ST-41 can pay ★6", 6 in s41, str(sorted(s41)))
    check("ST-1 cannot pay above R", max(r1) <= 1, str(sorted(r1)))
    check("ST-41 can pay LR", 4 in r41, str(sorted(r41)))


def check_every_rolled_id_exists():
    """A missing id is silently skipped by the roller, quietly shrinking a clear."""
    missing = []
    for sort in range(1, 42):
        sid = 1600000 + sort
        (ls, hs), (lr, hr) = bt._box_rank_bounds(sid)
        for sets in (bt.STARSHARD_SETS_MWF, bt.STARSHARD_SETS_TTSS):
            for el in sets:
                for slot in bt.STARSHARD_TEMPLE_SLOTS:
                    for star in range(ls, hs + 1):
                        for rank in range(lr, hr + 1):
                            iid = el * 1000 + slot * 100 + rank * 10 + star
                            if not bt.dd.row("item", iid):
                                missing.append(iid)
    check("every id the bands can roll exists as an item",
          not missing, f"{len(missing)} missing, e.g. {missing[:5]}")


def check_preview():
    for sid in (1600001, 1600041):
        pool = bt.starshard_temple_pool(sid, when=TUE)
        check(f"stage {sid} previews a non-empty pool", bool(pool), str(pool))
        band = sorted({st for _w, st in bt.starshard_star_weights(sid)})
        check(f"  ...covering the WHOLE band, not a truncated top slice",
              len(pool) == len(band) * 4, f"{len(pool)} vs {len(band)}x4")
    check("a non-Temple stage previews nothing",
          bt.starshard_temple_pool(1101) == [])


def main():
    for fn in (check_day_rotation, check_box_rank_decode, check_ladder_is_monotone,
               check_drops_respect_the_band, check_depth_actually_matters,
               check_every_rolled_id_exists, check_preview):
        print(f"\n{fn.__name__}:")
        fn()
    print(f"\n{_fail} failure(s)")
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(main())

"""Recovered PURSUIT damage -- the figure a follow-up hits for.

A pursuit compiles to `follow_up` naming a sub-skill, and the damage figure is stated by
the PARENT: the sub-skill's own row is usually a bare animation stub. `core.execute`
already passes the parent's figure down as `coefficient_override` -- when the compiler
found one. It does not always: 608 parent skills reach a pursuit with a coefficient in
NEITHER place (measured), so the pursuit plays its animation and lands a 0, and a cast
built on pursuits silently loses a whole attack per turn.

Contributed as a 488-row table, re-derived here rather than taken on trust, and split by
WHERE each row's evidence comes from -- the project's tier order is Chinese prose, then
English, then a stated house default (CLAUDE.md sections 1 and 3):

  PURSUIT               433  figure AND stat both stated in Chinese.
  PURSUIT_BY_SUB         12  same, but keyed by sub-skill: six parents state two
                             different pursuits and one figure cannot serve both.
  PURSUIT_STAT_FROM_EN   10  figure in Chinese, stat only in the English.
  PURSUIT_STAT_INVENTED  19  figure in Chinese, stat in neither -- ATK by design choice.
  PURSUIT_NAMED_SKILL    20  Chinese names a SKILL rather than a number (Satan's Bankai
                             "uses Normal Attack IV once"); the figure is that skill's own.

The Chinese rows are verified mechanically against `skill._note1`: the figure must appear
in a clause carrying a pursuit marker, and that clause must name the stat. Nothing here is
a guess about a FIGURE -- every figure is the game's own. The only invented thing in this
file is the stat on 19 rows, and it is in its own table so a reader can see exactly which.

Getting the figure out of the prose needs care, because every obvious rule produces a
plausible WRONG number:

  * NOT the nearest percentage. Uriel's Prelude states 65% ATK + 65% DEF for the main hit
    and then pursues for 40% DEF -- the pursuit is the 40 and the nearest number is 65.
  * NOT every after-the-action clause. Lucifer's Fallen Star recovers 30% of his own max
    HP right beside his pursuit; that 30 is a HEAL, and the pursuit is the 200% ATK.
  * The pursuit marker is not always present. The Supreme writes the same mechanic as
    "after the attack action ends, deal 180% ATK in 2 hits to the highest-HP enemy".

The BASIS travels with the figure: a DEF pursuit read as ATK is wrong by the gap between
those two stats on that cast.

This table is a RECONSTRUCTION, not a design choice -- every entry is the game's own
stated number. Re-audit it with tools/verify_pursuit_values.py, which re-derives every
row from the pack and fails on any that no longer checks out.
"""

# parent skill id -> (coefficient, basis) for its pursuit.
# 299 ATK, 131 DEF, 9 MAX_HP.
PURSUIT = {
    1000131: (1.8, 'ATK'),
    1000132: (1.8, 'ATK'),
    1000133: (1.8, 'ATK'),
    1000134: (1.8, 'ATK'),
    1000135: (1.8, 'ATK'),
    1000136: (1.8, 'ATK'),
    1003106: (0.5, 'DEF'),
    1013101: (0.4, 'DEF'),
    1013102: (0.4, 'DEF'),
    1013103: (0.4, 'DEF'),
    1013104: (0.4, 'DEF'),
    1013105: (0.4, 'DEF'),
    1013106: (0.4, 'DEF'),
    1013121: (0.75, 'DEF'),
    1013122: (0.75, 'DEF'),
    1013123: (0.75, 'DEF'),
    1013124: (0.75, 'DEF'),
    1013125: (0.75, 'DEF'),
    1013126: (0.75, 'DEF'),
    1040101: (1.8, 'DEF'),
    1040102: (1.8, 'DEF'),
    1040103: (1.8, 'DEF'),
    1040104: (2.0, 'DEF'),
    1040105: (2.0, 'DEF'),
    1040106: (2.0, 'DEF'),
    1040131: (1.8, 'DEF'),
    1040132: (1.8, 'DEF'),
    1040133: (1.8, 'DEF'),
    1040134: (1.8, 'DEF'),
    1040135: (1.8, 'DEF'),
    1040136: (1.8, 'DEF'),
    1042131: (1.25, 'ATK'),
    1042132: (1.25, 'ATK'),
    1042133: (1.25, 'ATK'),
    1042134: (1.25, 'ATK'),
    1042135: (1.25, 'ATK'),
    1042136: (1.25, 'ATK'),
    1055111: (1.0, 'ATK'),
    1055112: (1.2, 'ATK'),
    1055113: (1.4, 'ATK'),
    1055114: (1.6, 'ATK'),
    1055115: (1.8, 'ATK'),
    1060121: (0.5, 'ATK'),
    1060122: (0.5, 'ATK'),
    1060123: (0.75, 'ATK'),
    1060124: (0.75, 'ATK'),
    1076101: (1.0, 'ATK'),
    1076102: (1.0, 'ATK'),
    1076103: (1.0, 'ATK'),
    1076104: (1.0, 'ATK'),
    1110101: (1.5, 'ATK'),
    1110102: (1.5, 'ATK'),
    1110103: (1.5, 'ATK'),
    1110104: (1.5, 'ATK'),
    1110111: (1.5, 'ATK'),
    1110112: (1.5, 'ATK'),
    1110113: (1.5, 'ATK'),
    1110114: (1.5, 'ATK'),
    1110121: (1.5, 'ATK'),
    1110122: (1.5, 'ATK'),
    1110123: (1.5, 'ATK'),
    1110124: (1.5, 'ATK'),
    1110125: (1.5, 'ATK'),
    1119111: (0.8, 'ATK'),
    1119112: (0.8, 'ATK'),
    1119113: (1.0, 'ATK'),
    1119114: (1.0, 'ATK'),
    1119176: (1.0, 'ATK'),
    2000121: (2.0, 'ATK'),
    2000122: (2.2, 'ATK'),
    2000123: (2.2, 'ATK'),
    2000124: (2.5, 'ATK'),
    2000125: (2.8, 'ATK'),
    2000126: (3.1, 'ATK'),
    2000131: (1.2, 'ATK'),
    2000132: (1.2, 'ATK'),
    2000133: (1.5, 'ATK'),
    2000134: (1.5, 'ATK'),
    2000135: (1.5, 'ATK'),
    2000136: (1.5, 'ATK'),
    2003111: (0.8, 'DEF'),
    2003112: (0.8, 'DEF'),
    2003113: (0.8, 'DEF'),
    2003114: (0.8, 'DEF'),
    2003115: (0.8, 'DEF'),
    2003116: (0.8, 'DEF'),
    2005101: (5.0, 'ATK'),
    2005102: (5.0, 'ATK'),
    2005103: (5.0, 'ATK'),
    2005104: (5.0, 'ATK'),
    2005105: (5.0, 'ATK'),
    2005106: (5.0, 'ATK'),
    2005131: (2.0, 'ATK'),
    2005132: (2.0, 'ATK'),
    2005133: (2.0, 'ATK'),
    2005134: (2.0, 'ATK'),
    2005135: (2.0, 'ATK'),
    2005136: (2.0, 'ATK'),
    2009121: (2.0, 'ATK'),
    2009122: (2.0, 'ATK'),
    2009123: (2.0, 'ATK'),
    2009124: (2.0, 'ATK'),
    2009125: (2.0, 'ATK'),
    2009126: (2.0, 'ATK'),
    2009131: (1.8, 'ATK'),
    2009132: (1.8, 'ATK'),
    2009133: (1.8, 'ATK'),
    2009134: (1.8, 'ATK'),
    2009135: (1.8, 'ATK'),
    2011121: (1.5, 'ATK'),
    2011122: (1.5, 'ATK'),
    2011123: (1.5, 'ATK'),
    2011124: (1.5, 'ATK'),
    2011125: (1.5, 'ATK'),
    2011126: (1.5, 'ATK'),
    2024111: (1.5, 'ATK'),
    2024112: (1.5, 'ATK'),
    2024113: (1.5, 'ATK'),
    2024114: (1.5, 'ATK'),
    2024115: (1.5, 'ATK'),
    2024116: (1.5, 'ATK'),
    2039121: (3.3, 'ATK'),
    2039122: (3.6, 'ATK'),
    2039123: (3.9, 'ATK'),
    2039124: (4.2, 'ATK'),
    2039125: (4.5, 'ATK'),
    2039126: (4.8, 'ATK'),
    2043131: (1.0, 'ATK'),
    2043132: (1.0, 'ATK'),
    2043133: (1.0, 'ATK'),
    2043134: (1.0, 'ATK'),
    2043135: (1.0, 'ATK'),
    2043136: (1.0, 'ATK'),
    2089111: (0.05, 'MAX_HP'),
    2089112: (0.05, 'MAX_HP'),
    2089113: (0.1, 'MAX_HP'),
    2089114: (0.1, 'MAX_HP'),
    2089115: (0.15, 'MAX_HP'),
    2089116: (0.15, 'MAX_HP'),
    7003642: (0.05, 'MAX_HP'),
    7003646: (0.1, 'MAX_HP'),
    7003650: (0.15, 'MAX_HP'),
    100001601: (0.4, 'DEF'),
    100001602: (0.4, 'DEF'),
    100001603: (0.4, 'DEF'),
    100001604: (0.4, 'DEF'),
    100001621: (0.75, 'DEF'),
    100001622: (0.75, 'DEF'),
    100001623: (0.75, 'DEF'),
    100001624: (0.75, 'DEF'),
    130000014: (1.8, 'ATK'),
    130000082: (1.0, 'ATK'),
    130000114: (1.25, 'ATK'),
    130000161: (5.0, 'ATK'),
    130000164: (2.0, 'ATK'),
    130000223: (2.0, 'ATK'),
    130000224: (1.8, 'ATK'),
    130000281: (0.4, 'DEF'),
    130000283: (0.75, 'DEF'),
    130000331: (2.0, 'DEF'),
    130000334: (1.8, 'DEF'),
    130000352: (0.8, 'DEF'),
    130000394: (1.0, 'ATK'),
    130000433: (2.5, 'ATK'),
    130000434: (1.5, 'ATK'),
    130000483: (1.5, 'ATK'),
    130000612: (1.5, 'ATK'),
    130000893: (4.2, 'ATK'),
    151000521: (2.0, 'ATK'),
    151000522: (2.0, 'ATK'),
    151000523: (2.0, 'ATK'),
    151000524: (2.0, 'ATK'),
    151000525: (2.0, 'ATK'),
    151000526: (2.0, 'ATK'),
    151000621: (2.0, 'ATK'),
    151000622: (2.2, 'ATK'),
    151000623: (2.2, 'ATK'),
    151000624: (2.5, 'ATK'),
    151000625: (2.8, 'ATK'),
    151000626: (3.1, 'ATK'),
    151001321: (1.5, 'ATK'),
    151001322: (1.5, 'ATK'),
    151001323: (1.5, 'ATK'),
    151001324: (1.5, 'ATK'),
    151001325: (1.5, 'ATK'),
    151001326: (1.5, 'ATK'),
    151001801: (1.8, 'DEF'),
    151001802: (1.8, 'DEF'),
    151001803: (1.8, 'DEF'),
    151001804: (2.0, 'DEF'),
    151001805: (2.0, 'DEF'),
    151001806: (2.0, 'DEF'),
    151002501: (5.0, 'ATK'),
    151002502: (5.0, 'ATK'),
    151002503: (5.0, 'ATK'),
    151002504: (5.0, 'ATK'),
    151002505: (5.0, 'ATK'),
    151002506: (5.0, 'ATK'),
    151003921: (3.3, 'ATK'),
    151003922: (3.6, 'ATK'),
    151003923: (3.9, 'ATK'),
    151003924: (4.2, 'ATK'),
    151003925: (4.2, 'ATK'),
    151003926: (4.2, 'ATK'),
    151004801: (0.4, 'DEF'),
    151004802: (0.4, 'DEF'),
    151004803: (0.4, 'DEF'),
    151004804: (0.4, 'DEF'),
    151004805: (0.4, 'DEF'),
    151004806: (0.4, 'DEF'),
    151004821: (0.75, 'DEF'),
    151004822: (0.75, 'DEF'),
    151004823: (0.75, 'DEF'),
    151004824: (0.75, 'DEF'),
    151004825: (0.75, 'DEF'),
    151004826: (0.75, 'DEF'),
    151005411: (1.5, 'ATK'),
    151005412: (1.5, 'ATK'),
    151005413: (1.5, 'ATK'),
    151005414: (1.5, 'ATK'),
    151005415: (1.5, 'ATK'),
    151005416: (1.5, 'ATK'),
    151005606: (0.5, 'DEF'),
    151005711: (0.8, 'DEF'),
    151005712: (0.8, 'DEF'),
    151005713: (0.8, 'DEF'),
    151005714: (0.8, 'DEF'),
    151005715: (0.8, 'DEF'),
    151005716: (0.8, 'DEF'),
    152000431: (1.8, 'ATK'),
    152000432: (1.8, 'ATK'),
    152000433: (1.8, 'ATK'),
    152000434: (1.8, 'ATK'),
    152000435: (1.8, 'ATK'),
    152000436: (1.8, 'ATK'),
    152000521: (2.0, 'ATK'),
    152000522: (2.0, 'ATK'),
    152000523: (2.0, 'ATK'),
    152000524: (2.0, 'ATK'),
    152000525: (2.0, 'ATK'),
    152000526: (2.0, 'ATK'),
    152000531: (1.8, 'ATK'),
    152000532: (1.8, 'ATK'),
    152000533: (1.8, 'ATK'),
    152000534: (1.8, 'ATK'),
    152000535: (1.8, 'ATK'),
    152000621: (2.2, 'ATK'),
    152000622: (2.4, 'ATK'),
    152000623: (2.4, 'ATK'),
    152000624: (2.7, 'ATK'),
    152000625: (3.0, 'ATK'),
    152000626: (3.3, 'ATK'),
    152000631: (1.2, 'ATK'),
    152000632: (1.2, 'ATK'),
    152000633: (1.5, 'ATK'),
    152000634: (1.5, 'ATK'),
    152000635: (1.5, 'ATK'),
    152000636: (1.5, 'ATK'),
    152001321: (1.5, 'ATK'),
    152001322: (1.5, 'ATK'),
    152001323: (1.5, 'ATK'),
    152001324: (1.5, 'ATK'),
    152001325: (1.5, 'ATK'),
    152001326: (1.5, 'ATK'),
    152001801: (1.8, 'DEF'),
    152001802: (1.8, 'DEF'),
    152001803: (1.8, 'DEF'),
    152001804: (2.0, 'DEF'),
    152001805: (2.0, 'DEF'),
    152001806: (2.0, 'DEF'),
    152001831: (1.8, 'DEF'),
    152001832: (1.8, 'DEF'),
    152001833: (1.8, 'DEF'),
    152001834: (1.8, 'DEF'),
    152001835: (1.8, 'DEF'),
    152001836: (1.8, 'DEF'),
    152001931: (1.25, 'ATK'),
    152001932: (1.25, 'ATK'),
    152001933: (1.25, 'ATK'),
    152001934: (1.25, 'ATK'),
    152001935: (1.25, 'ATK'),
    152001936: (1.25, 'ATK'),
    152002501: (5.0, 'ATK'),
    152002502: (5.0, 'ATK'),
    152002503: (5.0, 'ATK'),
    152002504: (5.0, 'ATK'),
    152002505: (5.0, 'ATK'),
    152002506: (5.0, 'ATK'),
    152002531: (2.0, 'ATK'),
    152002532: (2.0, 'ATK'),
    152002533: (2.0, 'ATK'),
    152002534: (2.0, 'ATK'),
    152002535: (2.0, 'ATK'),
    152002536: (2.0, 'ATK'),
    152003921: (3.3, 'ATK'),
    152003922: (3.6, 'ATK'),
    152003923: (3.9, 'ATK'),
    152003924: (4.2, 'ATK'),
    152003925: (4.2, 'ATK'),
    152003926: (4.2, 'ATK'),
    152004801: (0.4, 'DEF'),
    152004802: (0.4, 'DEF'),
    152004803: (0.4, 'DEF'),
    152004804: (0.4, 'DEF'),
    152004805: (0.4, 'DEF'),
    152004806: (0.4, 'DEF'),
    152004821: (1.5, 'DEF'),
    152004822: (1.5, 'DEF'),
    152004823: (1.5, 'DEF'),
    152004824: (1.5, 'DEF'),
    152004825: (1.5, 'DEF'),
    152004826: (1.5, 'DEF'),
    152005411: (1.5, 'ATK'),
    152005412: (1.5, 'ATK'),
    152005413: (1.5, 'ATK'),
    152005414: (1.5, 'ATK'),
    152005415: (1.5, 'ATK'),
    152005416: (1.5, 'ATK'),
    152005606: (0.5, 'DEF'),
    152005711: (0.8, 'DEF'),
    152005712: (0.8, 'DEF'),
    152005713: (0.8, 'DEF'),
    152005714: (0.8, 'DEF'),
    152005715: (0.8, 'DEF'),
    152005716: (0.8, 'DEF'),
    152006131: (1.0, 'ATK'),
    152006132: (1.0, 'ATK'),
    152006133: (1.0, 'ATK'),
    152006134: (1.0, 'ATK'),
    152006135: (1.0, 'ATK'),
    152006136: (1.0, 'ATK'),
    153000431: (1.8, 'ATK'),
    153000432: (1.8, 'ATK'),
    153000433: (1.8, 'ATK'),
    153000434: (1.8, 'ATK'),
    153000435: (1.8, 'ATK'),
    153000436: (1.8, 'ATK'),
    153000521: (2.0, 'ATK'),
    153000522: (2.0, 'ATK'),
    153000523: (2.0, 'ATK'),
    153000524: (2.0, 'ATK'),
    153000525: (2.0, 'ATK'),
    153000526: (2.0, 'ATK'),
    153000531: (1.8, 'ATK'),
    153000532: (1.8, 'ATK'),
    153000533: (1.8, 'ATK'),
    153000534: (1.8, 'ATK'),
    153000535: (1.8, 'ATK'),
    153000621: (2.4, 'ATK'),
    153000622: (2.6, 'ATK'),
    153000623: (2.6, 'ATK'),
    153000624: (2.9, 'ATK'),
    153000625: (3.2, 'ATK'),
    153000626: (3.5, 'ATK'),
    153000631: (1.2, 'ATK'),
    153000632: (1.2, 'ATK'),
    153000633: (1.5, 'ATK'),
    153000634: (1.5, 'ATK'),
    153000635: (1.5, 'ATK'),
    153000636: (1.5, 'ATK'),
    153001321: (1.5, 'ATK'),
    153001322: (1.5, 'ATK'),
    153001323: (1.5, 'ATK'),
    153001324: (1.5, 'ATK'),
    153001325: (1.5, 'ATK'),
    153001326: (1.5, 'ATK'),
    153001801: (1.8, 'DEF'),
    153001802: (1.8, 'DEF'),
    153001803: (1.8, 'DEF'),
    153001804: (2.0, 'DEF'),
    153001805: (2.0, 'DEF'),
    153001806: (2.0, 'DEF'),
    153001831: (1.8, 'DEF'),
    153001832: (1.8, 'DEF'),
    153001833: (1.8, 'DEF'),
    153001834: (1.8, 'DEF'),
    153001835: (1.8, 'DEF'),
    153001836: (1.8, 'DEF'),
    153001931: (1.25, 'ATK'),
    153001932: (1.25, 'ATK'),
    153001933: (1.25, 'ATK'),
    153001934: (1.25, 'ATK'),
    153001935: (1.25, 'ATK'),
    153001936: (1.25, 'ATK'),
    153002501: (5.0, 'ATK'),
    153002502: (5.0, 'ATK'),
    153002503: (5.0, 'ATK'),
    153002504: (5.0, 'ATK'),
    153002505: (5.0, 'ATK'),
    153002506: (5.0, 'ATK'),
    153002531: (2.0, 'ATK'),
    153002532: (2.0, 'ATK'),
    153002533: (2.0, 'ATK'),
    153002534: (2.0, 'ATK'),
    153002535: (2.0, 'ATK'),
    153002536: (2.0, 'ATK'),
    153003921: (3.3, 'ATK'),
    153003922: (3.6, 'ATK'),
    153003923: (3.9, 'ATK'),
    153003924: (4.2, 'ATK'),
    153003925: (4.2, 'ATK'),
    153003926: (4.2, 'ATK'),
    153004801: (0.4, 'DEF'),
    153004802: (0.4, 'DEF'),
    153004803: (0.4, 'DEF'),
    153004804: (0.4, 'DEF'),
    153004805: (0.4, 'DEF'),
    153004806: (0.4, 'DEF'),
    153004821: (2.0, 'DEF'),
    153004822: (2.0, 'DEF'),
    153004823: (2.0, 'DEF'),
    153004824: (2.0, 'DEF'),
    153004825: (2.0, 'DEF'),
    153004826: (2.0, 'DEF'),
    153005411: (1.5, 'ATK'),
    153005412: (1.5, 'ATK'),
    153005413: (1.5, 'ATK'),
    153005414: (1.5, 'ATK'),
    153005415: (1.5, 'ATK'),
    153005416: (1.5, 'ATK'),
    153005606: (0.5, 'DEF'),
    153005711: (0.8, 'DEF'),
    153005712: (0.8, 'DEF'),
    153005713: (0.8, 'DEF'),
    153005714: (0.8, 'DEF'),
    153005715: (0.8, 'DEF'),
    153005716: (0.8, 'DEF'),
    153006131: (1.0, 'ATK'),
    153006132: (1.0, 'ATK'),
    153006133: (1.0, 'ATK'),
    153006134: (1.0, 'ATK'),
    153006135: (1.0, 'ATK'),
    153006136: (1.0, 'ATK'),
}


# ---- the figure is Chinese, the STAT is not ----------------------------------
# Evidence tiers, in the order the project ranks them: Chinese prose, then English, then
# a stated house default. Every row below states its FIGURE in Chinese -- only the stat
# it scales off is missing there, because the clause reads "zao-cheng 50% shang-hai",
# damage with no stat attached.

# ENGLISH supplies the stat. `_note1_en` spells these out where `_note1` does not --
# Jacqueline's "pursues the enemy target with 75% ATK 2 times", Thylla's 250/285/320% ATK.
# A translation is weaker evidence than the original but it is still the game's own text,
# and it beats a default.
PURSUIT_STAT_FROM_EN = {
    1100101: (0.75, 'ATK'),
    1100102: (0.75, 'ATK'),
    1100103: (0.75, 'ATK'),
    1100104: (0.75, 'ATK'),
    1119101: (2.5, 'ATK'),
    1119102: (2.5, 'ATK'),
    1119103: (2.85, 'ATK'),
    1119104: (3.2, 'ATK'),
    1119175: (3.2, 'ATK'),
    130000081: (3.2, 'ATK'),
}

# NEITHER language names the stat. Mammon's Money Talks is the whole set: the Chinese is
# "zao-cheng 50% shang-hai" and the English is "dealing 50% damage", and no other row,
# column or panel in the pack says which stat. So this is a DESIGN CHOICE, and ATK is it.
#
# ATK because it is already the engine's default basis for a damage effect that names no
# stat (`core.execute`: `e.get("basis") or "ATK"`), so this changes no behaviour that a
# reader would not already predict -- it just makes the choice visible instead of implicit.
# The figure is still the game's own; only the stat is ours. If footage ever settles one
# of these, move the row up into PURSUIT and delete it here.
PURSUIT_STAT_INVENTED = {
    1004131: (0.5, 'ATK'),
    1004132: (0.5, 'ATK'),
    1004133: (0.5, 'ATK'),
    1004134: (0.5, 'ATK'),
    1004135: (0.5, 'ATK'),
    1004136: (0.5, 'ATK'),
    130000034: (0.5, 'ATK'),
    152002231: (0.5, 'ATK'),
    152002232: (0.5, 'ATK'),
    152002233: (0.5, 'ATK'),
    152002234: (0.5, 'ATK'),
    152002235: (0.5, 'ATK'),
    152002236: (0.5, 'ATK'),
    153002231: (0.5, 'ATK'),
    153002232: (0.5, 'ATK'),
    153002233: (0.5, 'ATK'),
    153002234: (0.5, 'ATK'),
    153002235: (0.5, 'ATK'),
    153002236: (0.5, 'ATK'),
}

# ---- the figure is a SKILL the Chinese names, not a number --------------------
# Satan's Bankai: 萬解：攻擊行動後追加使用1次普攻IV -- "after the attack action,
# additionally uses Normal Attack IV once". The Chinese states the pursuit completely,
# just not as a percentage: it names a SKILL, and that skill states its own figure.
#
# The sub-skill actually run is the stub 2002151-156 ("Starfall Lash IV(Bankai)"), which
# carries no numbers. Satan's own basic-attack ladder does:
#
#   2002101 Starfall Lash    0.75      2002104 Starfall Lash IV   0.99
#   2002102 ...II            0.83      2002105 ...V               1.07
#   2002103 ...III           0.91      2002106 Starfall Lash VI   1.15
#
# THE RUNG IS NOT CONSTANT. 17 of the 20 parents say 普攻IV, and THREE say 普攻VI --
# 2002136, 152000136 and 153000136, the top rung of each band. A flat 0.99 across all
# twenty under-paid those three by the gap between 0.99 and 1.15, and it was contributed
# and shipped that way before anyone re-read the top rung. Checked by extracting the
# numeral from each parent's own prose rather than assuming the ladder is uniform.
#
# NOT MODELLED: the Wrath -> Bankai gate in front of it is real but lives elsewhere --
# Satan's Special Move grants Bankai (2002121, `requires` 5 stacks of Wrath, permanent),
# and PURSUIT_GATES holds the requirement that the pursuit only fires while it is held.
PURSUIT_NAMED_SKILL = {
    2002131: (0.99, 'ATK'),
    2002132: (0.99, 'ATK'),
    2002133: (0.99, 'ATK'),
    2002134: (0.99, 'ATK'),
    2002135: (0.99, 'ATK'),
    2002136: (1.15, 'ATK'),  # 普攻VI
    130000474: (0.99, 'ATK'),
    130000784: (0.99, 'ATK'),
    152000131: (0.99, 'ATK'),
    152000132: (0.99, 'ATK'),
    152000133: (0.99, 'ATK'),
    152000134: (0.99, 'ATK'),
    152000135: (0.99, 'ATK'),
    152000136: (1.15, 'ATK'),  # 普攻VI
    153000131: (0.99, 'ATK'),
    153000132: (0.99, 'ATK'),
    153000133: (0.99, 'ATK'),
    153000134: (0.99, 'ATK'),
    153000135: (0.99, 'ATK'),
    153000136: (1.15, 'ATK'),  # 普攻VI
}


# ---- parents with MORE THAN ONE distinct pursuit ------------------------------
# A single figure per parent is the wrong shape for six rows, and the contributed table
# had it wrong for all six. Kwon's 劍術奧義 states TWO pursuits in one clause chain:
#
#   攻擊後對敵方隨機1人發動追擊，造成35%攻擊力的2段傷害，
#   追擊結束後再以15%固定機率對敵方體力最低者追擊造成90%攻擊力的2段傷害。
#   ("...pursue for 35% ATK in 2 hits; after that pursuit, a 15% fixed chance to pursue
#     the lowest-HP enemy for 90% ATK in 2 hits.")
#
# They run as two different sub-skills -- 1070171 Blazing Arcanum and 1070172 Moon Trice
# Arcanum -- and the parent-keyed table gave BOTH the 90, paying the first pursuit 2.57x
# what the prose states. Found by measuring pursuit damage against the main hit before
# shipping: this family came out at 104x, which is what sent me back to the prose.
#
# 35/90 at every rung (1070131-135 and 130000094 all state the same two figures), so the
# level does not enter into it.
#
# NOT MODELLED: the 15% chance gate in front of the second pursuit. The compiled
# follow_up carries no chance, so Moon Trice fires every time instead of 15% of the time.
# That over-pays this cast and is tracked rather than papered over with a guess.
# ponytail: sub-skill keyed overrides for 6 rows; fold into PURSUIT if the pack ever
# gives a parent three distinct pursuits.
PURSUIT_BY_SUB = {
    (1070131, 1070171): (0.35, "ATK"), (1070131, 1070172): (0.9, "ATK"),
    (1070132, 1070171): (0.35, "ATK"), (1070132, 1070172): (0.9, "ATK"),
    (1070133, 1070171): (0.35, "ATK"), (1070133, 1070172): (0.9, "ATK"),
    (1070134, 1070171): (0.35, "ATK"), (1070134, 1070172): (0.9, "ATK"),
    (1070135, 1070171): (0.35, "ATK"), (1070135, 1070172): (0.9, "ATK"),
    (130000094, 1070171): (0.35, "ATK"), (130000094, 1070172): (0.9, "ATK"),
}


def lookup(skill_id, sub_id=None):
    """-> (coefficient, basis) for this pursuit, or None if not recovered.

    `sub_id` matters only for the handful of parents that drive two different pursuits;
    everything else is keyed by the parent alone. See PURSUIT_BY_SUB.
    """
    if sub_id is not None:
        got = PURSUIT_BY_SUB.get((int(skill_id or 0), int(sub_id or 0)))
        if got:
            return got
    sid = int(skill_id or 0)
    for table in (PURSUIT, PURSUIT_STAT_FROM_EN, PURSUIT_STAT_INVENTED,
                  PURSUIT_NAMED_SKILL):
        got = table.get(sid)
        if got:
            return got
    return None

# ---- gates: WHEN a pursuit is allowed to fire --------------------------------
# `core.execute` already honours `chance_pct` and a `requires` gate on a follow_up. The
# COMPILER just never filled either in for these: `zh_pursuit_numbers` returns after the
# FIRST pursuit fragment it finds, so a skill stating two pursuits loses the second one's
# odds entirely -- the same "one value per parent" shape as the coefficients above.
#
# Measured: 51 skills state a chance inside a pursuit clause and compiled with none
# (52 others compiled correctly). Only the rows verified one at a time are here; the rest
# want the compiler fixed, which is a bigger piece of work than this file.
# ponytail: per-(parent, sub) gate data; the real fix is making zh_pursuit_numbers
# per-pursuit, which needs a fragment -> sub-skill mapping that does not exist yet.
PURSUIT_GATES = {
    # Kwon, the Sword ULT. Two pursuits in one clause chain and only the second is a
    # gamble: "...pursue for 35% ATK in 2 hits; AFTER that pursuit, a 15% fixed chance to
    # pursue the lowest-HP enemy for 90% ATK in 2 hits."
    # The "fixed chance" spelling is one `_CHANCE_PCT` already handles, so this was lost
    # to the early return, not to the pattern. Blazing Arcanum stays ungated -- its own
    # clause states no chance.
    (1070131, 1070172): {"chance_pct": 15.0},
    (1070132, 1070172): {"chance_pct": 15.0},
    (1070133, 1070172): {"chance_pct": 15.0},
    (1070134, 1070172): {"chance_pct": 15.0},
    (1070135, 1070172): {"chance_pct": 15.0},
    (130000094, 1070172): {"chance_pct": 15.0},
}

# Satan's Bankai: "after the attack action, additionally uses Normal Attack IV once".
# The pursuit belongs to the BANKAI, not to the skill, so it may only fire while Bankai
# is held. The grant itself is engine.passives.PASSIVES_EXTRA[2002131] (5 stacks of Wrath
# plus the Special Move); without that this gate would silently DISABLE the pursuit
# rather than time it, which is why the two had to land together.
_BANKAI_GATE = {"requires_status": "Bankai"}
for _pid in PURSUIT_NAMED_SKILL:
    PURSUIT_GATES[(_pid, None)] = dict(_BANKAI_GATE)
del _pid


def gate(skill_id, sub_id=None):
    """-> the gate for this pursuit, or None. Sub-keyed first, then parent-wide."""
    sid = int(skill_id or 0)
    got = PURSUIT_GATES.get((sid, int(sub_id))) if sub_id is not None else None
    return got or PURSUIT_GATES.get((sid, None))


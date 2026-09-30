"""Recovered SECOND damage components -- the skills that hit with two stats at once.

Belphegor's Spring Hammer reads 20%攻擊力+50%防禦力的3段傷害: 20% of ATK **plus** 50% of
DEF, three times. Rage Party is 60%攻擊力+180%防禦力. The compiler keeps only the FIRST
component, so those casts have been fighting on part of their kit -- and it is the DEF
half that goes missing, on precisely the casts built around DEF.

153 skills state two bases in one damage clause and every one of them compiles with a
single component. 141 are recovered here; the other 12 compile with a coefficient matching
NEITHER prose figure, so which half is present cannot be established and they are left
alone rather than guessed at.

The bar for inclusion is that the compiled coefficient AND basis match one of the two
figures the prose states exactly -- then the one being added is provably the other half,
not an invention. Derived from the pack by tools/verify_multi_basis.py rather than
transcribed, which is also what re-checks it after a pack update.

IT IS USUALLY THE BIGGER HALF. 66 of the 141 drop a component larger than the one they
keep; Rage Party lands 60% ATK and drops 180% DEF, three quarters of the hit.

WHY ONE NUMBER PER SWING. Each half is struck separately, so DEF-scaling damage is still
mitigated by the victim's defence on its own terms, and then the two are added into ONE
reported amount. A second DamageInfo row per swing would push the group count past what
the client's cinematic consumes -- the same constraint that makes the attack rider fire
once rather than per damage effect.

    129 entries, every one a DEF component.

HELD BACK: the 12 ATK+Max HP rows. Their prose states 「X%攻擊力+Y%最大體力值的傷害」 with NO
possessor -- no 自身, no 目標 -- and the engine's MAX_HP basis reads the TARGET's pool
(`formula._basis_value`: "deals 20% of the target's Max HP" is how the pack words those).
The context points the other way: 70020111 continues 再以25%最大體力值的恢復量，治療我方全體,
healing allies for a share of a pool that can only be the caster's, and 70020121 has
在攻擊前使自己的體力最大. If the clause means the CASTER's pool and we read the target's, a
9% component against a boss is catastrophic rather than merely wrong -- so these 12 wait
for footage or a clearer row instead of being shipped on the coin-toss.

The 129 DEF rows have no such ambiguity: they also name no possessor, and that is the same
convention the compiled 攻擊力 half already relies on -- `_basis_value` resolves DEF to
`effective_def(caster)`, the attacker's own, which is what an attack clause means.
"""

# skill id -> (basis of the missing half, coefficient)
SECOND_COMPONENT = {
    1003101: ('DEF', 0.5),
    1003102: ('DEF', 0.55),
    1003103: ('DEF', 0.65),
    1003104: ('DEF', 0.65),
    1003105: ('DEF', 0.65),
    1003106: ('DEF', 0.75),
    1003111: ('DEF', 0.8),
    1003112: ('DEF', 1.0),
    1003113: ('DEF', 1.2),
    1003114: ('DEF', 1.4),
    1003115: ('DEF', 1.6),
    1003116: ('DEF', 1.8),
    1003121: ('DEF', 1.0),
    1003122: ('DEF', 1.2),
    1003123: ('DEF', 1.4),
    1003124: ('DEF', 1.6),
    1003125: ('DEF', 1.8),
    1003126: ('DEF', 1.8),
    1013101: ('DEF', 0.6),
    1013102: ('DEF', 0.65),
    1013103: ('DEF', 0.7),
    1013104: ('DEF', 0.75),
    1013105: ('DEF', 0.8),
    1013106: ('DEF', 0.85),
    1040121: ('DEF', 2.0),
    1040122: ('DEF', 2.2),
    1040123: ('DEF', 2.4),
    1040124: ('DEF', 2.6),
    1040125: ('DEF', 2.8),
    1040126: ('DEF', 3.0),
    100000401: ('DEF', 0.5),
    100000402: ('DEF', 0.55),
    100000403: ('DEF', 0.65),
    100000404: ('DEF', 0.65),
    100000411: ('DEF', 1.8),
    100000412: ('DEF', 2.1),
    100000413: ('DEF', 2.4),
    100000414: ('DEF', 2.4),
    100000421: ('DEF', 2.0),
    100000422: ('DEF', 2.2),
    100000423: ('DEF', 2.4),
    100000424: ('DEF', 2.4),
    100001601: ('DEF', 0.6),
    100001602: ('DEF', 0.65),
    100001603: ('DEF', 0.7),
    100001604: ('DEF', 0.75),
    130000281: ('DEF', 0.75),
    130000291: ('DEF', 0.65),
    130000292: ('DEF', 1.6),
    130000293: ('DEF', 1.8),
    130000333: ('DEF', 2.6),
    151001821: ('DEF', 2.2),
    151001822: ('DEF', 2.4),
    151001823: ('DEF', 2.6),
    151001824: ('DEF', 2.8),
    151001825: ('DEF', 3.0),
    151001826: ('DEF', 3.2),
    151004801: ('DEF', 0.65),
    151004802: ('DEF', 0.7),
    151004803: ('DEF', 0.75),
    151004804: ('DEF', 0.8),
    151004805: ('DEF', 0.85),
    151004806: ('DEF', 0.9),
    151005601: ('DEF', 0.55),
    151005602: ('DEF', 0.6),
    151005603: ('DEF', 0.7),
    151005604: ('DEF', 0.7),
    151005605: ('DEF', 0.7),
    151005606: ('DEF', 0.8),
    151005611: ('DEF', 1.0),
    151005612: ('DEF', 1.2),
    152001821: ('DEF', 2.2),
    152001822: ('DEF', 2.4),
    152001823: ('DEF', 2.6),
    152001824: ('DEF', 2.8),
    152001825: ('DEF', 3.0),
    152001826: ('DEF', 3.2),
    152004801: ('DEF', 0.65),
    152004802: ('DEF', 0.7),
    152004803: ('DEF', 0.75),
    152004804: ('DEF', 0.8),
    152004805: ('DEF', 0.85),
    152004806: ('DEF', 0.9),
    152005601: ('DEF', 0.55),
    152005602: ('DEF', 0.6),
    152005603: ('DEF', 0.7),
    152005604: ('DEF', 0.7),
    152005605: ('DEF', 0.7),
    152005606: ('DEF', 0.8),
    152005611: ('DEF', 1.0),
    152005612: ('DEF', 1.2),
    152005613: ('DEF', 1.4),
    152005614: ('DEF', 1.6),
    152005616: ('DEF', 2.0),
    152005621: ('DEF', 1.2),
    152005622: ('DEF', 1.4),
    152005623: ('DEF', 1.6),
    152005624: ('DEF', 1.8),
    152005625: ('DEF', 2.0),
    152005626: ('DEF', 2.0),
    153001821: ('DEF', 2.2),
    153001822: ('DEF', 2.4),
    153001823: ('DEF', 2.6),
    153001824: ('DEF', 2.8),
    153001825: ('DEF', 3.0),
    153001826: ('DEF', 3.2),
    153004801: ('DEF', 0.65),
    153004802: ('DEF', 0.7),
    153004803: ('DEF', 0.75),
    153004804: ('DEF', 0.8),
    153004805: ('DEF', 0.85),
    153004806: ('DEF', 0.9),
    153005601: ('DEF', 0.55),
    153005602: ('DEF', 0.6),
    153005603: ('DEF', 0.7),
    153005604: ('DEF', 0.7),
    153005605: ('DEF', 0.7),
    153005606: ('DEF', 0.8),
    153005611: ('DEF', 1.0),
    153005612: ('DEF', 1.2),
    153005613: ('DEF', 1.4),
    153005614: ('DEF', 1.6),
    153005616: ('DEF', 2.0),
    153005621: ('DEF', 1.2),
    153005622: ('DEF', 1.4),
    153005623: ('DEF', 1.6),
    153005624: ('DEF', 1.8),
    153005625: ('DEF', 2.0),
    153005626: ('DEF', 2.0),
}


def second_component(skill_id):
    """-> (basis, coefficient) for this skill's missing half, or None."""
    return SECOND_COMPONENT.get(int(skill_id or 0))

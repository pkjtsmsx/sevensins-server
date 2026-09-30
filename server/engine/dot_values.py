"""Recovered DoT ticks -- the rate and the stat it is measured off.

A status ROW never states its rate. Venom's own row says only "deals a certain ratio of
the caster's ATK as damage over time"; Deep Sorrow's says "a certain ratio of the target's
ATK". The number is in the SKILL that inflicts it, on the ※ glossary line, and it is per
skill LEVEL -- Beelzebub's Devil Cooking runs 40/50/50/75/125/125 and Astaroth's Deep
Sorrow 80/100/120 -- so there is no per-status constant to write down and the key has to
be the skill.

355 of the pack's DoT applications compile with `magnitude: null`, land, draw their icon,
count down and tick for NOTHING. 111 skills are bound here.

THE BINDING RULE, and it is deliberately narrow: the skill applies exactly one numberless
DoT, and its prose carries exactly one ※ line stating a damage rate. Two of either and it
is left out rather than guessed. 203 skills have no rate-stating ※ line at all -- their
figure is nowhere in the pack, in either language -- and they still tick zero.

TWO THINGS BESIDES THE RATE, because each changes the damage a lot:

    basis   atk, def or max_hp. Potion's Kiss is 造成目標最大體力10%的傷害, a share of a
            POOL, not of an attack stat.
    whose   "caster" is the inflicter's ATK snapshotted when the status landed -- the
            engine's existing `source_atk`. "owner" is read LIVE off whoever carries it,
            because the prose says so: 造成狀態擁有者本身攻擊力80%傷害, "the status
            OWNER's own ATK", so the victim's own buffs and debuffs move the tick.

The stat can sit either side of the percentage -- 最大體力10% and 攻擊力80% put it first,
40%攻擊力 puts it second -- and reading only one form gets Potion's Kiss wrong in both
fields at once (atk/caster instead of max_hp/owner). Regenerate and re-audit with
tools/verify_dot_values.py.

CADENCE IS DELIBERATELY NOT MODELLED. 2 of these statuses state 每次行動時, "on every
action by any unit" -- Venom and Magic Potion. They tick at their holder's own turn start
here, with every other DoT, which is the same damage per round against a single acting
enemy and less on a full board. The alternative was submitted, measured on a device and
withdrawn by its author: an action tick lands on a bystander with no message attached, so
HP only catches up at the next sync, and the report was enemies losing HP without having
had a turn and the fight hanging afterwards. Turn cadence is the honest approximation
until the client can be told about an out-of-band beat.

Statuses covered: Venom (29), Deep Sorrow (25), Colorful Whirlpool - Green (25), Potion's Kiss (19), The Curse (12), Magic Potion (1)
"""

# skill id -> (status name, rate %, basis, whose)
RECOVERED_DOT = {
    1005121: ('Venom', 40.0, 'atk', 'caster'),
    1005122: ('Venom', 50.0, 'atk', 'caster'),
    1005123: ('Venom', 50.0, 'atk', 'caster'),
    1005124: ('Venom', 75.0, 'atk', 'caster'),
    1005125: ('Venom', 125.0, 'atk', 'caster'),
    1005126: ('Venom', 125.0, 'atk', 'caster'),
    1056141: ('Venom', 40.0, 'atk', 'caster'),
    1056142: ('Venom', 40.0, 'atk', 'caster'),
    1056143: ('Venom', 40.0, 'atk', 'caster'),
    1056144: ('Venom', 40.0, 'atk', 'caster'),
    2008121: ('Deep Sorrow', 80.0, 'atk', 'owner'),
    2008122: ('Deep Sorrow', 100.0, 'atk', 'owner'),
    2008123: ('Deep Sorrow', 100.0, 'atk', 'owner'),
    2008124: ('Deep Sorrow', 100.0, 'atk', 'owner'),
    2008125: ('Deep Sorrow', 120.0, 'atk', 'owner'),
    2008126: ('Deep Sorrow', 120.0, 'atk', 'owner'),
    2037101: ('Colorful Whirlpool - Green', 25.0, 'atk', 'owner'),
    2037102: ('Colorful Whirlpool - Green', 25.0, 'atk', 'owner'),
    2037103: ('Colorful Whirlpool - Green', 35.0, 'atk', 'owner'),
    2037104: ('Colorful Whirlpool - Green', 35.0, 'atk', 'owner'),
    2037105: ('Colorful Whirlpool - Green', 35.0, 'atk', 'owner'),
    2037106: ('Colorful Whirlpool - Green', 35.0, 'atk', 'owner'),
    2084101: ("Potion's Kiss", 10.0, 'max_hp', 'owner'),
    2084102: ("Potion's Kiss", 10.0, 'max_hp', 'owner'),
    2084103: ("Potion's Kiss", 10.0, 'max_hp', 'owner'),
    2084104: ("Potion's Kiss", 10.0, 'max_hp', 'owner'),
    2084105: ("Potion's Kiss", 10.0, 'max_hp', 'owner'),
    2084106: ("Potion's Kiss", 10.0, 'max_hp', 'owner'),
    2084111: ("Potion's Kiss", 10.0, 'max_hp', 'owner'),
    2084112: ("Potion's Kiss", 10.0, 'max_hp', 'owner'),
    2084113: ("Potion's Kiss", 10.0, 'max_hp', 'owner'),
    2084114: ("Potion's Kiss", 10.0, 'max_hp', 'owner'),
    2084115: ("Potion's Kiss", 10.0, 'max_hp', 'owner'),
    2084116: ("Potion's Kiss", 10.0, 'max_hp', 'owner'),
    2084121: ("Potion's Kiss", 10.0, 'max_hp', 'owner'),
    2084122: ("Potion's Kiss", 10.0, 'max_hp', 'owner'),
    2084123: ("Potion's Kiss", 10.0, 'max_hp', 'owner'),
    2084124: ("Potion's Kiss", 10.0, 'max_hp', 'owner'),
    2084125: ("Potion's Kiss", 10.0, 'max_hp', 'owner'),
    2084126: ("Potion's Kiss", 10.0, 'max_hp', 'owner'),
    2092101: ('The Curse', 150.0, 'atk', 'owner'),
    2092102: ('The Curse', 150.0, 'atk', 'owner'),
    2092103: ('The Curse', 150.0, 'atk', 'owner'),
    2092104: ('The Curse', 150.0, 'atk', 'owner'),
    2092105: ('The Curse', 150.0, 'atk', 'owner'),
    2092106: ('The Curse', 150.0, 'atk', 'owner'),
    2092121: ('The Curse', 150.0, 'atk', 'owner'),
    2092122: ('The Curse', 150.0, 'atk', 'owner'),
    2092123: ('The Curse', 150.0, 'atk', 'owner'),
    2092124: ('The Curse', 150.0, 'atk', 'owner'),
    2092125: ('The Curse', 150.0, 'atk', 'owner'),
    2092126: ('The Curse', 150.0, 'atk', 'owner'),
    7001647: ('Venom', 40.0, 'atk', 'caster'),
    7001680: ('Magic Potion', 100.0, 'atk', 'caster'),
    122002119: ("Potion's Kiss", 10.0, 'max_hp', 'owner'),
    130000643: ('Deep Sorrow', 100.0, 'atk', 'owner'),
    130000911: ('Colorful Whirlpool - Green', 35.0, 'atk', 'owner'),
    151002421: ('Venom', 40.0, 'atk', 'caster'),
    151002422: ('Venom', 50.0, 'atk', 'caster'),
    151002423: ('Venom', 50.0, 'atk', 'caster'),
    151002424: ('Venom', 75.0, 'atk', 'caster'),
    151002425: ('Venom', 125.0, 'atk', 'caster'),
    151002426: ('Venom', 125.0, 'atk', 'caster'),
    151002821: ('Deep Sorrow', 80.0, 'atk', 'owner'),
    151002822: ('Deep Sorrow', 100.0, 'atk', 'owner'),
    151002823: ('Deep Sorrow', 100.0, 'atk', 'owner'),
    151002824: ('Deep Sorrow', 100.0, 'atk', 'owner'),
    151002825: ('Deep Sorrow', 120.0, 'atk', 'owner'),
    151002826: ('Deep Sorrow', 120.0, 'atk', 'owner'),
    151005101: ('Colorful Whirlpool - Green', 25.0, 'atk', 'owner'),
    151005102: ('Colorful Whirlpool - Green', 25.0, 'atk', 'owner'),
    151005103: ('Colorful Whirlpool - Green', 35.0, 'atk', 'owner'),
    151005104: ('Colorful Whirlpool - Green', 35.0, 'atk', 'owner'),
    151005105: ('Colorful Whirlpool - Green', 35.0, 'atk', 'owner'),
    151005106: ('Colorful Whirlpool - Green', 35.0, 'atk', 'owner'),
    152002421: ('Venom', 40.0, 'atk', 'caster'),
    152002422: ('Venom', 50.0, 'atk', 'caster'),
    152002423: ('Venom', 50.0, 'atk', 'caster'),
    152002424: ('Venom', 75.0, 'atk', 'caster'),
    152002425: ('Venom', 125.0, 'atk', 'caster'),
    152002426: ('Venom', 125.0, 'atk', 'caster'),
    152002821: ('Deep Sorrow', 80.0, 'atk', 'owner'),
    152002822: ('Deep Sorrow', 100.0, 'atk', 'owner'),
    152002823: ('Deep Sorrow', 100.0, 'atk', 'owner'),
    152002824: ('Deep Sorrow', 100.0, 'atk', 'owner'),
    152002825: ('Deep Sorrow', 120.0, 'atk', 'owner'),
    152002826: ('Deep Sorrow', 120.0, 'atk', 'owner'),
    152005101: ('Colorful Whirlpool - Green', 25.0, 'atk', 'owner'),
    152005102: ('Colorful Whirlpool - Green', 25.0, 'atk', 'owner'),
    152005103: ('Colorful Whirlpool - Green', 35.0, 'atk', 'owner'),
    152005104: ('Colorful Whirlpool - Green', 35.0, 'atk', 'owner'),
    152005105: ('Colorful Whirlpool - Green', 35.0, 'atk', 'owner'),
    152005106: ('Colorful Whirlpool - Green', 35.0, 'atk', 'owner'),
    153002421: ('Venom', 40.0, 'atk', 'caster'),
    153002422: ('Venom', 50.0, 'atk', 'caster'),
    153002423: ('Venom', 50.0, 'atk', 'caster'),
    153002424: ('Venom', 75.0, 'atk', 'caster'),
    153002425: ('Venom', 125.0, 'atk', 'caster'),
    153002426: ('Venom', 125.0, 'atk', 'caster'),
    153002821: ('Deep Sorrow', 80.0, 'atk', 'owner'),
    153002822: ('Deep Sorrow', 100.0, 'atk', 'owner'),
    153002823: ('Deep Sorrow', 100.0, 'atk', 'owner'),
    153002824: ('Deep Sorrow', 100.0, 'atk', 'owner'),
    153002825: ('Deep Sorrow', 120.0, 'atk', 'owner'),
    153002826: ('Deep Sorrow', 120.0, 'atk', 'owner'),
    153005101: ('Colorful Whirlpool - Green', 25.0, 'atk', 'owner'),
    153005102: ('Colorful Whirlpool - Green', 25.0, 'atk', 'owner'),
    153005103: ('Colorful Whirlpool - Green', 35.0, 'atk', 'owner'),
    153005104: ('Colorful Whirlpool - Green', 35.0, 'atk', 'owner'),
    153005105: ('Colorful Whirlpool - Green', 35.0, 'atk', 'owner'),
    153005106: ('Colorful Whirlpool - Green', 35.0, 'atk', 'owner'),
}


def lookup(skill_id, status_name=None):
    """-> (rate, basis, whose) for this skill's numberless DoT, or None.

    `status_name` is checked when given, so a pack change that swaps which status a skill
    inflicts cannot silently hand it another status's rate.
    """
    got = RECOVERED_DOT.get(int(skill_id or 0))
    if not got:
        return None
    name, rate, basis, whose = got
    if status_name is not None and str(status_name) != name:
        return None
    return rate, basis, whose

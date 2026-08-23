"""Server-owned rate settings: the one place a deliberate balance change may live.

WHY THIS EXISTS. Every number this server pays out is either RECONSTRUCTED (observed in
footage, or read off a design row) or INVENTED to fill a gap the design data cannot
fill. Keeping those apart is what makes the reconstruction trustworthy, and the way it
gets lost is a rate change arriving disguised as a bug fix -- a contributed patch set
did exactly that, rescaling the karma decision payouts 5/10/15 -> 25/50/100 in among a
list of genuine corrections, where it read as a fix rather than as the balance change it
was.

So the rule here is deliberately narrow:

  **1.0 ALWAYS MEANS "WHAT THE RETAIL GAME DID."**

Never a tuned value, never a compromise, never "1.0 is close enough". A default build is
a faithful build, and every deviation from the real game is a number somebody typed on
purpose and can see in one place. If a rate's 1.0 behaviour is ever found to disagree
with observed footage, the FIX is to the payout code, not to the default here.

Two more rules that follow from that:

  * A rate MULTIPLIES, it does not replace. The evidence records -- KARMA_OBSERVED, the
    drop pool weights, the design rows -- stay exactly as they are and keep documenting
    what was real; the rate scales the amount at the point it is granted.
  * A rate scales QUANTITIES only. It cannot express "rarer but bigger", and it must
    never touch anything structural: not the drop pool weights, not the one-stack-per-
    wave rule, not which item a dungeon pays. Those are reconstructions, not knobs.

READ AT GRANT TIME, not at import, so editing the file takes effect without restarting
the server -- a phone hosting a session should not have to be restarted to retune. The
file is re-read only when its mtime changes, so the common case is a dict lookup.

Nothing here ever raises. A missing file, unreadable JSON, a negative number or a string
where a float belongs all fall back to the faithful default rather than taking a live
battle down with them.
"""
import json
import os

# Same relocation convention as player_state.core.STATE_DIR: an Android host app cannot
# write next to the code, so the env var always wins and the desktop default sits beside
# the accounts it belongs with. Deliberately NOT in the package dir -- that is read-only
# on device, and a hot update replaces it wholesale.
_SERVER_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_DIR = os.environ.get("SEVENSINS_ACCOUNTS") or os.path.join(_SERVER_DIR, "accounts")
SETTINGS_NAME = "settings.json"

# Every rate the server honours, with the faithful default and what 1.0 means. Adding a
# rate means adding it HERE -- `rate()` refuses names it does not know, so a typo in the
# settings file cannot silently do nothing while looking like it worked.
#
# The "1.0 is" column is the contract: it names the observation or design row that the
# default reproduces, so a later reader can check the claim rather than trust it.
RATES = {
    # 1.0 is the retail karma decision payouts, 5 / 10 / 15 gems by tier -- see
    # player_state.karma.KARMA_OBSERVED, which is footage of the real server.
    "karma_gems": 1.0,
    # 1.0 is 250 Mira per wave at ap 5, the anchor every observed main-story clear
    # agrees on, scaled by COIN_PER_AP for other stamina costs.
    "coin": 1.0,
    # 1.0 is the observed material stack sizes -- x1 at ap 5, growing with the ap ladder.
    # Applies to drop COUNTS only; the pool weights are a reconstruction and are not
    # scalable.
    "drop_count": 1.0,
    # 1.0 is the dungeon share read off live footage: an Evolution Abyss clear paying
    # 4+7+9 against a listed Stage Clear reward of 100.
    "dungeon_drop": 1.0,
    # 1.0 is FLAT, which is what retail paid: the Evolution Abyss lists the same Stage
    # Clear reward (100) and the same stamina cost (1) on all 48 rungs, so a deep clear
    # and a shallow one pay alike. The dungeon next door settles that this is deliberate
    # rather than an oversight -- the Trainers Gym, same family and same 1 stamina, does
    # scale its reward by rung (5 -> 15).
    #
    # This is therefore a HOUSE RULE and not a reconstruction: above 1.0 the Abyss climbs
    # linearly to this multiple of the shallow payout at the deepest rung, so 10.0 means
    # rung 48 pays about ten times rung 1. Rung 1 never moves, so the footage the
    # `dungeon_drop` default is anchored on stays reproduced whatever this is set to.
    "evolution_depth": 1.0,
    # 1.0 is stage_battle_xp as it stands. Flagged honestly: the real per-stage XP is
    # NOT in the pack, so this default is itself a reconstruction of unknown accuracy --
    # unlike the others, 1.0 here means "our best guess", not "what retail paid".
    "battle_xp": 1.0,
}

# A rate outside this range is almost certainly a typo (a stray zero, a percentage
# written as 500). Clamped rather than rejected so a fat-fingered edit degrades to
# something playable instead of zeroing every reward in the game.
RATE_MIN, RATE_MAX = 0.0, 1000.0

_cache = None
_cache_stamp = None


def settings_path():
    """Absolute path of the settings file. It does not have to exist."""
    return os.path.join(SETTINGS_DIR, SETTINGS_NAME)


def _stamp(path):
    try:
        st = os.stat(path)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _load():
    """The settings dict, re-read only when the file's mtime/size changed."""
    global _cache, _cache_stamp
    path = settings_path()
    stamp = _stamp(path)
    if _cache is not None and stamp == _cache_stamp:
        return _cache
    data = {}
    if stamp is not None:
        try:
            with open(path, encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                data = loaded
        except Exception:                   # noqa: BLE001 -- never break a payout
            data = {}
    _cache, _cache_stamp = data, stamp
    return data


def rate(name):
    """-> the multiplier for `name`, or its faithful default. Never raises.

    Unknown names raise KeyError deliberately: that is a programming error at a call
    site, not user input, and failing loudly beats silently scaling by 1.0 forever.
    """
    default = RATES[name]                   # KeyError here is a bug, not bad input
    section = _load().get("rates")
    if not isinstance(section, dict):
        return default
    value = section.get(name, default)
    try:
        value = float(value)
    except (TypeError, ValueError):
        return default
    if value != value:                      # NaN
        return default
    return max(RATE_MIN, min(RATE_MAX, value))


def scale(amount, name, minimum=1):
    """Scale an integer reward by a rate, keeping a nonzero payout nonzero.

    `minimum` guards the small counts: at rate 0.4 a drop of x1 would round to nothing
    and the stack would vanish from the panel entirely, which reads as a bug rather than
    as a lower rate. Pass minimum=0 where a zero result is legitimate.

    A rate of exactly 0 is honoured as OFF and returns 0 -- that is someone deliberately
    switching a reward off, not a rounding artefact.
    """
    if not amount:
        return 0
    multiplier = rate(name)
    if multiplier == 0:
        return 0
    scaled = int(round(amount * multiplier))
    return max(minimum, scaled) if amount > 0 else min(-minimum, scaled)


def all_rates():
    """-> {name: effective value} for every known rate. For the editor UI and logging."""
    return {name: rate(name) for name in RATES}


def non_default_rates():
    """-> {name: value} for rates that are NOT 1.0.

    What a "is this a faithful build?" check reads, and what start-up should log: an
    empty dict means every payout is the reconstructed one.
    """
    return {name: value for name, value in all_rates().items()
            if value != RATES[name]}


def write_rates(values):
    """Merge `values` into the settings file and return the effective rates.

    Merges rather than replaces so an unrelated key a future version adds is not
    dropped by an editor that has never heard of it. Unknown or unparseable names are
    ignored rather than written through, so the file cannot fill up with typos.
    """
    global _cache, _cache_stamp
    data = dict(_load())
    section = dict(data.get("rates") or {})
    for name, value in (values or {}).items():
        if name not in RATES:
            continue
        try:
            section[name] = max(RATE_MIN, min(RATE_MAX, float(value)))
        except (TypeError, ValueError):
            continue
    data["rates"] = section
    os.makedirs(SETTINGS_DIR, exist_ok=True)
    path = settings_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, path)                   # atomic: never a half-written settings file
    _cache = _cache_stamp = None            # force a re-read on the next rate()
    return all_rates()

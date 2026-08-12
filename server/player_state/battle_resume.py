"""In-progress battle persistence: lets a killed/restarted server resume a fight
instead of silently dropping it (see memory sevensins-battle-resume / the karma-events
bug report that surfaced the gap -- an interrupted tutorial run paid its one-time AVG
karma but never recorded the stage clear, because the in-progress Battle object lived
only in the connection's memory and vanished the instant the process died).

Split out of the former monolithic core.py; depends only on .core for the account-state
plumbing (nothing here touches battle.py -- the Battle object itself owns
to_state()/restore_battle(), this module just stores/retrieves the dict under a
reserved account key)."""

BATTLE_KEY = "battle"


def save_battle(state, battle):
    """Snapshot the live Battle after every action that can change it -- HP, statuses,
    wave, turn order. Cheap and frequent by design: a save this doesn't cover is a
    fight that silently can't resume, which is the exact bug this module exists to
    close."""
    state[BATTLE_KEY] = battle.to_state()


def saved_battle(state):
    """The saved battle snapshot, or None -- also what the login sync reply checks to
    decide whether to offer the client a reconnect prompt at all."""
    return state.get(BATTLE_KEY)


def clear_battle(state):
    """Drop the snapshot once a fight has genuinely ended (REQ_BATTLE_END) or been
    abandoned (REQ_RETREAT) -- a completed fight must never come back as a stale
    'resume?' prompt on the next login."""
    state.pop(BATTLE_KEY, None)

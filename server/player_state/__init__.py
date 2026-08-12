"""Player account state, split by game system.

core = the account spine (default/load/save + every shared helper and constant);
each feature submodule below depends ONLY on core. `from .X import *` folds them
all back into a flat namespace so `import player_state as ps` reaches `ps.load`,
`ps.grant_bloodpact`, etc. exactly as before the split.
"""
from .core import *  # noqa: F401,F403
from .roster import *  # noqa: F401,F403
from .charprogress import *  # noqa: F401,F403
from .soulbook import *  # noqa: F401,F403
from .quests import *  # noqa: F401,F403
from .runes import *  # noqa: F401,F403
from .gear import *  # noqa: F401,F403
from .roulette import *  # noqa: F401,F403
from .ofa import *  # noqa: F401,F403
from .mail import *  # noqa: F401,F403
from .loginbonus import *  # noqa: F401,F403
from .karma import *  # noqa: F401,F403
from .battle_resume import *  # noqa: F401,F403
from .shop import *  # noqa: F401,F403
from .gacha import *  # noqa: F401,F403

# `import *` drops leading-underscore names; re-export the private helpers that
# sibling scripts reach through the package (e.g. reset_tutorial.py uses _char_uid).
from .core import _char_uid  # noqa: F401

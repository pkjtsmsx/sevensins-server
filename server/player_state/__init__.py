"""Player account state: the SoT for a logged-in account and every game system's
reads/writes to it.

This is a PACKAGE that re-exports a flat namespace, so importers keep doing
`import player_state as ps` and reaching `ps.load`, `ps.grant_bloodpact`, etc.
unchanged -- the 99 `ps.*` call sites in titan_server.py never had to move. The
implementation is split across submodules by game system (core = the account spine:
default/load/save + shared helpers; then roster, runes, quests, shop, ...), each
folded back into this namespace below.

Split order matters only in that a name defined in two submodules resolves to the
LAST import here; keep systems disjoint. Submodules depend on `core`, not on each
other.
"""
from .core import *          # noqa: F401,F403  (the whole account API, flat)

"""The battle engine — pure, no wire format, no I/O.

Deliberately a package rather than more modules next to `battle.py`: this is the
replacement for that engine, and the boundary needs to be one you can see. Nothing in
here imports the old engine, and nothing in here knows what JSON looks like.

    specs    load the compiled skill/status data (build artifacts, not logic)
    formula  damage arithmetic and the attribute triangle -- every tunable named
    core     target resolution and skill execution -> Outcome

Phase 4 adds the serialiser that turns an `Outcome` into the client's `data` payload.
Keeping that out of here is the point: the serialiser is where the wire invariants get
asserted, and an engine that cannot emit JSON cannot quietly emit wrong JSON.
"""

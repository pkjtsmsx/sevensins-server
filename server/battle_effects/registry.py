"""The op registry: op-name -> handler. Kept dependency-free so `core` and `ops` can both
import it without a cycle (ops imports core for helpers; core reads the registry at run
time to dispatch). Adding an op = writing a handler in `ops` (or a new module) and
decorating it with @register("name") -- the engine spine never changes."""

OPS = {}


def register(name):
    """Decorator: register a handler `fn(eff, ctx)` under an op name."""
    def deco(fn):
        OPS[name] = fn
        return fn
    return deco


def registered_ops():
    """The set of op names the engine can execute -- used to assert parser/engine
    lock-step (every op the parser emits must have a handler)."""
    return set(OPS)

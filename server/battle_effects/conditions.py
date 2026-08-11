"""Condition evaluators (Phase 2) -- lock-step with tools/skillparse/conditions.py.

An effect carrying {"when": cond} only fires if its cond evaluates True here. Each
cond["kind"] registers an evaluator in CONDS; the harness asserts every kind the parser
can emit has one. Evaluation NEVER throws: an unknown kind, a missing unit, or absent
env data (no turn number, no crit model) evaluates False -- a branch that cannot be
judged must not fire.

Evaluators take (cond, ctx) where ctx is duck-typed: .attacker .primary .allies
.enemies .env (dict: "turn", "crit") .per_target (this action's damage rows, for kill
checks). core.Ctx satisfies it; tests may pass a SimpleNamespace.
"""

CONDS = {}


def cond(name):
    def deco(fn):
        CONDS[name] = fn
        return fn
    return deco


def registered_conds():
    return set(CONDS)


def eval_cond(c, ctx):
    fn = CONDS.get(c.get("kind")) if isinstance(c, dict) else None
    if fn is None:
        return False
    try:
        return bool(fn(c, ctx))
    except Exception:                                     # noqa: BLE001
        return False                                      # a broken gate never fires


# ---- unit pools -------------------------------------------------------------
def _target(ctx):
    if ctx.primary is not None:
        return ctx.primary
    live = [u for u in ctx.enemies if u.alive]
    return live[0] if live else None


def _pool(subject, ctx):
    if subject == "self":
        return [ctx.attacker]
    if subject == "any_enemy":
        return [u for u in ctx.enemies if u.alive]
    if subject == "any_ally":
        return [u for u in ctx.allies if u.alive]
    t = _target(ctx)
    return [t] if t is not None else []


# ---- status classes ---------------------------------------------------------
def _is_dot(defn):
    return "damage_over_time" in defn.get("flags", []) or defn.get("tick_pct_atk")


def _is_hot(defn):
    return "heal_over_time" in defn.get("flags", []) or defn.get("heal_pct_maxhp")


def _is_buff(defn):
    if _is_hot(defn) or defn.get("immune_to") or defn.get("shield_amount") \
            or defn.get("shield_pct_atk"):
        return True
    good = bad = False
    for mod in defn.get("stat_mods", []):
        up = mod.get("value", 0) > 0
        if mod.get("stat") == "damage_taken":             # taking more damage is bad
            up = not up
        good, bad = good or up, bad or not up
    return good and not bad


def _is_debuff(defn):
    if _is_buff(defn):
        return False
    flags = set(defn.get("flags", []))
    if _is_dot(defn) or flags & {"immobilize", "skip_action", "damage_taken_up",
                                 "heal_block", "ability_seal"}:
        return True
    return any(mod.get("value", 0) < 0 if mod.get("stat") != "damage_taken"
               else mod.get("value", 0) > 0 for mod in defn.get("stat_mods", []))


_CLASSES = {"_buff": _is_buff, "_debuff": _is_debuff, "_dot": _is_dot, "_hot": _is_hot}


def _has(unit, name, min_stacks):
    if name == "Elite" and getattr(unit, "elite", False):
        return True
    classifier = _CLASSES.get(name)
    for st in unit.statuses:
        if classifier is not None:
            if not classifier(st.definition):
                continue
        elif st.name != name:
            continue
        if min_stacks and st.stacks < min_stacks:
            continue
        return True
    return False


# ---- evaluators -------------------------------------------------------------
_CMP = {"gt": lambda a, b: a > b, "ge": lambda a, b: a >= b,
        "lt": lambda a, b: a < b, "le": lambda a, b: a <= b}


@cond("hp")
def _hp(c, ctx):
    pool = _pool(c.get("subject", "self"), ctx)
    if not pool:
        return False
    u = pool[0]
    return _CMP[c["cmp"]](100.0 * u.hp / max(1, u.max_hp), c["pct"])


@cond("hp_vs")
def _hp_vs(c, ctx):
    t = _target(ctx)
    if t is None:
        return False
    return _CMP[c["cmp"]](ctx.attacker.hp, t.hp)


@cond("status")
def _status(c, ctx):
    pool = _pool(c.get("subject", "target"), ctx)
    hit = any(all(_has(u, nm, c.get("min_stacks")) for nm in c.get("names", []))
              for u in pool)
    return not hit if c.get("negate") else hit


@cond("cast_type")
def _cast_type(c, ctx):
    # char _job: 2=STR, 3=AGI, 4=TEC (CommonUtil.GetJobUseText -> text 12099+job).
    pool = _pool(c.get("subject", "target"), ctx)
    jobs = {"STR": 2, "AGI": 3, "TEC": 4}
    return any(getattr(u, "job", None) == jobs.get(c.get("type")) for u in pool)


@cond("crit")
def _crit(c, ctx):
    return bool(getattr(ctx, "env", None) and ctx.env.get("crit"))


@cond("kill")
def _kill(c, ctx):
    died = any(e.get("died") for e in ctx.per_target.values())
    return not died if c.get("negate") else died


@cond("turn_parity")
def _turn_parity(c, ctx):
    turn = (getattr(ctx, "env", None) or {}).get("turn")
    if turn is None:
        return False
    return (turn % 2 == 1) if c["parity"] == "odd" else (turn % 2 == 0)


@cond("turn_cmp")
def _turn_cmp(c, ctx):
    turn = (getattr(ctx, "env", None) or {}).get("turn")
    return turn is not None and _CMP[c["cmp"]](turn, c["n"])


@cond("alive")
def _alive(c, ctx):
    pool = _pool(c.get("subject", "target"), ctx)
    return any(u.alive for u in pool)


@cond("all")
def _all(c, ctx):
    return all(eval_cond(s, ctx) for s in c.get("conds", []))


@cond("any")
def _any(c, ctx):
    return any(eval_cond(s, ctx) for s in c.get("conds", []))

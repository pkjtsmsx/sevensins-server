"""Effect-op handlers. Each is `fn(eff, ctx)` registered under its op name; the engine
spine (core) dispatches to them. This is the file that GROWS as skills are recreated --
add a handler here (or in a new sibling module that also imports `register`), never edit
core's dispatch. Every op the parser can emit MUST have a handler here, or a `complete`
skill silently does nothing (asserted by the regression harness via registry.registered_ops).
"""
from .core import (Status, apply_status, damage_taken_multiplier, flat_bonus,
                   grant_immunity, resolve_targets, stat_multiplier)
from .registry import register


@register("damage")
def _damage(eff, ctx):
    times = eff.get("times", 1) or 1
    pct = eff.get("pct_atk", 0)
    pct_def = eff.get("pct_def", 0)
    pct_hp = eff.get("pct_target_maxhp", 0)          # "absolute" -- ignores DEF
    a = ctx.attacker
    # Effective attacker ATK/DEF: base scaled by the attacker's own statuses (Keen +,
    # Fracture -) plus any flat mod.
    eff_atk = a.atk * stat_multiplier(a.statuses, "ATK") + flat_bonus(a.statuses, "ATK")
    eff_def = a.defense * stat_multiplier(a.statuses, "DEF") + flat_bonus(a.statuses, "DEF")
    for u in ctx.targets(eff.get("target")):
        for _ in range(times):
            mitigable = (eff_atk * pct + eff_def * pct_def) / 100.0
            val = mitigable * (1.0 - ctx.reduce(u)) * damage_taken_multiplier(u.statuses)
            val += u.max_hp * pct_hp / 100.0
            dmg = max(1, int(val))
            u.hp = max(0, u.hp - dmg)
            e = ctx.hit_entry(u)
            e["damage"] += dmg
            e["died"] = not u.alive


@register("apply_status")
def _apply_status(eff, ctx):
    for u in ctx.targets(eff.get("target")):
        st = apply_status(u, eff.get("status"), eff.get("duration"))
        if st:
            (ctx.outcome["self"]["statuses"] if u is ctx.attacker
             else ctx.hit_entry(u)["statuses"]).append(st.name)
            ctx.outcome["status_events"].append(
                {"unit": u, "name": st.name, "round": st.remaining})


@register("heal")
def _heal(eff, ctx):
    amt = int(ctx.attacker.max_hp * eff.get("pct_maxhp", 0) / 100.0)
    for u in ctx.targets(eff.get("target")):
        healed = min(amt, u.max_hp - u.hp)
        u.hp += healed
        if u is ctx.attacker:
            ctx.outcome["self"]["heal"] += healed


@register("cleanse")
def _cleanse(eff, ctx):
    names = set(eff.get("statuses", []))
    for u in ctx.targets(eff.get("target")):
        u.statuses = [s for s in u.statuses if s.name not in names]


@register("shield")
def _shield(eff, ctx):
    for u in ctx.targets(eff.get("target")):
        st = apply_status(u, "Shield", eff.get("duration"))
        if st:
            st.definition = dict(st.definition, shield_amount=eff.get("amount"))
            ctx.outcome["status_events"].append(
                {"unit": u, "name": st.name, "round": st.remaining})


@register("stat_mod")
def _stat_mod(eff, ctx):
    # HP mods change max_hp directly (statuses don't recompute max_hp); ATK/DEF/SPD ride a
    # synthesized status; unmodelled stats (CRIT, ...) are skipped rather than applied wrong.
    stat = eff["stat"]
    val = eff["pct"]
    unit = eff.get("unit", "pct")
    for u in ctx.targets(eff.get("target")):
        if stat == "HP":
            delta = int(u.max_hp * val / 100.0) if unit == "pct" else int(val)
            u.max_hp = max(1, u.max_hp + delta)
            u.hp = max(1, min(u.max_hp, u.hp + delta))
        elif stat in ("ATK", "DEF", "SPD"):
            synth = {"stat_mods": [{"stat": stat, "value": val, "unit": unit}],
                     "duration": eff.get("duration") or 1}
            tag = f"{stat}{val:+d}%" if unit == "pct" else f"{stat}{val:+d}"
            u.statuses.append(Status(tag, synth["duration"], synth))


@register("move_gauge")
def _move_gauge(eff, ctx):
    # pct signed: negative drains the target's charge gauge, positive fills it.
    for u in ctx.targets(eff.get("target")):
        ctx.outcome["gauge"].append({"unit": u, "pct": eff.get("pct", 0)})


@register("skill_cd")
def _skill_cd(eff, ctx):
    # delta signed: positive delays the target's skills, negative refreshes them.
    for u in ctx.targets(eff.get("target")):
        ctx.outcome["cd"].append({"unit": u, "delta": eff.get("delta", 0)})


@register("immunity")
def _immunity(eff, ctx):
    for u in ctx.targets(eff.get("target")):
        grant_immunity(u, eff.get("status"), eff.get("duration"))


@register("extend_status")
def _extend_status(eff, ctx):
    # No target field in the data; the named status is usually a self-buff ("extend Fear
    # Nothing"), occasionally on the struck enemy. Extend it wherever it lives among
    # {caster, primary target} -- absent elsewhere, this is a safe no-op.
    name = eff.get("status")
    add = eff.get("duration") or 0
    pool = [ctx.attacker] + ctx.targets("enemy_target")
    for u in pool:
        for s in u.statuses:
            if s.name == name and s.remaining != "battle":
                s.remaining += add

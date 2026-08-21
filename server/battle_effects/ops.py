"""Effect-op handlers. Each is `fn(eff, ctx)` registered under its op name; the engine
spine (core) dispatches to them. This is the file that GROWS as skills are recreated --
add a handler here (or in a new sibling module that also imports `register`), never edit
core's dispatch. Every op the parser can emit MUST have a handler here, or a `complete`
skill silently does nothing (asserted by the regression harness via registry.registered_ops).
"""
from .conditions import _CLASSES
from .core import (Status, absorb_shield, apply_status, damage_taken_multiplier,
                   effective_atk, flat_bonus, grant_immunity, has_flag, resolve_targets,
                   stat_multiplier)
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
    eff_atk = effective_atk(a)
    eff_def = a.defence * stat_multiplier(a.statuses, "DEF") + flat_bonus(a.statuses, "DEF")
    for u in ctx.targets(eff.get("target")):
        for i in range(times):
            mitigable = (eff_atk * pct + eff_def * pct_def) / 100.0
            val = mitigable * (1.0 - ctx.reduce(u)) * damage_taken_multiplier(u.statuses)
            val += u.max_hp * pct_hp / 100.0
            dmg = max(1, int(val))
            dmg = absorb_shield(u, dmg)          # Shield status drains before HP does
            u.hp = max(0, u.hp - dmg)
            e = ctx.hit_entry(u)
            e["damage"] += dmg
            e["died"] = not u.alive
            # Also record the blow on its own, tagged with its swing index: the folded
            # entry is what the server's totals want, one row per swing is what the
            # client's animation wants.
            ctx.outcome["strikes"].append(
                {"target": u, "damage": dmg, "seq": i})


@register("apply_status")
def _apply_status(eff, ctx):
    for u in ctx.targets(eff.get("target")):
        st = None
        for _ in range(eff.get("stacks", 1) or 1):
            st = apply_status(u, eff.get("status"), eff.get("duration"),
                              source=ctx.attacker) or st
        if st:
            (ctx.outcome["self"]["statuses"] if u is ctx.attacker
             else ctx.hit_entry(u)["statuses"]).append(st.name)
            ctx.outcome["status_events"].append(
                {"unit": u, "name": st.name, "round": st.remaining})


@register("heal")
def _heal(eff, ctx):
    # pct_maxhp scales off the caster's Max HP; pct_caster_hp off their CURRENT HP
    # ("recovers All allies' HP by 25% of the caster's HP").
    amt = int(ctx.attacker.max_hp * eff.get("pct_maxhp", 0) / 100.0
              + ctx.attacker.hp * eff.get("pct_caster_hp", 0) / 100.0)
    for u in ctx.targets(eff.get("target")):
        if has_flag(u, "heal_block"):
            continue
        healed = min(amt, u.max_hp - u.hp)
        u.hp += healed
        if u is ctx.attacker:
            ctx.outcome["self"]["heal"] += healed


def _removable(s):
    return "unremovable" not in s.definition.get("flags", [])


@register("cleanse")
def _cleanse(eff, ctx):
    names = set(eff.get("statuses", []))
    for u in ctx.targets(eff.get("target")):
        u.statuses = [s for s in u.statuses if s.name not in names or not _removable(s)]


@register("cleanse_class")
def _cleanse_class(eff, ctx):
    # Strip a whole class ("removes all buffs from the target"). Classifiers are shared
    # with the condition evaluators; immunity markers and "unremovable" statuses survive.
    classify = _CLASSES.get(eff.get("cls"))
    if classify is None:
        return
    for u in ctx.targets(eff.get("target")):
        u.statuses = [s for s in u.statuses
                      if "immunity" in s.definition.get("flags", [])
                      or not _removable(s)
                      or not classify(s.definition)]


@register("shield")
def _shield(eff, ctx):
    for u in ctx.targets(eff.get("target")):
        st = apply_status(u, "Shield", eff.get("duration"), flat_shield=eff.get("amount"))
        if st:
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
    # delta signed: positive delays the target's skills, negative refreshes them. A
    # unit under cd_reduction_block ("CD Reduction Block") ignores negative deltas only
    # -- it can still be delayed, just not sped back up.
    delta = eff.get("delta", 0)
    for u in ctx.targets(eff.get("target")):
        if delta < 0 and has_flag(u, "cd_reduction_block"):
            continue
        ctx.outcome["cd"].append({"unit": u, "delta": delta})


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

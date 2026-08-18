"""Runtime skill-effect engine, as a package.

Split from the former single battle_effects.py so it scales to recreating every skill
without becoming a monolith (see docs/BATTLE_SKILL_PLAN.md):
  * registry.py -- the op-name -> handler table (dependency-free).
  * core.py     -- the SPINE: loaders, Status model, stat/target helpers, immunity,
                   apply_status, the Ctx + dispatch, and execute_skill / run_phase.
  * ops.py      -- the effect-op handlers, each @register(...)'d. This is what grows.

Importers keep doing `import battle_effects as fx` and reaching `fx.execute_skill`,
`fx.is_complete`, etc. unchanged. Importing this package registers every op handler.
"""
from .core import (                                                     # noqa: F401
    Ctx,
    CC_STATUSES,
    IMMEDIATE_TRIGGERS,
    STATS,
    Status,
    absorb_shield,
    apply_status,
    catalog,
    damage_taken_multiplier,
    effective_atk,
    execute_skill,
    flat_bonus,
    grant_immunity,
    has_flag,
    hit_count,
    aoe_damage,
    design_enemy_targets,
    target_range,
    any_incomplete_aoe,
    is_complete,
    is_immobilized,
    resolve_targets,
    run_phase,
    set_rng,
    skill_effects,
    stat_multiplier,
    status_skill_id,
    tick_dot_hot,
    _apply_op,
    _default_reduce,
    _new_outcome,
)
from .conditions import eval_cond, registered_conds             # noqa: F401
from .registry import OPS, register, registered_ops             # noqa: F401
from . import ops as _ops                    # noqa: F401  -- side effect: register handlers

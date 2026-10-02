"""The escalation ladder (escalation spec §3, §4, §6).

Cheapest-correction-first: most triggers advance one rung; hard triggers
(blast radius, not capability) bypass the ladder. De-escalation happens only
at subtask boundaries — the next task re-enters at its own routed tier.
"""

from sprintbaton.entities.enums import EscalationTier, TaskType, TriggerType

# Router category -> entry tier (§4)
_ENTRY_TIERS: dict[TaskType, EscalationTier] = {
    TaskType.Simple: EscalationTier.E0,
    TaskType.Ambiguous: EscalationTier.E1,   # clarification precedes execution
    TaskType.Complex: EscalationTier.E2,     # Opus plans once, then Plan Classification
                                             # picks the executor
    TaskType.Abstract: EscalationTier.E1,    # clarification precedes execution here too
                                             # (finalization-passing-criteria spec §5.1);
                                             # the Opus-execution floor is enforced by the
                                             # orchestrator's classification checkpoints
}

# Categories whose entry tier the importance flag is allowed to floor at E2
# (classification-taxonomy spec §6.1). Ambiguous/Abstract are deliberately
# excluded: clarification (E1) always happens first for those two, regardless
# of importance — their importance floor is instead enforced downstream, at
# the Spec Classification checkpoint.
_IMPORTANCE_FLOORABLE = {TaskType.Simple, TaskType.Complex}

_LADDER = [EscalationTier.E0, EscalationTier.E1, EscalationTier.E2,
           EscalationTier.E3, EscalationTier.E4, EscalationTier.EH]


def entry_tier(category: TaskType, important: bool) -> EscalationTier:
    base = _ENTRY_TIERS[category]
    if important and category in _IMPORTANCE_FLOORABLE:
        return EscalationTier.E2
    return base


def next_tier(current: EscalationTier, trigger: TriggerType) -> EscalationTier:
    """Where a trigger sends the task from its current tier."""
    # Hard triggers bypass the ladder (§6) — a hard trigger means "human",
    # which is EH, never the E4 coding tier (fable-coding-tier spec §3.2)
    if trigger == TriggerType.IrreversibleOperation:
        return EscalationTier.EH
    if trigger == TriggerType.GlobalCircuitBreaker:
        return EscalationTier.EH
    if trigger == TriggerType.DiscoveredImportance:
        # Jump to the planning treatment: Opus plans, and _classify_plan's
        # importance floor guarantees Opus execution
        return EscalationTier.E2

    # Discovered ambiguity routes laterally to the Spec loop, not up the model ladder (§5.3)
    if trigger == TriggerType.DiscoveredAmbiguity:
        return EscalationTier.E1

    # Plan breakage sends the reactive portion to replanning (§5.4)
    if trigger == TriggerType.PlanBreakage:
        return EscalationTier.E2 if current < EscalationTier.E2 else _step_up(current)

    # Everything else: one rung up
    return _step_up(current)


def _step_up(current: EscalationTier) -> EscalationTier:
    idx = _LADDER.index(current)
    return _LADDER[min(idx + 1, len(_LADDER) - 1)]

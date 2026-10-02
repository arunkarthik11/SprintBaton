"""Escalation trigger evaluation (escalation spec §5-§6).

Triggers are concrete and machine-observable so the orchestrator can act
without a human. Signals are collected from the execution response and the
task's accumulated state.
"""

from dataclasses import dataclass, field

from sprintbaton.config.settings import EscalationConfig
from sprintbaton.entities.enums import TriggerType


@dataclass
class ExecutionSignals:
    file_edit_counts: dict[str, int] = field(default_factory=dict)
    consecutive_check_failures: int = 0
    no_progress_steps: int = 0
    asked_question: bool = False        # hedging / mid-execution question
    plan_broken: bool = False           # reactive replanning required
    importance_flags: list[str] = field(default_factory=list)
    irreversible_attempt: bool = False
    tier_tokens_used: int = 0
    tier_wall_clock_seconds: float = 0.0
    total_tokens_used: int = 0          # across all tiers (circuit breaker)
    review_failed: bool = False


def evaluate_triggers(signals: ExecutionSignals, config: EscalationConfig) -> list[TriggerType]:
    """Returns fired triggers, hard triggers first (they take precedence)."""
    fired: list[TriggerType] = []

    # Hard triggers (§6) — blast radius, not capability
    if signals.irreversible_attempt:
        fired.append(TriggerType.IrreversibleOperation)
    if signals.total_tokens_used > config.global_token_cap:
        fired.append(TriggerType.GlobalCircuitBreaker)
    if signals.importance_flags:
        fired.append(TriggerType.DiscoveredImportance)

    # Graduated triggers (§5)
    if signals.asked_question:
        fired.append(TriggerType.DiscoveredAmbiguity)
    if signals.plan_broken:
        fired.append(TriggerType.PlanBreakage)
    if any(count >= config.region_edit_limit for count in signals.file_edit_counts.values()) \
            or signals.no_progress_steps >= config.noprogress_window:
        fired.append(TriggerType.Thrashing)
    if signals.consecutive_check_failures >= config.fix_attempts:
        fired.append(TriggerType.CorrectnessStall)
    if signals.tier_tokens_used > config.tier_token_budget \
            or signals.tier_wall_clock_seconds > config.tier_wall_clock_seconds:
        fired.append(TriggerType.BudgetExhaustion)
    if signals.review_failed:
        fired.append(TriggerType.ReviewFailure)

    return fired

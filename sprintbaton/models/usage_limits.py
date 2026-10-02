"""UsageLimitPolicy — pause-or-continue decisions for harness-reported
usage/rate/budget constraints (usage-limit-aware execution spec §4.4).

Plays the same role guard.py plays for irreversibility: the single place the
env-var kill switch and the task-level "urgent" override are evaluated, reused
by every orchestrator call site. Deliberately NOT an escalation trigger — a
usage limit says nothing about model capability (spec §3); the correct
response is "come back later automatically, same tier, same role", never a
ladder move.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from sprintbaton.entities.base import now_millis
from sprintbaton.entities.task import Task
from sprintbaton.harness.base import UsageLimitSignal


@dataclass(frozen=True)
class UsageLimitDecision:
    should_pause: bool
    resume_at: int | None    # epoch millis; None only when should_pause is False
    reason: str              # human-readable, becomes Task.usageLimitScope / the log line


@runtime_checkable
class UsageLimitPolicy(Protocol):
    def evaluate(self, task: Task, signals: list[UsageLimitSignal]) -> UsageLimitDecision: ...


_CONTINUE = UsageLimitDecision(should_pause=False, resume_at=None, reason="")


class DefaultUsageLimitPolicy:
    """Wraps the env-var kill switch and the task-level override — with
    aware=False it is itself a complete no-op, so ServiceContext always
    carries a policy instance rather than a nullable one (spec §4.4)."""

    def __init__(self, aware: bool, urgent_label: str, default_backoff_seconds: int):
        self._aware = aware
        self._urgent_label = urgent_label
        self._default_backoff_seconds = default_backoff_seconds

    def evaluate(self, task: Task, signals: list[UsageLimitSignal]) -> UsageLimitDecision:
        if not self._aware or not signals:
            return _CONTINUE
        if self._urgent_label and task.labels and self._urgent_label in task.labels:
            # Task-level override (spec §7.1): skip SprintBaton's own
            # pause-and-wait and let the harness call's own outcome stand —
            # this never retroactively grants quota; a reactive rejection
            # simply flows into the turn's ordinary failure handling.
            return _CONTINUE
        # max, not min: when several scopes are exhausted at once (session AND
        # weekly), the run cannot succeed until every one has cleared —
        # waiting only for the sooner reset would just re-fail immediately on
        # the other scope (spec §4.4).
        known_resets = [s.resets_at for s in signals if s.resets_at is not None]
        resume_at = (max(known_resets) if known_resets
                     else now_millis() + self._default_backoff_seconds * 1000)
        reason = ", ".join(sorted({s.scope for s in signals}))
        return UsageLimitDecision(should_pause=True, resume_at=resume_at, reason=reason)

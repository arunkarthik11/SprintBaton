"""The Observer — escalation/router telemetry (escalation spec §10).

Every escalation transition emits a structured event carrying: task id, Router
category, from-tier, to-tier, trigger, and outcome. These feed the Router
calibration loop (escalation rate per category is the Router's error signal).
"""

import logging

from opentelemetry import metrics

log = logging.getLogger(__name__)


class Observer:
    def __init__(self):
        meter = metrics.get_meter("sprintbaton.observer")
        self._tasks_processed = meter.create_counter(
            "sprintbaton.tasks.processed", description="Tasks processed, by router category"
        )
        self._escalations = meter.create_counter(
            "sprintbaton.escalations",
            description="Escalation transitions, by category/trigger/from/to tier",
        )
        self._human_handoffs = meter.create_counter(
            "sprintbaton.human_handoffs", description="EH human handoffs"
        )
        self._hard_triggers = meter.create_counter(
            "sprintbaton.hard_triggers",
            description="Hard-trigger halts (irreversible ops expected near-zero)",
        )
        self._tokens = meter.create_counter(
            "sprintbaton.tokens.used", description="Model tokens used, by role and model"
        )

    def task_processed(self, category: str, outcome: str) -> None:
        self._tasks_processed.add(1, {"category": category, "outcome": outcome})

    def escalation(self, task_id: str, category: str, trigger: str,
                   from_tier: str, to_tier: str) -> None:
        attrs = {"category": category, "trigger": trigger,
                 "from_tier": from_tier, "to_tier": to_tier}
        self._escalations.add(1, attrs)
        log.info("escalation transition",
                 extra={"task_id": task_id, **attrs, "event": "escalation"})

    def human_handoff(self, task_id: str, category: str, trigger: str) -> None:
        self._human_handoffs.add(1, {"category": category, "trigger": trigger})
        log.warning("human handoff (EH)",
                    extra={"task_id": task_id, "category": category, "trigger": trigger,
                           "event": "human_handoff"})

    def hard_trigger(self, task_id: str, trigger: str) -> None:
        self._hard_triggers.add(1, {"trigger": trigger})
        log.warning("hard trigger fired",
                    extra={"task_id": task_id, "trigger": trigger, "event": "hard_trigger"})

    def tokens_used(self, role: str, model: str, input_tokens: int, output_tokens: int) -> None:
        self._tokens.add(input_tokens, {"role": role, "model": model, "direction": "input"})
        self._tokens.add(output_tokens, {"role": role, "model": model, "direction": "output"})

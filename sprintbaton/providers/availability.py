"""ProviderAvailabilityService — the per-user, cross-task quota-pool state the
fallback router reads (agent-fallback-limits spec §3.3, §3.4, §4).

Two independent things can make a Provider inactive, and routing never needs to
know which fired:

- **Reactive**: a harness reported a usage-limit signal during a real call; the
  orchestrator calls `mark_inactive` with the signal's reset time.
- **Proactive / self-imposed**: `is_available` computes the provider's spend in
  each declared rolling window from the recorded TaskActionEvent stream and,
  when a budget is currently breached, flips the row inactive itself — before
  any call is attempted.

State lives directly on the Provider row (spec §3.1's suggested default —
`Repository.metadataRevision`, the metadata pointer, is a precedent for
runtime state on a config entity), so a limit hit while running one task is visible to every other
task's routing, and it survives a `sprintbaton serve` restart.

Spend attribution (spec §9 open question): resolved here by mapping a provider
to the model ids of the AgentDefinitions that reference it, then summing the
TaskActionEvent usage for those models in the window. Unambiguous whenever pools
use distinct models/types (the intended multi-provider configuration); two
same-model pools would share attribution, a documented v1 limitation.
"""

from __future__ import annotations

import logging
from typing import Protocol

from sprintbaton.entities.agent_definition import AgentDefinition
from sprintbaton.entities.base import now_millis
from sprintbaton.entities.events import TaskActionEvent
from sprintbaton.entities.provider import Provider
from sprintbaton.storage.base import EntityDAO

log = logging.getLogger(__name__)


class _HasProviderName(Protocol):
    provider_name: str


class ProviderAvailabilityService:
    def __init__(self, provider_repo: EntityDAO[Provider],
                 agent_def_repo: EntityDAO[AgentDefinition],
                 event_repo: EntityDAO[TaskActionEvent]):
        self._providers = provider_repo
        self._defs = agent_def_repo
        self._events = event_repo

    # ------------------------------------------------------------- queries

    def is_available(self, provider_name: str, user_id: str) -> bool:
        """True when a provider is currently routable. A built-in with no
        persisted row is always available (its only constraint is a reactive
        signal, which can't be tracked without a row — the graceful degrade to
        today's per-task pause). A persisted row is unavailable while inactive
        (until its reset passes) or while any self-imposed budget is breached."""
        provider = self._providers.find_one(
            {"name": provider_name, "userId": user_id})
        if provider is None:
            return True

        now = now_millis()
        if not provider.active:
            if provider.inactiveUntil is not None and now < provider.inactiveUntil:
                return False
            # The reset passed — clear the reactive/proactive pause and re-test
            # the budgets below (a budget may still be breached).
            provider.active = True
            provider.inactiveUntil = None
            provider.inactiveReason = None
            self._providers.save(provider)

        breach = self._breached_limit(provider, user_id, now)
        if breach is not None:
            resets_at, reason = breach
            self._flip_inactive(provider, user_id, resets_at, reason)
            return False
        return True

    def first_available(self, chain: list[_HasProviderName],
                         user_id: str) -> _HasProviderName | None:
        """The first chain entry whose provider is currently active (spec §4)."""
        for cfg in chain:
            if self.is_available(getattr(cfg, "provider_name", "") or "", user_id):
                return cfg
        return None

    def has_available_alternative(self, chain: list[_HasProviderName],
                                  user_id: str) -> bool:
        return self.first_available(chain, user_id) is not None

    # ------------------------------------------------------------- mutation

    def mark_inactive(self, provider_name: str, user_id: str,
                      resets_at: int | None, reason: str) -> bool:
        """Reactive path (spec §3.3): a signal fired for this provider. Persist
        the pause on its row so every task's routing sees it. Returns False for
        a built-in with no row — the caller then falls back to a per-task
        pause, today's behavior."""
        provider = self._providers.find_one(
            {"name": provider_name, "userId": user_id})
        if provider is None:
            return False
        self._flip_inactive(provider, user_id, resets_at, reason)
        return True

    def reactivate_due(self, user_id: str | None = None) -> list[str]:
        """Wake-scan sibling of UsageLimitWakeJob's task scan (spec §4.1):
        reactivate every inactive provider whose reset time has passed."""
        query: dict = {"active": False}
        if user_id is not None:
            query["userId"] = user_id
        now = now_millis()
        woken: list[str] = []
        for provider in self._providers.find(query):
            if provider.inactiveUntil is not None and now < provider.inactiveUntil:
                continue
            provider.active = True
            provider.inactiveUntil = None
            provider.inactiveReason = None
            provider.touch()
            self._providers.save(provider)
            woken.append(provider.name or provider.id)
        return woken

    # -------------------------------------------------------------- helpers

    def _flip_inactive(self, provider: Provider, user_id: str,
                       resets_at: int | None, reason: str) -> None:
        provider.active = False
        provider.inactiveUntil = resets_at
        provider.inactiveReason = reason
        provider.touch(modified_by=user_id)
        self._providers.save(provider)
        log.info("provider marked inactive", extra={
            "event": "provider_inactive", "provider": provider.name,
            "resets_at": resets_at, "reason": reason})

    def _breached_limit(self, provider: Provider, user_id: str,
                        now: int) -> tuple[int | None, str] | None:
        """The soonest-resetting currently-breached self-imposed limit, or None.
        Mirrors DefaultUsageLimitPolicy taking the max of cascading resets: the
        provider stays inactive until every breached window clears, so the reset
        we record is the latest window boundary among the breaches."""
        if not provider.tokenLimits:
            return None
        model_ids = self._model_ids_for(provider, user_id)
        if not model_ids:
            return None
        latest_reset = 0
        breached = False
        for limit in provider.tokenLimits:
            window_millis = limit.windowSeconds * 1000
            spend = self._spend(user_id, model_ids, now - window_millis)
            if spend >= limit.maxTokens:
                breached = True
                latest_reset = max(latest_reset, now + window_millis)
        if not breached:
            return None
        return latest_reset, "self-imposed token budget exceeded"

    def _model_ids_for(self, provider: Provider, user_id: str) -> set[str]:
        return {
            d.modelId for d in self._defs.find(
                {"modelProvider": provider.name, "userId": user_id})
            if d.modelId
        }

    def _spend(self, user_id: str, model_ids: set[str], since: int) -> int:
        total = 0
        for event in self._events.find({"userId": user_id}):
            if event.createdTime >= since and event.modelId in model_ids:
                total += event.usage.totalTokens
        return total

"""TokenUsageQueryEngine — the read/aggregation layer over TaskActionEvent
(token-usage-reporting spec). Pure query logic: fetches by the equality
filters EntityDAO actually supports, then filters/aggregates in Python — the
persistence-abstraction spec's equality-only find() contract stays untouched
(spec §3.3/§3.4), at the accepted cost of a userId-bounded full scan (§9).

Axis keys, per the spec's §3 trace:
- "agent" is `modelId` — the identity actually recorded today (an explicit
  agentDefinitionId is a flagged future addition, §3.2).
- "task state" is `action`, not `toStatus` — several record sites persist
  toStatus inconsistently (pre-transition, or unchanged for whole phases like
  conflict_resolution), while `action` is set fresh and correctly at every
  one (§3.3).
"""

from dataclasses import dataclass

from sprintbaton.entities.events import TaskActionEvent
from sprintbaton.entities.usage import TokenUsage
from sprintbaton.storage.base import EntityDAO


@dataclass(frozen=True)
class TimeBucketUsage:
    bucket_start_millis: int     # epoch millis, aligned to the bucket width
    usage: TokenUsage
    event_count: int


@dataclass(frozen=True)
class UsageAggregate:
    usage: TokenUsage
    event_count: int
    task_ids: frozenset[str]     # distinct tasks contributing

    @property
    def task_count(self) -> int:
        return len(self.task_ids)


# Week buckets align to 604_800_000-ms boundaries from the Unix epoch
# (a Thursday), not ISO calendar weeks — a documented approximation (§12).
_BUCKET_MILLIS = {"hour": 3_600_000, "day": 86_400_000, "week": 604_800_000}


class TokenUsageQueryEngine:
    def __init__(self, event_repo: EntityDAO[TaskActionEvent]):
        self._repo = event_repo

    def _window(self, user_id: str, since_millis: int, until_millis: int,
                *, repo_id: str | None = None,
                task_id: str | None = None) -> list[TaskActionEvent]:
        query: dict[str, str] = {"userId": user_id}
        if repo_id:
            query["repoId"] = repo_id
        if task_id:
            query["taskId"] = task_id
        events = self._repo.find(query)
        return [e for e in events if since_millis <= e.createdTime < until_millis]

    # ---- Axis 1: usage over time -----------------------------------------

    def usage_timeseries(self, user_id: str, since_millis: int, until_millis: int, *,
                         bucket: str = "day",
                         repo_id: str | None = None) -> list[TimeBucketUsage]:
        if bucket not in _BUCKET_MILLIS:
            raise KeyError(f"unknown bucket {bucket!r} "
                           f"(known: {', '.join(_BUCKET_MILLIS)})")
        width = _BUCKET_MILLIS[bucket]
        buckets: dict[int, list[TaskActionEvent]] = {}
        for event in self._window(user_id, since_millis, until_millis, repo_id=repo_id):
            start = event.createdTime - (event.createdTime % width)
            buckets.setdefault(start, []).append(event)
        return [
            TimeBucketUsage(
                bucket_start_millis=start,
                usage=sum((e.usage for e in events), TokenUsage()),
                event_count=len(events),
            )
            for start, events in sorted(buckets.items())
        ]

    # ---- Axis 2: usage by task -------------------------------------------

    def usage_by_task(self, user_id: str, since_millis: int, until_millis: int, *,
                      repo_id: str | None = None,
                      task_id: str | None = None,
                      ) -> dict[str, dict[str, dict[str, TokenUsage]]]:
        """task_id -> action ("task state") -> modelId ("agent") -> TokenUsage."""
        result: dict[str, dict[str, dict[str, TokenUsage]]] = {}
        for event in self._window(user_id, since_millis, until_millis,
                                  repo_id=repo_id, task_id=task_id):
            by_state = result.setdefault(event.taskId, {})
            by_agent = by_state.setdefault(event.action, {})
            agent_key = event.modelId or "(none)"
            by_agent[agent_key] = by_agent.get(agent_key, TokenUsage()) + event.usage
        return result

    # ---- Axis 3: usage by agent ------------------------------------------

    def usage_by_agent(self, user_id: str, since_millis: int, until_millis: int, *,
                       repo_id: str | None = None) -> dict[str, UsageAggregate]:
        return self._group_by(user_id, since_millis, until_millis,
                              repo_id=repo_id, key=lambda e: e.modelId or "(none)")

    # ---- Axis 4: usage by task state -------------------------------------

    def usage_by_task_state(self, user_id: str, since_millis: int, until_millis: int, *,
                            repo_id: str | None = None) -> dict[str, UsageAggregate]:
        return self._group_by(user_id, since_millis, until_millis,
                              repo_id=repo_id, key=lambda e: e.action)

    def _group_by(self, user_id: str, since_millis: int, until_millis: int, *,
                  repo_id: str | None,
                  key) -> dict[str, UsageAggregate]:
        groups: dict[str, list[TaskActionEvent]] = {}
        for event in self._window(user_id, since_millis, until_millis, repo_id=repo_id):
            groups.setdefault(key(event), []).append(event)
        return {
            k: UsageAggregate(
                usage=sum((e.usage for e in events), TokenUsage()),
                event_count=len(events),
                task_ids=frozenset(e.taskId for e in events),
            )
            for k, events in groups.items()
        }

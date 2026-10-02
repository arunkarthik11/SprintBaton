"""Token usage report routes (token-usage-reporting spec §7) — the read-only
HTTP twin of `sprintbaton reports`, ownership-scoped like every other route
(the userId scoping is structural: every query starts from the bearer user).
The query engine's dynamic-dict returns are adapted into list-based response
models at this boundary only (§7.1) — dynamic dict keys don't document well
in OpenAPI and dict ordering isn't a contract."""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from sprintbaton.api.app import ApiState
from sprintbaton.api.auth import api_state, require_user
from sprintbaton.entities.base import now_millis
from sprintbaton.entities.usage import TokenUsage
from sprintbaton.entities.user import User

router = APIRouter(prefix="/reports/usage", tags=["reports"])

MILLIS_PER_DAY = 86_400_000


def _window(state: ApiState, days: int | None, since: int | None,
            until: int | None) -> tuple[int, int]:
    """Same precedence as the CLI's _default_window (§6): explicit epoch-millis
    since/until win; otherwise days; otherwise the configured default."""
    now = now_millis()
    default_days = state.settings.sprintbaton_report_default_days
    if since is not None or until is not None:
        return (since if since is not None else now - default_days * MILLIS_PER_DAY,
                until if until is not None else now)
    days = days if days is not None else default_days
    return now - days * MILLIS_PER_DAY, now


# --- response models (§7.1) ---------------------------------------------------

class TimeBucketView(BaseModel):
    bucketStart: int
    bucketLabel: str          # ISO string computed from bucketStart, display-only
    usage: TokenUsage
    eventCount: int


class TimeseriesResponse(BaseModel):
    since: int
    until: int
    bucket: str
    buckets: list[TimeBucketView]
    total: TokenUsage


class TaskAgentUsage(BaseModel):
    modelId: str
    usage: TokenUsage


class TaskStateUsage(BaseModel):
    action: str
    agents: list[TaskAgentUsage]
    usage: TokenUsage                 # sum across agents within this state


class TaskUsageView(BaseModel):
    taskId: str
    taskTitle: str | None             # best-effort join (§8); None if hard-deleted
    states: list[TaskStateUsage]
    usage: TokenUsage                 # sum across the whole task


class ByTaskResponse(BaseModel):
    since: int
    until: int
    tasks: list[TaskUsageView]
    total: TokenUsage


class AggregateView(BaseModel):
    key: str                          # model id (by-agent) or action (by-state)
    usage: TokenUsage
    eventCount: int
    taskCount: int


class AggregateResponse(BaseModel):
    since: int
    until: int
    groups: list[AggregateView]
    total: TokenUsage


# --- routes -------------------------------------------------------------------

@router.get("/timeseries", response_model=TimeseriesResponse)
def timeseries(days: int | None = None, since: int | None = None,
               until: int | None = None, bucket: str = "day",
               repo_id: str | None = None,
               user: User = Depends(require_user),
               state: ApiState = Depends(api_state)) -> TimeseriesResponse:
    since_ms, until_ms = _window(state, days, since, until)
    try:
        buckets = state.usage_reports.usage_timeseries(
            user.id, since_ms, until_ms, bucket=bucket, repo_id=repo_id)
    except KeyError as e:
        raise HTTPException(status_code=422, detail=str(e.args[0]))
    views = [
        TimeBucketView(
            bucketStart=b.bucket_start_millis,
            bucketLabel=datetime.fromtimestamp(
                b.bucket_start_millis / 1000, tz=timezone.utc).isoformat(),
            usage=b.usage, eventCount=b.event_count,
        )
        for b in buckets
    ]
    return TimeseriesResponse(
        since=since_ms, until=until_ms, bucket=bucket, buckets=views,
        total=sum((b.usage for b in buckets), TokenUsage()))


@router.get("/by-task", response_model=ByTaskResponse)
def by_task(days: int | None = None, since: int | None = None,
            until: int | None = None, repo_id: str | None = None,
            task_id: str | None = None,
            user: User = Depends(require_user),
            state: ApiState = Depends(api_state)) -> ByTaskResponse:
    since_ms, until_ms = _window(state, days, since, until)
    breakdown = state.usage_reports.usage_by_task(
        user.id, since_ms, until_ms, repo_id=repo_id, task_id=task_id)
    tasks = []
    grand = TokenUsage()
    for tid, states in breakdown.items():
        state_views = []
        task_usage = TokenUsage()
        for action, by_agent in states.items():
            agents = [TaskAgentUsage(modelId=model_id, usage=usage)
                      for model_id, usage in by_agent.items()]
            state_usage = sum((a.usage for a in agents), TokenUsage())
            task_usage = task_usage + state_usage
            state_views.append(TaskStateUsage(action=action, agents=agents,
                                              usage=state_usage))
        grand = grand + task_usage
        # Best-effort title join, ownership-checked like everything else —
        # a foreign or hard-deleted task id simply renders no title.
        task = state.tasks.get(tid)
        title = task.title if task is not None and task.userId == user.id else None
        tasks.append(TaskUsageView(taskId=tid, taskTitle=title,
                                   states=state_views, usage=task_usage))
    tasks.sort(key=lambda t: -t.usage.totalTokens)
    return ByTaskResponse(since=since_ms, until=until_ms, tasks=tasks, total=grand)


def _aggregate_response(since_ms: int, until_ms: int,
                        aggregates) -> AggregateResponse:
    groups = [
        AggregateView(key=key, usage=a.usage, eventCount=a.event_count,
                      taskCount=a.task_count)
        for key, a in sorted(aggregates.items(),
                             key=lambda kv: -kv[1].usage.totalTokens)
    ]
    return AggregateResponse(
        since=since_ms, until=until_ms, groups=groups,
        total=sum((g.usage for g in groups), TokenUsage()))


@router.get("/by-agent", response_model=AggregateResponse)
def by_agent(days: int | None = None, since: int | None = None,
             until: int | None = None, repo_id: str | None = None,
             user: User = Depends(require_user),
             state: ApiState = Depends(api_state)) -> AggregateResponse:
    since_ms, until_ms = _window(state, days, since, until)
    return _aggregate_response(since_ms, until_ms, state.usage_reports.usage_by_agent(
        user.id, since_ms, until_ms, repo_id=repo_id))


@router.get("/by-state", response_model=AggregateResponse)
def by_state(days: int | None = None, since: int | None = None,
             until: int | None = None, repo_id: str | None = None,
             user: User = Depends(require_user),
             state: ApiState = Depends(api_state)) -> AggregateResponse:
    since_ms, until_ms = _window(state, days, since, until)
    return _aggregate_response(since_ms, until_ms,
                               state.usage_reports.usage_by_task_state(
                                   user.id, since_ms, until_ms, repo_id=repo_id))

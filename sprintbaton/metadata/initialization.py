"""Project initialization runs — creation, the TaskPending gate, and the status
view (docs/project-initialization-task-spec.md §4-§5, §10).

The metadata init pass is an invisible, queued `Task` with
`kind == ProjectInitialization`. Writers (project onboarding, `sprintbaton
init`, the API) only ever **persist the row**; the polling lane discovers and
enqueues it (§5.2), so crash recovery, usage-limit pause/wake, sandboxing and
fallback chains all apply unchanged. This module owns everything about a run
that is not executing it — the orchestrator's `_initialize_project` does that.

Every run creation, gate override, and pointer swap happens under the one
`project-init:<projectId>` lock (§5.1), which `apply_project` also holds across
its whole upsert.
"""

import logging

from pydantic import BaseModel

from sprintbaton.entities.base import now_millis
from sprintbaton.entities.enums import (
    InitializationTrigger,
    MetadataScope,
    TaskKind,
    TaskStatus,
)
from sprintbaton.entities.project import Project
from sprintbaton.entities.repository import Repository
from sprintbaton.entities.task import MetadataRepoProgress, Task
from sprintbaton.metadata.revisions import held_init_lock
from sprintbaton.storage.base import DistributedLock, EntityDAO

log = logging.getLogger(__name__)

MAX_GUIDANCE_CHARS = 4000


class InitializationInProgress(Exception):
    """An explicit run was requested while the project already has an
    unfinished one (spec §5.1) — at most one per project."""

    def __init__(self, task: Task):
        super().__init__(f"an initialization run is already in progress "
                         f"for this project (task {task.id})")
        self.task = task


class NoMetadataProduced(RuntimeError):
    """A run whose repo loop left no member repo with a published revision —
    every repo was skipped for lack of a remoteUrl (spec §5.7). No metadata is
    always a failure, never a silent success."""


class MetadataPassFailed(RuntimeError):
    """A repo or project pass exhausted its validation or continuation budget
    (spec §8.1) — permanent."""


# ------------------------------------------------------------------ the gate

def project_metadata_ready(project: Project) -> bool:
    """Is the TaskPending gate open for this project (spec §5.4)? Open when the
    project opted out, has been initialized at least once, or the user
    explicitly overrode the gate."""
    return (not project.generateMetadata
            or project.metadataInitializedAt is not None
            or project.metadataGateOverride)


# -------------------------------------------------------------------- lookup

def _run_filter(project: Project) -> dict:
    return {"kind": TaskKind.ProjectInitialization, "projectId": project.id,
            "userId": project.userId}


def unfinished_initialization_task(task_repo: EntityDAO[Task],
                                   project: Project) -> Task | None:
    """The project's one unfinished run, if any (spec §5.1: an equality find)."""
    return task_repo.find_one({**_run_filter(project),
                               "status": TaskStatus.ProjectInitialization})


def latest_initialization_task(task_repo: EntityDAO[Task],
                               project: Project) -> Task | None:
    """The most recently created run (spec §10.3), unfinished or not."""
    runs = task_repo.find(_run_filter(project))
    return max(runs, key=lambda t: (t.createdTime, t.id)) if runs else None


# ------------------------------------------------------------------ creation

def _new_run(project: Project, scope: MetadataScope,
             trigger: InitializationTrigger) -> Task:
    return Task(
        userId=project.userId,
        projectId=project.id,
        createdBy=project.userId,
        modifiedBy=project.userId,
        title=f"Initialize metadata: {project.title}",
        kind=TaskKind.ProjectInitialization,
        status=TaskStatus.ProjectInitialization,
        metadataScope=scope,
        initializationTrigger=trigger,
        # Snapshot: editing the project mid-run never changes a running pass.
        metadataGuidance=project.metadataGuidance,
    )


def _persist_run(task_repo: EntityDAO[Task], task: Task) -> Task:
    task_repo.save(task)
    log.info("initialization queued", extra={
        "event": "initialization_queued", "task_id": task.id,
        "project_id": task.projectId, "trigger": str(task.initializationTrigger),
        "scope": str(task.metadataScope),
    })
    return task


def needs_automatic_initialization(project: Project,
                                   repos: list[Repository]) -> bool:
    """The auto-trigger's metadata condition (spec §5.1): never initialized,
    or a member repo with a remote has no published revision (a newly added
    repo). The opt-out and the unfinished-run check are the caller's."""
    if project.metadataInitializedAt is None:
        return True
    return any(r.remoteUrl and r.metadataRevision is None for r in repos)


def ensure_initialization_task(project: Project, repos: list[Repository], *,
                               task_repo: EntityDAO[Task]) -> Task | None:
    """Create the automatic run for a just-applied project, iff it opted in,
    has no unfinished run, and needs one (spec §5.1). Returns the created task,
    or None when nothing was created.

    **The caller must hold the project-init lock** — `apply_project` does,
    across its whole upsert. The lock is not re-entrant, so this function never
    takes it itself."""
    if not project.generateMetadata:
        return None
    if unfinished_initialization_task(task_repo, project) is not None:
        return None
    if not needs_automatic_initialization(project, repos):
        return None
    return _persist_run(task_repo, _new_run(
        project, MetadataScope.Missing, InitializationTrigger.ProjectApply))


def request_initialization(project: Project, *, task_repo: EntityDAO[Task],
                           lock: DistributedLock,
                           scope: MetadataScope = MetadataScope.All,
                           trigger: InitializationTrigger) -> Task:
    """An explicit run from `sprintbaton init` or the API (spec §5.1). Ignores
    generateMetadata — an explicit request wins. Raises
    InitializationInProgress when an unfinished run exists."""
    with held_init_lock(lock, project.id):
        existing = unfinished_initialization_task(task_repo, project)
        if existing is not None:
            raise InitializationInProgress(existing)
        return _persist_run(task_repo, _new_run(project, scope, trigger))


def set_metadata_gate_override(project: Project, open_gate: bool, *,
                               project_repo: EntityDAO[Project],
                               lock: DistributedLock,
                               user_id: str) -> Project:
    """Set Project.metadataGateOverride (spec §5.4, §10.2) — the documented
    remedy when a first run failed and the user chooses to proceed without
    metadata. Under the project-init lock, re-reading the row, because the
    Project row is saved whole."""
    with held_init_lock(lock, project.id):
        current = project_repo.get(project.id) or project
        current.metadataGateOverride = open_gate
        current.touch(modified_by=user_id)
        project_repo.save(current)
    log.info("metadata gate overridden", extra={
        "event": "metadata_gate_overridden", "project_id": current.id,
        "user_id": user_id, "open": open_gate,
        "gate_open": project_metadata_ready(current),
    })
    return current


# --------------------------------------------------------------- status view

class MetadataStatusResponse(BaseModel):
    """The shared CLI/API status view of a project's latest init run
    (spec §10.4)."""

    taskId: str | None = None
    status: str = "none"  # none|pending|running|retrying|paused|stale|completed|failed
    trigger: InitializationTrigger | None = None
    scope: MetadataScope | None = None
    createdAt: int | None = None
    retryAfter: int | None = None
    transientFailures: int = 0
    lastTransientError: str | None = None
    error: str | None = None
    repos: list[MetadataRepoProgress] = []
    metadataRevision: str | None = None
    metadataUrl: str | None = None
    metadataInitializedAt: int | None = None
    gateOpen: bool = False
    gateOverridden: bool = False
    remedies: list[str] = []


def remedies_for(project: Project) -> list[str]:
    """The three documented remedies for a failed run with the gate closed
    (spec §5.7)."""
    return [
        f"fix the cause, then re-run `sprintbaton init --project \"{project.title}\"` "
        f"(or POST /projects/{project.id}/metadata)",
        f"let board tasks run without metadata for now: `sprintbaton project "
        f"metadata-gate \"{project.title}\" --open` "
        f"(or PUT /projects/{project.id}/metadata/gate)",
        "opt out of metadata generation entirely: set `generateMetadata: false` "
        "in the project manifest and re-apply it",
    ]


def run_status(task: Task | None, stale_after_seconds: int,
               now: int | None = None) -> str:
    """Status derivation, first match wins (spec §10.4)."""
    if task is None:
        return "none"
    now = now if now is not None else now_millis()
    if task.status == TaskStatus.Shipped:
        return "completed"
    if task.status == TaskStatus.Blocked:
        return "failed"
    if task.usageLimitPaused:
        return "paused"
    if task.processingClaimedAt is not None:
        fresh = now - task.processingClaimedAt < stale_after_seconds * 1000
        return "running" if fresh else "stale"
    if task.retryAfter is not None and task.retryAfter > now:
        return "retrying"
    return "pending"


def initialization_status_view(project: Project, task: Task | None, *,
                               stale_after_seconds: int,
                               now: int | None = None) -> MetadataStatusResponse:
    status = run_status(task, stale_after_seconds, now)
    gate_open = project_metadata_ready(project)
    return MetadataStatusResponse(
        taskId=task.id if task else None,
        status=status,
        trigger=task.initializationTrigger if task else None,
        scope=task.metadataScope if task else None,
        createdAt=task.createdTime if task else None,
        retryAfter=task.retryAfter if task else None,
        transientFailures=task.transientFailures if task else 0,
        lastTransientError=task.lastTransientError if task else None,
        error=task.initializationError if task else None,
        repos=list(task.metadataRepoProgress) if task else [],
        metadataRevision=project.metadataRevision,
        metadataUrl=project.metadataUrl,
        metadataInitializedAt=project.metadataInitializedAt,
        gateOpen=gate_open,
        gateOverridden=project.metadataGateOverride,
        remedies=(remedies_for(project)
                  if status == "failed" and not gate_open else []),
    )


def failed_closed_gates(project_repo: EntityDAO[Project],
                        task_repo: EntityDAO[Task]) -> list[tuple[Project, Task]]:
    """Every project whose gate is closed behind a failed latest run — the
    `sprintbaton serve` startup summary (spec §5.7 channel 4)."""
    failures = []
    for project in project_repo.find({}):
        if project_metadata_ready(project):
            continue
        task = latest_initialization_task(task_repo, project)
        if task is not None and task.status == TaskStatus.Blocked:
            failures.append((project, task))
    return failures

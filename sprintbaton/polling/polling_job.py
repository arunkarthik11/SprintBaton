"""PendingTaskPollingJob — periodically polls the todolist for tasks queued for
the agent, ingests them into entity storage, and queues them for the orchestrator.
"Queued for the agent" is the adapter's own concern (todoist-label-routing spec
§2.1) — the polling job only asks list_agent_tasks and never sees the mechanism
(Todoist: the sprintbaton-agent label).

One loop enumerates every active Project row directly — across every user, with
no per-user job lifecycle (repository-onboarding spec §8; multi-repo-project spec
§11.2). Each Project owns one todolist board; polling reads its columns/board and
stamps each ingested Task with the project's owner and id.

The same loop is also the handoff for project initialization runs
(docs/project-initialization-task-spec.md §5.2): writers only persist an init
Task row, and this lane discovers and enqueues it — which makes discovery the
retry scheduler too, since a row waiting out its backoff is skipped until its
retryAfter passes.
"""

import logging
import threading
from typing import Callable

from sprintbaton.adaptors.base import ExternalTask, TaskAdapter
from sprintbaton.entities.base import now_millis
from sprintbaton.entities.enums import TaskKind, TaskStatus
from sprintbaton.entities.project import Project, column_to_status
from sprintbaton.entities.task import Task
from sprintbaton.entities.task_revision import (
    CardSnapshot,
    TaskRevision,
    content_fingerprint,
    latest_revision,
)
from sprintbaton.storage.base import EntityDAO, TaskQueue

log = logging.getLogger(__name__)


# Re-exported: the mapping moved next to ColumnConfig so the orchestrator's
# reconcile pass can use it without importing the polling job.
__all__ = ["PendingTaskPollingJob", "column_to_status"]


class PendingTaskPollingJob:
    def __init__(
        self,
        adapter_for: Callable[[Project], TaskAdapter],
        task_repo: EntityDAO[Task],
        project_repo: EntityDAO[Project],
        queue: TaskQueue,
        interval_seconds: int = 60,
        *,
        snapshot_repo: EntityDAO[CardSnapshot] | None = None,
        revision_repo: EntityDAO[TaskRevision] | None = None,
    ):
        # Per-project adapter construction (the todolist board lives on the
        # Project now — multi-repo-project spec §4.1) — normally
        # ServiceContext.task_adapter_for.
        self._adapter_for = adapter_for
        self._task_repo = task_repo
        self._project_repo = project_repo
        self._queue = queue
        self._interval = interval_seconds
        # The poller's only writes on an existing task (task-revisions spec
        # §4): the card's current look and its content history. None only in
        # unit tests that exercise intake alone.
        self._snapshots = snapshot_repo
        self._revisions = revision_repo
        self._stop = threading.Event()

    # -------------------------------------------------------------- lifecycle

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self._loop, name="polling-job", daemon=True)
        thread.start()
        return thread

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        log.info("polling job started", extra={"interval": self._interval})
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:
                log.exception("polling cycle failed")
            self._stop.wait(self._interval)

    # ------------------------------------------------------------------ work

    def poll_once(self) -> int:
        enqueued = 0
        # active=False pauses intake only (spec §3.1); find() already
        # excludes soft-deleted rows.
        for project in self._project_repo.find({"active": True}):
            try:
                adapter = self._adapter_for(project)
                for external in adapter.list_agent_tasks(project.boardId):
                    task = self._ingest(external, project)
                    self._queue.enqueue_task(task.id)
                    enqueued += 1
            except Exception:
                # One tenant's bad token or dead board must not stall the
                # whole multi-user loop.
                log.exception("polling failed for project",
                              extra={"project_id": project.id})
        enqueued += self._discover_initialization_runs()
        if enqueued:
            log.info("tasks enqueued", extra={"count": enqueued})
        return enqueued

    def _discover_initialization_runs(self) -> int:
        """Enqueue every due, unfinished init run (spec §5.2). Inactive projects
        are included (`active: False` pauses board intake, not setup); claimed
        rows may be mid-run in another worker and stale claims are the reconcile
        pass's job; usage-limit-paused rows belong to UsageLimitWakeJob. The
        queue's de-dupe set absorbs repeats."""
        now = now_millis()
        enqueued = 0
        try:
            runs = self._task_repo.find({"kind": TaskKind.ProjectInitialization,
                                         "status": TaskStatus.ProjectInitialization})
        except Exception:
            log.exception("initialization run discovery failed")
            return 0
        for task in runs:
            if task.processingClaimedAt is not None or task.usageLimitPaused:
                continue
            if task.retryAfter is not None and task.retryAfter > now:
                continue
            self._queue.enqueue_task(task.id)
            enqueued += 1
        return enqueued

    def current_round(self, external_id: str, user_id: str) -> Task | None:
        """The card's current round (task-revisions spec §5.3). Two current
        rows can exist transiently, across a crash between saving a new round
        and flipping the old one's flag (§9.2 step 3) — the highest round wins."""
        rounds = self._task_repo.find({"externalId": external_id, "userId": user_id,
                                       "isCurrentRound": True})
        return max(rounds, key=lambda t: t.round) if rounds else None

    def _ingest(self, external: ExternalTask, project: Project) -> Task:
        """Observe a card (task-revisions spec §5.3). Creates the Task row the
        first time a card is seen; after that it NEVER saves the Task row —
        it upserts the CardSnapshot and appends a TaskRevision when the
        content fingerprint changed, and the orchestrator reconciles both at
        the start of the next process() (spec §4, invariant 1)."""
        fingerprint = content_fingerprint(external.title, external.description)
        task = self.current_round(external.externalId, project.userId)
        if task is None:
            task = Task(
                userId=project.userId,
                projectId=project.id,
                externalId=external.externalId,
                boardId=external.boardId,
                title=external.title,
                description=external.description,
                priority=external.priority,
                labels=external.labels or None,
                attachments=external.attachments or None,
                # An unmapped column starts at TaskPending and is parked by
                # the first reconcile (spec §6).
                status=(column_to_status(project.columns, external.columnId)
                        or TaskStatus.TaskPending),
                # The card starts where it was found — that placement is the
                # baseline, not a human move.
                lastSyncedColumnId=external.columnId or None,
            )
            self._task_repo.save(task)
            if self._revisions is not None:
                self._revisions.save(TaskRevision(
                    userId=task.userId, taskId=task.id, revision=1,
                    title=external.title, description=external.description,
                    fingerprint=fingerprint))
            self._save_snapshot(task, external, fingerprint)
            return task

        self._save_snapshot(task, external, fingerprint)
        if self._revisions is not None:
            latest = latest_revision(self._revisions, task)
            # Against the latest *recorded* revision, not currentRevision: a
            # revision can be recorded but not yet accepted (spec §5.3).
            if latest is None or latest.fingerprint != fingerprint:
                number = (latest.revision + 1) if latest else 1
                self._revisions.save(TaskRevision(
                    userId=task.userId, taskId=task.id, revision=number,
                    title=external.title, description=external.description,
                    fingerprint=fingerprint))
                log.info("card revision recorded", extra={
                    "event": "revision_recorded", "task_id": task.id,
                    "revision": number})
        return task

    def _save_snapshot(self, task: Task, external: ExternalTask,
                       fingerprint: str) -> None:
        if self._snapshots is None:
            return
        self._snapshots.save(CardSnapshot(
            id=task.id, userId=task.userId, taskId=task.id,
            columnId=external.columnId, fingerprint=fingerprint,
            title=external.title, description=external.description,
            labels=external.labels or None, priority=external.priority))

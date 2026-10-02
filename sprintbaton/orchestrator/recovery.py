"""Startup crash recovery (zero-infra-storage spec §4.3): the task queue is a
non-durable hint — what must survive a crash is "which tasks still need the
orchestrator," and that is answerable from the durable entity store.

On `sprintbaton serve` startup, before the polling job starts, this pass
re-enqueues every non-terminal task whose in-flight claim
(Task.processingClaimedAt, stamped by TaskOrchestrator.process) is older than
any plausible single process() call. Status alone can't distinguish "actively
running," "correctly waiting on a human" (a clarification pause has no status
change, conversation-lifecycle spec §3), and "orphaned by a crash" — the claim
marker is what separates the three: unclaimed tasks are either waiting on a
human or will be re-observed by the next poll; freshly claimed ones may be
mid-process in a live worker; only a *stale* claim means a crash.
"""

import logging

from sprintbaton.entities.base import now_millis
from sprintbaton.entities.enums import TaskStatus
from sprintbaton.entities.task import Task
from sprintbaton.storage.base import EntityDAO, TaskQueue

log = logging.getLogger(__name__)

# Statuses no orchestrator pass ever acts on again (Blocked resumes only via
# a human reassignment the polling job observes).
TERMINAL_STATUSES = frozenset({TaskStatus.Shipped, TaskStatus.Blocked})


def reconcile_orphaned_tasks(task_repo: EntityDAO[Task], queue: TaskQueue,
                             stale_after_seconds: int) -> list[str]:
    """Re-enqueue crash-orphaned tasks; returns the re-enqueued task ids.
    Queried per equality-only status filter (the EntityDAO contract) with the
    claim-staleness check applied in Python."""
    cutoff = now_millis() - stale_after_seconds * 1000
    requeued: list[str] = []
    for status in TaskStatus:
        if status in TERMINAL_STATUSES:
            continue
        for task in task_repo.find({"status": status}):
            if task.processingClaimedAt is None or task.processingClaimedAt >= cutoff:
                continue
            if not task.isCurrentRound:
                # A superseded round (task-revisions spec §10): process()
                # would return at once, so re-enqueueing it is pointless.
                continue
            queue.enqueue_task(task.id)
            requeued.append(task.id)
            log.warning("re-enqueued crash-orphaned task", extra={
                "task_id": task.id, "status": str(status),
                "claimed_at": task.processingClaimedAt,
            })
    return requeued

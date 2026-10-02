"""UsageLimitWakeJob — the re-entry mechanism for usage-limit-paused tasks
(usage-limit-aware execution spec §6).

Mirrors ReleaseWindowJob's shape (daemon thread, start/stop, a run_once a test
can call directly) but scans tasks instead of repos — usage limits attach to a
harness process/subscription, not a repository. Deliberately a periodic scan
over durable Task state, not a per-task timer: a scan naturally survives a
`sprintbaton serve` restart, the same principle processingClaimedAt's
reconciliation pass already established (zero-infra-storage spec §4.3).
"""

import logging
import threading

from sprintbaton.entities.base import now_millis
from sprintbaton.entities.task import Task
from sprintbaton.providers.availability import ProviderAvailabilityService
from sprintbaton.storage.base import EntityDAO, TaskQueue

log = logging.getLogger(__name__)


class UsageLimitWakeJob:
    def __init__(self, task_repo: EntityDAO[Task], task_queue: TaskQueue,
                 interval_seconds: int = 300,
                 provider_availability: ProviderAvailabilityService | None = None):
        self._task_repo = task_repo
        self._queue = task_queue
        self._interval = interval_seconds
        # Generalized to reactivate provider quota pools too (agent-fallback
        # spec §4.1): one job, two scans — paused tasks and inactive providers.
        self._provider_availability = provider_availability
        self._stop = threading.Event()

    # -------------------------------------------------------------- lifecycle

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self._loop, name="usage-limit-wake-job",
                                  daemon=True)
        thread.start()
        return thread

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        log.info("usage limit wake job started", extra={"interval": self._interval})
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:
                log.exception("usage limit wake check failed")
            self._stop.wait(self._interval)

    # ------------------------------------------------------------------ work

    def run_once(self) -> list[str]:
        """Equality-only filter (persistence-abstraction spec): scan the
        coarse usageLimitPaused=True marker, filter the precise time condition
        in Python — the same pattern orchestrator/recovery.py uses for
        staleness; this job does not grow the find() query DSL."""
        # Reactivate any provider whose reset time has passed first, so a task
        # re-enqueued below re-evaluates its chain against fresh availability.
        if self._provider_availability is not None:
            reactivated = self._provider_availability.reactivate_due()
            if reactivated:
                log.info("reactivated providers", extra={
                    "event": "provider_reactivated", "providers": reactivated})
        woken = []
        for task in self._task_repo.find({"usageLimitPaused": True}):
            if task.usageLimitPausedUntil is None or now_millis() < task.usageLimitPausedUntil:
                continue
            task.usageLimitPaused = False
            task.usageLimitPausedUntil = None
            task.usageLimitScope = None
            task.touch()
            self._task_repo.save(task)
            self._queue.enqueue_task(task.id)
            woken.append(task.id)
            log.info("woke usage-limit-paused task", extra={
                "event": "usage_limit_resumed", "task_id": task.id})
        return woken

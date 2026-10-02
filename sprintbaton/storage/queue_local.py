"""InProcessTaskQueue — the tool-mode TaskQueue backend (zero-infra-storage
spec §4.2). Stdlib-only and thread-safe: the polling job's background thread
is the producer, `sprintbaton serve`'s main thread the consumer — exactly
queue.Queue's intended use.

Deliberately no filesystem persistence: durability is the startup
reconciliation pass's job (spec §4.3), not this class's — the Redis-backed
queue provides no stronger guarantee today either (a popped-but-unprocessed
task has no ack/retry path on any backend).
"""

import queue
import threading


class InProcessTaskQueue:
    def __init__(self) -> None:
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._pending: set[str] = set()  # de-dupe, mirrors Redis's SADD guard
        self._lock = threading.Lock()

    def enqueue_task(self, task_id: str) -> None:
        with self._lock:
            if task_id in self._pending:
                return
            self._pending.add(task_id)
        self._queue.put(task_id)

    def dequeue_task(self, timeout_seconds: int = 5) -> str | None:
        try:
            task_id = self._queue.get(timeout=timeout_seconds)
        except queue.Empty:
            return None
        with self._lock:
            self._pending.discard(task_id)
        return task_id

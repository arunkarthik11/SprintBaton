"""FileLock — the tool-mode DistributedLock backend (zero-infra-storage spec
§5.2). flock() (stdlib fcntl, POSIX-only — consistent with this project's
Linux/WSL-only assumptions) gives real cross-OS-process mutual exclusion:
`sprintbaton serve` and a concurrent `sprintbaton repo create`/`init`
invocation are separate processes that can touch the same provenance bundle.

ttl_seconds has no flock() equivalent and is a no-op here — the OS releases a
flock the moment the holding process dies or its fd closes, which is a
*stronger* stuck-lock guarantee than Redis's SET NX EX expiry.
"""

import fcntl
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sprintbaton.storage.base import safe_segment as _safe_segment


class FileLock:
    def __init__(self, lock_dir: str):
        self._lock_dir = Path(lock_dir).expanduser()

    @contextmanager
    def lock(self, key: str, ttl_seconds: int = 60, wait_seconds: int = 30,
             poll_seconds: float = 0.1) -> Iterator[bool]:
        path = self._lock_dir / f"{_safe_segment(key)}.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR)
        deadline = time.monotonic() + wait_seconds
        acquired = False
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(poll_seconds)
            yield acquired
        finally:
            if acquired:
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

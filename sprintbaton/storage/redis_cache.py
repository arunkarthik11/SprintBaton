import logging
import time
import uuid
from contextlib import contextmanager
from typing import Iterator
from sprintbaton.dependencies import require_storage_module

log = logging.getLogger(__name__)

TASK_QUEUE_KEY = "sprintbaton:task_queue"


class RedisCache:
    """Redis client wearing three hats (zero-infra-storage spec §8): the
    hosted-mode TaskQueue (enqueue/dequeue), the hosted-mode DistributedLock
    (lock), and the optional backing for RedisConfigCache (get/set/delete)."""

    def __init__(self, uri: str):
        # Lazy import (zero-infra-storage spec §11.1): redis is the `redis`
        # extra; a tool-mode install never constructs this class.
        redis = require_storage_module("redis", package="redis", extra="redis",
                                       feature="the redis queue/lock/cache")

        self._redis_error = redis.RedisError
        self._client = redis.Redis.from_url(uri, decode_responses=True)

    # --- task queue -----------------------------------------------------

    def enqueue_task(self, task_id: str) -> None:
        key = TASK_QUEUE_KEY
        # Guard against double-enqueue while a task is already queued
        if self._client.sadd(f"{key}:members", task_id):
            self._client.lpush(key, task_id)

    def dequeue_task(self, timeout_seconds: int = 5) -> str | None:
        key = TASK_QUEUE_KEY
        item = self._client.brpop(key, timeout=timeout_seconds)
        if item is None:
            return None
        task_id = item[1]
        self._client.srem(f"{key}:members", task_id)
        return task_id

    # --- generic cache ----------------------------------------------------

    def get(self, key: str) -> str | None:
        return self._client.get(key)

    def set(self, key: str, value: str, ttl_seconds: int | None = None) -> None:
        self._client.set(key, value, ex=ttl_seconds)

    def delete(self, key: str) -> None:
        self._client.delete(key)

    def ping(self) -> bool:
        try:
            return bool(self._client.ping())
        except self._redis_error:
            return False

    # --- distributed lock -------------------------------------------------

    @contextmanager
    def lock(self, key: str, ttl_seconds: int = 60, wait_seconds: int = 30,
             poll_seconds: float = 0.1) -> Iterator[bool]:
        """Best-effort SET NX EX lock, serialising the per-project provenance
        bundle read-modify-write (classification provenance spec §4.1). Yields
        True if acquired, False if it timed out waiting — callers that need
        exclusivity should treat False as "skip this write". Never raises on a
        Redis error: provenance is a side-channel that must not fail a task."""
        token = uuid.uuid4().hex
        deadline = time.monotonic() + wait_seconds
        acquired = False
        try:
            while time.monotonic() < deadline:
                try:
                    if self._client.set(key, token, nx=True, ex=ttl_seconds):
                        acquired = True
                        break
                except self._redis_error:
                    break
                time.sleep(poll_seconds)
            yield acquired
        finally:
            if acquired:
                try:
                    if self._client.get(key) == token:
                        self._client.delete(key)
                except self._redis_error:
                    pass

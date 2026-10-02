"""ConfigCache — the pluggable cache behind UserService.settings_for
(user-multitenancy spec §5).

"memory" is correct for a single-process deployment (which `serve` is) and
needs no Redis — the CLI/SQLite install's default. "redis" becomes useful once
the API process (serve-api) and the worker (serve) are separate deployables
that need to observe each other's config writes without waiting out
independent in-memory TTLs.
"""

import json
import threading
import time
from typing import TYPE_CHECKING, Protocol

from sprintbaton.config.settings import Settings

if TYPE_CHECKING:
    from sprintbaton.storage.redis_cache import RedisCache


class ConfigCache(Protocol):
    def get(self, user_id: str) -> Settings | None: ...

    def set(self, user_id: str, settings: Settings) -> None: ...

    def delete(self, user_id: str) -> None: ...


class MemoryConfigCache:
    def __init__(self, ttl_seconds: int = 60):
        self._ttl = ttl_seconds
        self._entries: dict[str, tuple[Settings, float]] = {}
        self._lock = threading.Lock()

    def get(self, user_id: str) -> Settings | None:
        with self._lock:
            entry = self._entries.get(user_id)
            if entry is None:
                return None
            settings, expiry = entry
            if time.monotonic() >= expiry:
                del self._entries[user_id]
                return None
            return settings

    def set(self, user_id: str, settings: Settings) -> None:
        with self._lock:
            self._entries[user_id] = (settings, time.monotonic() + self._ttl)

    def delete(self, user_id: str) -> None:
        with self._lock:
            self._entries.pop(user_id, None)


class RedisConfigCache:
    KEY_PREFIX = "sprintbaton:user_settings:"

    def __init__(self, redis_cache: "RedisCache", ttl_seconds: int = 60):
        self._redis = redis_cache
        self._ttl = ttl_seconds

    def get(self, user_id: str) -> Settings | None:
        raw = self._redis.get(self.KEY_PREFIX + user_id)
        if raw is None:
            return None
        return Settings.model_validate(json.loads(raw))

    def set(self, user_id: str, settings: Settings) -> None:
        self._redis.set(self.KEY_PREFIX + user_id, settings.model_dump_json(),
                        ttl_seconds=self._ttl)

    def delete(self, user_id: str) -> None:
        self._redis.delete(self.KEY_PREFIX + user_id)


def build_config_cache(settings: Settings,
                       redis_cache: "RedisCache | None" = None) -> ConfigCache:
    """Fail-loudly cache factory, same shape as build_storage."""
    backend = settings.sprintbaton_config_cache_backend
    ttl = settings.sprintbaton_config_cache_ttl_seconds
    if backend == "memory":
        return MemoryConfigCache(ttl)
    if backend == "redis":
        if redis_cache is None:
            raise ValueError("config cache backend 'redis' needs a RedisCache")
        return RedisConfigCache(redis_cache, ttl)
    raise ValueError(f"unknown config cache backend: {backend!r}")

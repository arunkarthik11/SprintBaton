"""UserService — per-user config resolution (user-multitenancy spec §5).

The one thing standing between "a UserConfiguration row exists" and "the
system actually behaves differently for that user". Unlike AgentDefinitions
(resolved once at container build, restart to change — an accepted limitation
there), user configuration changes on a web-UI cadence, so this resolves
**per call, keyed by user_id**, behind a short-TTL cache that writes
invalidate immediately.

CLI mode degenerates to exactly today's behavior: with no UserConfiguration
row for LOCAL_USER_ID (which there never is unless someone deliberately
creates one), every field falls through to the deployment default (spec §10).
"""

import logging

from sprintbaton.config.settings import Settings
from sprintbaton.entities.user_config import (
    ESCALATION_OVERRIDES,
    SETTINGS_OVERRIDES,
    UserConfiguration,
)
from sprintbaton.storage.base import EntityDAO
from sprintbaton.users.config_cache import ConfigCache, MemoryConfigCache

log = logging.getLogger(__name__)


class UserService:
    def __init__(self, config_repo: EntityDAO[UserConfiguration],
                 settings: Settings, cache: ConfigCache | None = None):
        self._repo = config_repo
        self._settings = settings
        self._cache = cache if cache is not None else MemoryConfigCache()

    def settings_for(self, user_id: str) -> Settings:
        """Deployment-wide Settings, overridden field-by-field by the user's
        UserConfiguration (None fields fall through), cached with a short
        TTL."""
        cached = self._cache.get(user_id)
        if cached is not None:
            return cached
        resolved = self._resolve(user_id)
        self._cache.set(user_id, resolved)
        return resolved

    def configuration_for(self, user_id: str) -> UserConfiguration | None:
        return self._repo.find_one({"userId": user_id})

    def save_configuration(self, config: UserConfiguration) -> UserConfiguration:
        """Persist and invalidate immediately — a user's own edit must be
        visible on their next task, never lag behind the TTL (spec §5)."""
        existing = self._repo.find_one({"userId": config.userId})
        if existing is not None and existing.id != config.id:
            config.id = existing.id  # one configuration per user
        config.touch(modified_by=config.userId)
        self._repo.save(config)
        self.invalidate(config.userId)
        return config

    def invalidate(self, user_id: str) -> None:
        self._cache.delete(user_id)

    def _resolve(self, user_id: str) -> Settings:
        config = self._repo.find_one({"userId": user_id})
        if config is None:
            return self._settings

        updates: dict[str, object] = {}
        for config_field, settings_field in SETTINGS_OVERRIDES.items():
            value = getattr(config, config_field)
            if value is not None:
                updates[settings_field] = value
        if config.escalation is not None:
            for esc_field, settings_field in ESCALATION_OVERRIDES.items():
                updates[settings_field] = getattr(config.escalation, esc_field)
        if not updates:
            return self._settings
        resolved = self._settings.model_copy(update=updates)
        log.info("resolved per-user settings", extra={
            "user_id": user_id, "overridden": sorted(updates),
        })
        return resolved

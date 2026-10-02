import time
import uuid

from pydantic import BaseModel, Field

SYSTEM_USER_ID = "sprintbaton-system"
# The owner sentinel a CLI/local-mode process stamps on everything it creates
# (user-multitenancy spec §6): CLI mode has no login, so instead of skipping
# the tenant filter it is the case where the filter always resolves to this
# one fixed value — the code path stays identical between modes.
LOCAL_USER_ID = "local"


def now_millis() -> int:
    return int(time.time() * 1000)


def new_id(prefix: str = "ent") -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class BaseEntity(BaseModel):
    id: str = Field(default_factory=lambda: new_id())
    name: str | None = None
    createdBy: str = SYSTEM_USER_ID
    modifiedBy: str = SYSTEM_USER_ID
    createdTime: int = Field(default_factory=now_millis)
    modifiedTime: int = Field(default_factory=now_millis)
    deleted: bool = False  # soft deletion

    def touch(self, modified_by: str = SYSTEM_USER_ID) -> None:
        self.modifiedBy = modified_by
        self.modifiedTime = now_millis()


class DescriptionEntity(BaseEntity):
    title: str = ""
    description: str | None = None

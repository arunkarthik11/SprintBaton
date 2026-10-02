"""Release — a release window's batch as a unit of state (three-branch
promotion spec §5). One release groups the tasks swept from `dev` into
`staging` at a window boundary; its QA sign-off and ship decision fan out to
every task in the batch, which no per-task field could carry.
"""

from pydantic import Field, model_validator

from sprintbaton.entities.base import LOCAL_USER_ID, DescriptionEntity
from sprintbaton.entities.enums import ReleaseStatus


class Release(DescriptionEntity):
    # Tenant owner (user-multitenancy spec §6), inherited from the project
    userId: str = LOCAL_USER_ID
    # The owning Project (multi-repo-project spec §9; was repoId).
    projectId: str = ""
    status: ReleaseStatus = ReleaseStatus.InQA
    taskIds: list[str] = Field(default_factory=list)
    # One promotion/ship PR per member repo (multi-repo-project spec §9) — a
    # repo with nothing to promote is simply absent from the dict.
    promotionPrUrls: dict[str, str] = Field(default_factory=dict)  # repoId -> dev->staging PR
    shipPrUrls: dict[str, str] = Field(default_factory=dict)       # repoId -> staging->prod PR

    @model_validator(mode="before")
    @classmethod
    def _shim_legacy_repo_id(cls, data):
        if isinstance(data, dict) and "repoId" in data and "projectId" not in data:
            data = {**data, "projectId": data["repoId"]}
            data.pop("repoId", None)
        return data

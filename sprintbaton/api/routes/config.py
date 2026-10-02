"""UserConfiguration read/update (user-multitenancy spec §9). PATCH
invalidates the caller's UserService cache entry immediately (§5), so an edit
is visible on the user's next task, never lagging the TTL."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from sprintbaton.api.app import ApiState
from sprintbaton.api.auth import api_state, require_user
from sprintbaton.config.settings import EscalationConfig
from sprintbaton.entities.user import User
from sprintbaton.entities.user_config import UserConfiguration

router = APIRouter(prefix="/config", tags=["config"])

_CONFIG_FIELDS = [
    "sprintbatonOpusModel", "sprintbatonSonnetModel", "sprintbatonRouterModel",
    "sprintbatonCodingHarness",
    # The 15 agent bindings (provider-setup-cli spec §6.2) — what
    # `providers add --bind-roles` writes, editable here per user.
    "sprintbatonClassificationAgent",
    "sprintbatonFinalizationAgent",
    "sprintbatonAbstractFinalizationAgent",
    "sprintbatonPassingCriteriaAgent",
    "sprintbatonSpecClassificationAgent",
    "sprintbatonRepoScopingAgent",
    "sprintbatonRevisionClassificationAgent",
    "sprintbatonPlanningAgent",
    "sprintbatonPlanClassificationAgent",
    "sprintbatonReviewAgent",
    "sprintbatonConflictResolutionAgent",
    "sprintbatonMetadataGenerationAgent",
    "sprintbatonProjectMetadataGenerationAgent",
    "sprintbatonExecutionAgent",
    "sprintbatonExecutionTierAgents", "sprintbatonProvenanceEnabled",
    "pollIntervalSeconds", "releaseCheckIntervalSeconds",
    "conversationResumeWindowSeconds",
    # Project initialization tuning (project-initialization-task spec §12)
    "sprintbatonMetadataGenerationMaxTurns",
    "sprintbatonMetadataGenerationMaxContinuations",
    "sprintbatonMetadataValidationMaxRounds",
    "sprintbatonInitRetryMaxAttempts", "sprintbatonInitRetryBaseSeconds",
    "escalation",
]


class UpdateConfigRequest(BaseModel):
    """Partial update: absent fields are untouched; explicit nulls clear an
    override back to the deployment default."""

    sprintbatonOpusModel: str | None = None
    sprintbatonSonnetModel: str | None = None
    sprintbatonRouterModel: str | None = None
    sprintbatonCodingHarness: str | None = None
    sprintbatonClassificationAgent: str | None = None
    sprintbatonFinalizationAgent: str | None = None
    sprintbatonAbstractFinalizationAgent: str | None = None
    sprintbatonPassingCriteriaAgent: str | None = None
    sprintbatonSpecClassificationAgent: str | None = None
    sprintbatonRepoScopingAgent: str | None = None
    sprintbatonRevisionClassificationAgent: str | None = None
    sprintbatonPlanningAgent: str | None = None
    sprintbatonPlanClassificationAgent: str | None = None
    sprintbatonReviewAgent: str | None = None
    sprintbatonConflictResolutionAgent: str | None = None
    sprintbatonMetadataGenerationAgent: str | None = None
    sprintbatonProjectMetadataGenerationAgent: str | None = None
    sprintbatonExecutionAgent: str | None = None
    sprintbatonExecutionTierAgents: str | None = None
    sprintbatonProvenanceEnabled: bool | None = None
    pollIntervalSeconds: int | None = None
    releaseCheckIntervalSeconds: int | None = None
    conversationResumeWindowSeconds: int | None = None
    sprintbatonMetadataGenerationMaxTurns: int | None = None
    sprintbatonMetadataGenerationMaxContinuations: int | None = None
    sprintbatonMetadataValidationMaxRounds: int | None = None
    sprintbatonInitRetryMaxAttempts: int | None = None
    sprintbatonInitRetryBaseSeconds: int | None = None
    escalation: EscalationConfig | None = None


def _view(config: UserConfiguration) -> dict:
    return {field: getattr(config, field) for field in _CONFIG_FIELDS}


@router.get("")
def get_config(user: User = Depends(require_user),
               state: ApiState = Depends(api_state)) -> dict:
    config = state.users.configuration_for(user.id)
    if config is None:
        config = UserConfiguration(userId=user.id)
    return _view(config)


@router.patch("")
def update_config(body: UpdateConfigRequest, user: User = Depends(require_user),
                  state: ApiState = Depends(api_state)) -> dict:
    config = state.users.configuration_for(user.id)
    if config is None:
        config = UserConfiguration(userId=user.id, createdBy=user.id)
    # getattr from the model (not model_dump values) keeps nested pydantic
    # fields like `escalation` as model instances, not raw dicts
    for field in body.model_dump(exclude_unset=True):
        setattr(config, field, getattr(body, field))
    state.users.save_configuration(config)  # invalidates the cache (spec §5)
    return _view(config)

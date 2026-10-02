"""UserConfiguration — a per-user, live-editable snapshot of the non-secret
settings Settings reads once from .env at process start (user-multitenancy
spec §4).

Every field is Optional; None means "fall through to the deployment-wide
default from the process's own Settings" — the same override-with-fallback
shape SPRINTBATON_EXECUTION_TIER_AGENTS already uses relative to
SPRINTBATON_EXECUTION_AGENT. Anything credential-shaped is deliberately
excluded: secret tokens are Credential entities (§7, encrypted), never plain
configuration.
"""

from pydantic import Field

from sprintbaton.config.settings import EscalationConfig
from sprintbaton.entities.base import BaseEntity, new_id

# UserConfiguration field -> Settings attribute it overrides. UserService
# applies these field-by-field (spec §5); extend both sides together when a
# new non-secret Settings field should become per-user tunable.
SETTINGS_OVERRIDES: dict[str, str] = {
    "sprintbatonOpusModel": "sprintbaton_opus_model",
    "sprintbatonSonnetModel": "sprintbaton_sonnet_model",
    "sprintbatonRouterModel": "sprintbaton_router_model",
    "sprintbatonCodingHarness": "sprintbaton_coding_harness",
    # Agent bindings — all 15 (provider-setup-cli spec §6.2). These are where
    # `sprintbaton providers add --bind-roles` writes, so they have to be
    # per-user overridable for the bindings to exist at all in hosted mode;
    # before that spec only the two execution ones were mapped, and the 12
    # per-action ones were computed and never consulted.
    "sprintbatonClassificationAgent": "sprintbaton_classification_agent",
    "sprintbatonFinalizationAgent": "sprintbaton_finalization_agent",
    "sprintbatonAbstractFinalizationAgent": "sprintbaton_abstract_finalization_agent",
    "sprintbatonPassingCriteriaAgent": "sprintbaton_passing_criteria_agent",
    "sprintbatonSpecClassificationAgent": "sprintbaton_spec_classification_agent",
    "sprintbatonRepoScopingAgent": "sprintbaton_repo_scoping_agent",
    "sprintbatonRevisionClassificationAgent":
        "sprintbaton_revision_classification_agent",
    "sprintbatonPlanningAgent": "sprintbaton_planning_agent",
    "sprintbatonPlanClassificationAgent": "sprintbaton_plan_classification_agent",
    "sprintbatonReviewAgent": "sprintbaton_review_agent",
    "sprintbatonConflictResolutionAgent": "sprintbaton_conflict_resolution_agent",
    "sprintbatonMetadataGenerationAgent": "sprintbaton_metadata_generation_agent",
    "sprintbatonProjectMetadataGenerationAgent":
        "sprintbaton_project_metadata_generation_agent",
    "sprintbatonExecutionAgent": "sprintbaton_execution_agent",
    "sprintbatonExecutionTierAgents": "sprintbaton_execution_tier_agents",
    "sprintbatonProvenanceEnabled": "sprintbaton_provenance_enabled",
    "pollIntervalSeconds": "poll_interval_seconds",
    "releaseCheckIntervalSeconds": "release_check_interval_seconds",
    "conversationResumeWindowSeconds": "conversation_resume_window_seconds",
    # Project initialization tuning (project-initialization-task spec §12)
    "sprintbatonMetadataGenerationMaxTurns": "sprintbaton_metadata_generation_max_turns",
    "sprintbatonMetadataGenerationMaxContinuations":
        "sprintbaton_metadata_generation_max_continuations",
    "sprintbatonMetadataValidationMaxRounds": "sprintbaton_metadata_validation_max_rounds",
    "sprintbatonInitRetryMaxAttempts": "sprintbaton_init_retry_max_attempts",
    "sprintbatonInitRetryBaseSeconds": "sprintbaton_init_retry_base_seconds",
}

# EscalationConfig field -> the flat Settings escalation attribute behind it
ESCALATION_OVERRIDES: dict[str, str] = {
    "region_edit_limit": "escalation_region_edit_limit",
    "fix_attempts": "escalation_fix_attempts",
    "noprogress_window": "escalation_noprogress_window",
    "max_clarify_rounds": "escalation_max_clarify_rounds",
    "global_token_cap": "escalation_global_token_cap",
    "tier_token_budget": "escalation_tier_token_budget",
    "tier_wall_clock_seconds": "escalation_tier_wall_clock_seconds",
    "ai_review_max_rounds": "escalation_ai_review_rounds",
    "human_review_max_rounds": "escalation_human_review_rounds",
}


class UserConfiguration(BaseEntity):
    id: str = Field(default_factory=lambda: new_id("userconfig"))
    userId: str = ""

    sprintbatonOpusModel: str | None = None
    sprintbatonSonnetModel: str | None = None
    sprintbatonRouterModel: str | None = None
    sprintbatonCodingHarness: str | None = None
    # The 15 agent bindings (provider-setup-cli spec §6). None = unbound, which
    # makes the action fail loudly at resolution time (there is no built-in
    # default wiring) — `serve` checks for that at startup.
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
    escalation: EscalationConfig | None = None  # reuses the existing pydantic model

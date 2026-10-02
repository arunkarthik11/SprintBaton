from pydantic import Field

from sprintbaton.entities.base import LOCAL_USER_ID, BaseEntity, new_id


class AgentDefinition(BaseEntity):
    """A named, persisted agent recipe: which harness executes which model,
    optionally behind a model-specific system preamble.

    Stored in the `agent_definitions` collection. The inherited `name` is the
    unique handle a binding references, so custom agents (e.g. a GLM model on
    the claude_agent_sdk harness, a Qwen model on open_hands, or an Anthropic
    model on an in-house chained harness) can be wired to any task action
    without code changes. Bindings are written either as
    SPRINTBATON_<ACTION>_AGENT env vars or as the equivalent per-user
    UserConfiguration fields `sprintbaton providers add --bind-roles` fills in
    (provider-setup-cli spec §6).

    Every field is action-agnostic, which is what makes one definition
    reusable across several of the 13 task actions.
    """

    id: str = Field(default_factory=lambda: new_id("agentdef"))
    # Tenant owner (user-multitenancy spec §6): `name` is unique per user, not
    # globally — every lookup folds userId into the filter
    userId: str = LOCAL_USER_ID
    harnessName: str = ""        # Harness.name registered in the HarnessRegistry
    # References a Provider by `name` (provider-registration spec §10;
    # agent-fallback spec §3.1) — a registered pool, or a zero-registration
    # built-in ("anthropic"/"openai"/"google"). Validated against
    # ProviderService.known_names at `agents create` and resolution time.
    # AgentDefinitions sharing this value share a real-world quota pool.
    modelProvider: str = "anthropic"
    modelId: str = ""            # provider model id, e.g. "claude-sonnet-5", "glm-5.2"
    # prompts/templates/<name>.md, PREPENDED before the action's own template
    # (agent-system-prompt spec §4.1) — model-specific standing instructions
    # ("stricter JSON discipline", "prefer minimal diffs"), reusable across
    # every action this agent is wired to. It never replaces the action
    # template, so it cannot desynchronize from the action's OUTPUT_SCHEMA.
    # None -> the action template renders alone.
    systemPromptName: str | None = None
    description: str | None = None

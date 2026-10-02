"""Provider — a first-class, persisted, user-owned model-backend entity
(provider-registration spec §4, agent-fallback-limits spec §3.1).

Two specs converge on one entity, so it carries two orthogonal groups of
fields:

- **Registration** (provider-registration spec): how a coding-model backend is
  made usable at all — which harness(es) it backs, what external CLI binary it
  assumes, what optional pip dependency (if any) it needs, and which
  CredentialProvider a captured token is stored under. `providerType` is the
  litellm/harness routing string a harness guards on ("anthropic" / "openai" /
  "gemini" / ...), deliberately decoupled from `name` (the pool identity) so a
  team can register several rows of the same type — "anthropic-1",
  "anthropic-2" — for independent quota pools.

- **Quota-pool state** (agent-fallback-limits spec): `active`/`inactiveUntil`/
  `inactiveReason` are the runtime availability the fallback router reads, and
  `tokenLimits` are user-declared rolling-window budgets SprintBaton enforces
  from its own recorded spend. Several `AgentDefinition`s may reference one
  `Provider` (by `name`), which is exactly how sharing a quota pool is
  expressed — a limit hit on one is understood to affect them all.

`anthropic` (and `openai`/`google`) exist as zero-registration built-ins in
`providers/registry.py`; a persisted `Provider` row exists only for a provider
a user explicitly registered (or a same-type pool they named themselves).
"""

from pydantic import BaseModel, Field

from sprintbaton.entities.base import LOCAL_USER_ID, BaseEntity, new_id


class SelfImposedLimit(BaseModel):
    """A user-declared ceiling: no more than `maxTokens` consumed by the
    owning Provider within a rolling window of `windowSeconds` (agent-fallback
    spec §3.4). Multiple limits may attach to one Provider — the Provider is
    inactive whenever *any* is currently breached (the `max` of resets, never
    the `min`)."""

    maxTokens: int
    windowSeconds: int


class HarnessDependency(BaseModel):
    """The optional pip footprint of exactly one harness a provider backs
    (multi-provider-parity spec §4.3). One provider now backs harnesses with
    different (or zero) dependency footprints — codex_cli/gemini_cli need
    nothing, openai_single_shot needs the thin `openai` client, openai_agent_sdk
    the full `openai-agents` framework — so the dependency state is per-harness,
    not per-provider. An all-None dependency (or an absent map entry) means the
    harness needs nothing installed."""

    pipExtra: str | None = None       # sprintbaton[<extra>] the harness needs, or None
    importCheckModule: str | None = None  # module find_spec probes to decide "already satisfied"


class Provider(BaseEntity):
    id: str = Field(default_factory=lambda: new_id("provider"))
    # Tenant owner; `name` (inherited from BaseEntity) is unique per user and is
    # the pool identity AgentDefinition.modelProvider / SPRINTBATON_DEFAULT_PROVIDER
    # reference.
    userId: str = LOCAL_USER_ID

    # --- registration (provider-registration spec §4.1) ---
    providerType: str = ""        # litellm/harness routing string a harness guards on
    harnessNames: list[str] = Field(default_factory=list)
    credentialProvider: str | None = None  # a CredentialProvider value, or None if login-only
    credentialId: str | None = None        # optional explicit Credential id (else three-tier resolve)
    cliBinary: str | None = None           # external binary checked (never installed) via shutil.which
    # An API endpoint other than the provider type's default, e.g. an
    # Anthropic-compatible gateway (hosted-sandbox-isolation spec §9.3).
    # https:// only, validated at write time; None = the type's default.
    baseUrl: str | None = None
    # Per-harness pip footprint (multi-provider-parity spec §4.3), keyed by
    # harness name — one entry per member of harnessNames that needs a
    # dependency. A harness absent from the map needs nothing installed.
    harnessDependencies: dict[str, HarnessDependency] = Field(default_factory=dict)
    description: str | None = None

    # --- quota-pool availability (agent-fallback spec §3.3) ---
    active: bool = True
    inactiveUntil: int | None = None       # epoch millis it is expected to become usable again
    inactiveReason: str | None = None

    # --- self-imposed limits (agent-fallback spec §3.4) ---
    tokenLimits: list[SelfImposedLimit] = Field(default_factory=list)

"""Provider seeding and role binding (provider-setup-cli spec §5, §6, §7).

This module is the **seed-value source** that used to be a second resolution
path. `models/definitions.py` resolves a task action by reading its binding and
fetching the named AgentDefinition — nothing else. Everything a provider's
"built-in wiring" used to mean is expressed here instead, as data that gets
written into rows and bindings once, by `sprintbaton providers add|update` and
`sprintbaton setup`.

Two independent decisions, along the seam the code already had (§4.1) — the
four router-tier roles versus the other nine:

    --classify-harness   default | api | cli | agent_sdk
    --agentic-harness    default | cli | agent_sdk

Each maps to a concrete harness through PROVIDER_RUNTIME_HARNESS (§4.2). A
seeded row is identified by `(provider, runtime, tier)` and named
`<provider>-<runtime-slug>-<tier>` (§5.1) — the *action* is the contract, so
several rows may be legitimate candidates for one role and the collection is an
accumulating catalog rather than an exclusive configuration.

Bindings live on `UserConfiguration`, not `.env` (§6.1): already per-user,
already persisted, already consulted by `UserService.settings_for` on every
resolve, and the only store that means anything in hosted mode.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass

from sprintbaton.config.settings import Settings
from sprintbaton.entities.agent_definition import AgentDefinition
from sprintbaton.entities.enums import EscalationTier
from sprintbaton.entities.user_config import UserConfiguration
from sprintbaton.storage.base import EntityDAO

log = logging.getLogger(__name__)

# --------------------------------------------------------------- vocabulary

# The runtime each flag accepts. `default` is a valid explicit value so a script
# can re-assert mode-derived behavior without knowing the mode (§4.3).
CLASSIFY_RUNTIMES = ("default", "api", "cli", "agent_sdk")
# No `api` for the agentic flag (§4.4): SprintBaton has no agentic-API harness
# except anthropic's legacy raw_tool_loop, and designing a flag slot around a
# vestigial path is the same mistake as designing one around a missing harness.
AGENTIC_RUNTIMES = ("default", "cli", "agent_sdk")

# Row-name slug per runtime (§5.1). `exec` is a pseudo-runtime: see
# EXEC_RUNTIME below.
RUNTIME_SLUG = {"api": "api", "cli": "cli", "agent_sdk": "sdk", "exec": "exec"}

# The one deviation from §5.2's flat four-rows-per-invocation matrix, and why it
# exists. §6.3 binds the three advisory coding-tier roles AND the execution
# tiers to the same `-code` row, so a single row would decide the harness for
# both — but SPRINTBATON_CODING_HARNESS has only ever meant "the harness the
# *write-capable* roles run on", and anthropic's default (raw_tool_loop, pending
# claude_agent_sdk's live-conformance run) differs from its agentic advisory
# harness. Honoring that setting on the shared row would silently downgrade
# `review`/`finalization`/`passing_criteria` to the coding loop; ignoring it
# would silently flip execution onto a harness that has not cleared its gate.
#
# So when — and only when — the agentic runtime is `agent_sdk` and the
# provider's coding-harness setting names a different harness, three extra rows
# are seeded on that harness and the write-capable roles bind to those instead.
# Confined to `agent_sdk` because that is the choice the setting has always
# described (raw_tool_loop vs. claude_agent_sdk); with the `cli` runtime it is
# ignored, which is what keeps a CLI-only install free of metered keys
# (invariant 2). The advisory rows are never affected, so invariant 8 (the
# metadata actions resolve to a writable-paths harness) holds untouched.
EXEC_RUNTIME = "exec"

# The four seeded tiers, and which of a provider's model tiers each one takes.
TIERS = ("router", "code", "plan", "escalate")
TIER_MODEL = {
    "router": "router",
    "code": "coding",
    "plan": "planning",
    "escalate": "escalation",
}

# (provider, runtime) -> harness name (§4.2). Every cell is a registered
# harness — asserted by a test against the real HarnessRegistry.
PROVIDER_RUNTIME_HARNESS: dict[str, dict[str, str]] = {
    "anthropic": {
        "api": "single_shot",
        "cli": "claude_code_cli",
        "agent_sdk": "claude_agent_sdk",
    },
    "openai": {
        "api": "openai_single_shot",
        "cli": "codex_cli",
        "agent_sdk": "openai_agent_sdk",
    },
    "google": {
        "api": "gemini_single_shot",
        "cli": "gemini_cli",
        "agent_sdk": "gemini_agent_sdk",
    },
}

# The CLI binary each provider's `cli` runtime shells out to — the
# precondition `default_runtimes` probes for, and the same set `sprintbaton
# setup` lists on PATH (auth-eligibility-decoupling spec §4.4). One table, so
# the default and the advisory can never disagree about what "installed" means.
PROVIDER_CLI_BINARY: dict[str, str] = {
    "anthropic": "claude",
    "openai": "codex",
    "google": "gemini",
}

# Providers that fit neither flag's runtime vocabulary and are therefore never
# seeded (§14 q3): open_hands_llm backs one harness, which is a prompt-
# forwarding placeholder, and declares no default model id — seeding it would
# write a row with an empty modelId (invariant 7). `providers add
# open_hands_llm` still registers the row; wire it with `agents create`.
UNSEEDABLE_PROVIDERS = frozenset({"open_hands_llm"})


@dataclass(frozen=True)
class ProviderModelTiers:
    """Where a provider's four model tiers come from, plus the ModelSpec
    routing type its harnesses guard on. What survives of the old
    ProviderHarnessFamily once the harness half moved into
    PROVIDER_RUNTIME_HARNESS (§7.1)."""

    api_provider_type: str
    router_model_attr: str
    coding_model_attr: str
    planning_model_attr: str
    escalation_model_attr: str
    # The provider's SPRINTBATON_<P>_CODING_HARNESS Settings attr — the harness
    # the write-capable roles run on. See EXEC_RUNTIME for when it applies.
    coding_harness_attr: str


PROVIDER_MODEL_TIERS: dict[str, ProviderModelTiers] = {
    "anthropic": ProviderModelTiers(
        api_provider_type="anthropic",
        router_model_attr="sprintbaton_router_model",
        coding_model_attr="sprintbaton_sonnet_model",
        planning_model_attr="sprintbaton_opus_model",
        escalation_model_attr="sprintbaton_fable_model",
        coding_harness_attr="sprintbaton_coding_harness",
    ),
    "openai": ProviderModelTiers(
        api_provider_type="openai",
        router_model_attr="sprintbaton_openai_router_model",
        coding_model_attr="sprintbaton_openai_coding_model",
        planning_model_attr="sprintbaton_openai_planning_model",
        escalation_model_attr="sprintbaton_openai_escalation_model",
        coding_harness_attr="sprintbaton_openai_coding_harness",
    ),
    "google": ProviderModelTiers(
        api_provider_type="google",
        router_model_attr="sprintbaton_gemini_router_model",
        coding_model_attr="sprintbaton_gemini_coding_model",
        planning_model_attr="sprintbaton_gemini_planning_model",
        escalation_model_attr="sprintbaton_gemini_escalation_model",
        coding_harness_attr="sprintbaton_gemini_coding_harness",
    ),
}

# ----------------------------------------------------------- the binding map

# Which seeded tier each of the 13 actions binds to (§6.3). Reproduces the
# per-action model tiers the deleted built-in default resolved, which is why a
# test asserts this map against ACTION_MODEL_TIER below rather than restating
# the models.
ACTION_TIER: dict[str, str] = {
    # the four router-tier roles — the same set that used to be the
    # single_shot-harness set, which is the seam the two flags follow (§4.1)
    "classification": "router",
    "spec_classification": "router",
    "plan_classification": "router",
    "repo_scoping": "router",
    # the 15th binding: card edits after work was derived (task-revisions
    # spec §7.5) — classification-shaped, so on the classify runtime
    "revision_classification": "router",
    # coding-tier advisory roles
    "finalization": "code",
    "passing_criteria": "code",
    "review": "code",
    # planning-tier roles, incl. the two in-place metadata init actions, which
    # need supports_writable_paths — structurally safe because every agentic
    # runtime declares it (§6.4)
    "planning": "plan",
    "abstract_finalization": "plan",
    "metadata_generation": "plan",
    "project_metadata_generation": "plan",
    # the second write-capable role, on the E4/Fable tier (fable spec §6)
    "conflict_resolution": "escalate",
}

# Model tier per action, for the equivalence assertion in the tests (§11.4).
# `execution` is absent: it binds per escalation tier, see TIER_ROW.
ACTION_MODEL_TIER: dict[str, str] = {
    action: TIER_MODEL[tier] for action, tier in ACTION_TIER.items()
}

# Escalation tier -> seeded row tier (§6.3), the persisted form of the
# Sonnet(E0-E2)/Opus(E3)/Fable(E4) split.
TIER_ROW: dict[EscalationTier, str] = {
    EscalationTier.E0: "code",
    EscalationTier.E1: "code",
    EscalationTier.E2: "code",
    EscalationTier.E3: "plan",
    EscalationTier.E4: "escalate",
}

# Action name -> the UserConfiguration field its binding is written to. The
# camelCase twin of Settings.sprintbaton_<action>_agent (§6.2).
ACTION_CONFIG_FIELD: dict[str, str] = {
    action: "sprintbaton"
            + "".join(part.capitalize() for part in action.split("_"))
            + "Agent"
    for action in ACTION_TIER
}
EXECUTION_TIER_CONFIG_FIELD = "sprintbatonExecutionTierAgents"
EXECUTION_CONFIG_FIELD = "sprintbatonExecutionAgent"


class SeedingError(ValueError):
    """An unseedable provider or an unknown runtime — a plain message from the
    CLI, 422 at any API boundary."""


@dataclass(frozen=True)
class SeedRow:
    """One (provider, runtime, tier) candidate definition."""

    name: str
    provider: str
    runtime: str
    tier: str
    harness_name: str
    model_id: str

    @property
    def description(self) -> str:
        return (f"Seeded {self.tier}-tier agent for provider {self.provider} "
                f"on the {self.runtime} runtime")


def agent_name(provider: str, runtime: str, tier: str) -> str:
    return f"{provider}-{RUNTIME_SLUG[runtime]}-{tier}"


def default_runtimes(settings: Settings,
                     provider: str | None = None) -> tuple[str, str]:
    """(classify, agentic) runtime defaults for this install (§4.3).

    One rule: tool mode assumes you have a CLI login, hosted mode assumes you
    have an API key. A default requiring a metered key in the mode built for
    people without one is the wrong default; the latency of classifying through
    a subprocess is the price of a zero-key install that works.

    Reads `sprintbaton_mode`, not the persistence backend
    (auth-eligibility-decoupling spec §4.4). This is the one consumer where a
    mode declaration is straightforwardly correct — it is a UX default, not a
    boundary, and being wrong costs one re-run of `providers add`.

    Given a `provider`, the `cli` default additionally requires that provider's
    binary to be on PATH: a runtime whose precondition is absent is not a
    default, it is a broken install waiting for its first task. (That is
    exactly the hosted image, which carries no `claude` — `claude_agent_sdk`
    uses the copy bundled inside its wheel, which `claude_code_cli` does not
    look for.) Passing no provider keeps the pure mode answer, for callers that
    have no provider in hand.
    """
    if settings.sprintbaton_mode != "tool":
        return "api", "agent_sdk"
    binary = PROVIDER_CLI_BINARY.get(provider or "")
    if binary and shutil.which(binary) is None:
        return "api", "agent_sdk"
    return "cli", "cli"


def resolve_runtimes(settings: Settings, classify: str, agentic: str,
                     provider: str | None = None) -> tuple[str, str]:
    """Validate the two flag values and expand `default` for this install."""
    if classify not in CLASSIFY_RUNTIMES:
        raise SeedingError(
            f"unknown --classify-harness {classify!r} "
            f"(valid: {', '.join(CLASSIFY_RUNTIMES)})")
    if agentic not in AGENTIC_RUNTIMES:
        raise SeedingError(
            f"unknown --agentic-harness {agentic!r} "
            f"(valid: {', '.join(AGENTIC_RUNTIMES)})")
    default_classify, default_agentic = default_runtimes(settings, provider)
    return (default_classify if classify == "default" else classify,
            default_agentic if agentic == "default" else agentic)


def harness_for(provider: str, runtime: str) -> str:
    try:
        return PROVIDER_RUNTIME_HARNESS[provider][runtime]
    except KeyError:
        raise SeedingError(
            f"provider {provider!r} has no {runtime!r} runtime "
            f"(seedable providers: "
            f"{', '.join(sorted(PROVIDER_RUNTIME_HARNESS))})") from None


def model_for(provider: str, model_tier: str, settings: Settings) -> str:
    tiers = PROVIDER_MODEL_TIERS[provider]
    attr = {
        "router": tiers.router_model_attr,
        "coding": tiers.coding_model_attr,
        "planning": tiers.planning_model_attr,
        "escalation": tiers.escalation_model_attr,
    }[model_tier]
    return getattr(settings, attr)


def coding_harness_override(provider: str, agentic_runtime: str,
                            settings: Settings) -> str:
    """The harness the write-capable roles should run on instead of the agentic
    runtime's, or "" when there is nothing to override (see EXEC_RUNTIME)."""
    if agentic_runtime != "agent_sdk":
        return ""
    configured = getattr(
        settings, PROVIDER_MODEL_TIERS[provider].coding_harness_attr, "") or ""
    if not configured or configured == harness_for(provider, agentic_runtime):
        return ""
    return configured


def plan_rows(provider: str, settings: Settings, *,
              classify_runtime: str | None = None,
              agentic_runtime: str | None = None) -> list[SeedRow]:
    """The rows the given flag values seed (§5.2): one router row for the
    classify flag, three (code/plan/escalate) for the agentic flag.

    Identical model tiers still produce distinct rows (§5.3) — google's coding,
    planning and escalation models are all gemini-3-pro today, and deduping
    would make the row set a function of coincidental settings equality.
    """
    if provider in UNSEEDABLE_PROVIDERS:
        raise SeedingError(
            f"provider {provider!r} is not seedable: it declares no runtime in "
            f"the api/cli/agent_sdk vocabulary and no default model. Register "
            f"it and wire it by hand with `sprintbaton agents create`.")
    if provider not in PROVIDER_RUNTIME_HARNESS:
        raise SeedingError(
            f"unknown provider {provider!r} (seedable: "
            f"{', '.join(sorted(PROVIDER_RUNTIME_HARNESS))})")
    rows: list[SeedRow] = []
    wanted: list[tuple[str, str]] = []
    if classify_runtime is not None:
        wanted.append((classify_runtime, "router"))
    if agentic_runtime is not None:
        wanted.extend((agentic_runtime, tier)
                      for tier in ("code", "plan", "escalate"))
        if coding_harness_override(provider, agentic_runtime, settings):
            wanted.extend((EXEC_RUNTIME, tier)
                          for tier in ("code", "plan", "escalate"))
    for runtime, tier in wanted:
        if runtime == EXEC_RUNTIME:
            harness_name = coding_harness_override(
                provider, agentic_runtime, settings)
        else:
            harness_name = harness_for(provider, runtime)
        model_id = model_for(provider, TIER_MODEL[tier], settings)
        if not model_id:
            # Invariant 7: never write a row with an empty model id.
            attr = getattr(PROVIDER_MODEL_TIERS[provider],
                           f"{TIER_MODEL[tier]}_model_attr")
            raise SeedingError(
                f"provider {provider!r} has no model configured for the "
                f"{TIER_MODEL[tier]} tier — set {attr.upper()}")
        rows.append(SeedRow(
            name=agent_name(provider, runtime, tier), provider=provider,
            runtime=runtime, tier=tier, harness_name=harness_name,
            model_id=model_id))
    return rows


def exec_rows_present(rows: list[SeedRow]) -> bool:
    """Did `plan_rows` seed the coding-harness `-exec-*` rows? The binding map
    needs to know which set the write-capable roles should point at."""
    return any(row.runtime == EXEC_RUNTIME for row in rows)


def harnesses_for_rows(rows: list[SeedRow]) -> list[str]:
    """The distinct harnesses those rows need, in first-seen order — what
    `providers add` installs the pip extras of."""
    seen: list[str] = []
    for row in rows:
        if row.harness_name not in seen:
            seen.append(row.harness_name)
    return seen


@dataclass
class SeedOutcome:
    created: list[SeedRow]
    updated: list[SeedRow]
    unchanged: list[SeedRow]


def seed_agents(rows: list[SeedRow], definitions: EntityDAO[AgentDefinition],
                user_id: str, *, update_existing: bool = False) -> SeedOutcome:
    """Persist the planned rows.

    `update_existing=False` (`providers add`) is create-if-absent, so re-running
    the same command is a byte-identical no-op (invariant 5).
    `update_existing=True` (`providers update`) rewrites an existing row's
    harness/model from current Settings, which is the whole point of that
    command — and touches no binding (§4.6).
    """
    outcome = SeedOutcome([], [], [])
    for row in rows:
        existing = definitions.find_one({"name": row.name, "userId": user_id})
        if existing is None:
            definitions.save(AgentDefinition(
                userId=user_id, createdBy=user_id, modifiedBy=user_id,
                name=row.name, harnessName=row.harness_name,
                modelProvider=row.provider, modelId=row.model_id,
                description=row.description,
            ))
            outcome.created.append(row)
            continue
        if not update_existing:
            outcome.unchanged.append(row)
            continue
        if (existing.harnessName == row.harness_name
                and existing.modelId == row.model_id
                and existing.modelProvider == row.provider):
            outcome.unchanged.append(row)
            continue
        existing.harnessName = row.harness_name
        existing.modelId = row.model_id
        existing.modelProvider = row.provider
        existing.touch(modified_by=user_id)
        definitions.save(existing)
        outcome.updated.append(row)
    return outcome


# The roles that write to a repository, and therefore follow the coding-harness
# override rather than the agentic runtime (see EXEC_RUNTIME). `execution` binds
# through the tier map, not through ACTION_TIER.
WRITE_CAPABLE_ACTIONS = frozenset({"conflict_resolution"})


def binding_map(provider: str, *, classify_runtime: str, agentic_runtime: str,
                exec_rows: bool = False) -> dict[str, str]:
    """action -> AgentDefinition name, for all 13 actions plus the execution
    tier map (§6.3). Returned as UserConfiguration field names so the caller
    only has to persist it.

    `exec_rows=True` routes the write-capable roles to the `-exec-*` rows
    (EXEC_RUNTIME); the caller passes whatever `plan_rows` actually seeded.

    Invariant 1: every action is covered. Invariant 8 holds structurally — the
    two writable-paths actions bind to the agentic planning row, never an exec
    row, and every agentic runtime's harness declares supports_writable_paths
    (§6.4).
    """
    write_runtime = EXEC_RUNTIME if exec_rows else agentic_runtime
    bindings = {}
    for action, tier in ACTION_TIER.items():
        if tier == "router":
            runtime = classify_runtime
        elif action in WRITE_CAPABLE_ACTIONS:
            runtime = write_runtime
        else:
            runtime = agentic_runtime
        bindings[ACTION_CONFIG_FIELD[action]] = agent_name(
            provider, runtime, tier)
    bindings[EXECUTION_TIER_CONFIG_FIELD] = ";".join(
        f"{','.join(t.value for t in tiers)}:"
        f"{agent_name(provider, write_runtime, row)}"
        for row, tiers in _grouped_execution_tiers().items()
    )
    # The uniform execution binding as well as the per-tier map. The map wins
    # per tier, so this changes no tier's agent; it exists so that the plain
    # `execution` action resolves (resolve_chain reads this field, not the map)
    # and so a hand-written partial tier map still has something behind it.
    bindings[EXECUTION_CONFIG_FIELD] = agent_name(
        provider, write_runtime, "code")
    return bindings


def _grouped_execution_tiers() -> dict[str, list[EscalationTier]]:
    """TIER_ROW inverted, preserving row order, so the written value reads
    `E0,E1,E2:...;E3:...;E4:...` rather than one clause per tier."""
    grouped: dict[str, list[EscalationTier]] = {}
    for tier, row in TIER_ROW.items():
        grouped.setdefault(row, []).append(tier)
    return grouped


def bind_roles(provider: str, user_service, user_id: str, *,
               classify_runtime: str, agentic_runtime: str,
               exec_rows: bool = False) -> dict[str, str]:
    """Write the 13 action bindings + the execution tier map onto the user's
    UserConfiguration (§6.1). Returns the written map.

    Persisting through UserService (rather than the repo directly) is what makes
    a binding take effect on the next task instead of after the settings cache
    TTL — the same reason the API's PATCH /config goes through it
    (user-multitenancy spec §5).
    """
    bindings = binding_map(provider, classify_runtime=classify_runtime,
                           agentic_runtime=agentic_runtime,
                           exec_rows=exec_rows)
    config = user_service.configuration_for(user_id)
    if config is None:
        config = UserConfiguration(userId=user_id, createdBy=user_id)
    for field, value in bindings.items():
        setattr(config, field, value)
    user_service.save_configuration(config)
    log.info("bound task actions to seeded agents", extra={
        "user_id": user_id, "provider": provider,
        "classify_runtime": classify_runtime, "agentic_runtime": agentic_runtime,
    })
    return bindings


def unbound_actions(settings: Settings) -> list[str]:
    """The actions with no binding, for `serve`'s startup check (§7.3). Empty
    means every action resolves; anything else is an install that never ran
    `providers add --bind-roles`.

    `execution` counts as unbound when any escalation tier is uncovered: a
    hand-written SPRINTBATON_EXECUTION_TIER_AGENTS replaces the seeded map
    wholesale (it is one field), so a partial map with no uniform
    SPRINTBATON_EXECUTION_AGENT behind it resolves for some tiers and raises on
    the others — worth surfacing at startup rather than at the first escalation.
    """
    from sprintbaton.models.definitions import ACTION_PROMPTS, parse_tier_agent_map

    missing = [action for action in ACTION_PROMPTS
               if action != "execution"
               and not getattr(settings, f"sprintbaton_{action}_agent", "").strip()]
    try:
        tier_map = parse_tier_agent_map(settings.sprintbaton_execution_tier_agents)
    except ValueError:
        tier_map = {}  # malformed config fails loudly elsewhere; here it covers nothing
    if (not settings.sprintbaton_execution_agent.strip()
            and not set(TIER_ROW).issubset(tier_map)):
        missing.append("execution")
    return missing

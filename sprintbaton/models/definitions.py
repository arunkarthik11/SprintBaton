"""AgentDefinitionResolver — turns "which agent runs this task action?" into a
concrete (harness, model, prompt) triple.

There is exactly **one** resolution path (provider-setup-cli spec §7.1): every
action resolves through a *binding* naming one or more AgentDefinitions. A
binding is `SPRINTBATON_<ACTION>_AGENT` — set either as an env var or as the
per-user `UserConfiguration` field `sprintbaton providers add --bind-roles`
writes — and each named definition is fetched from the `agent_definitions`
collection and resolved against the HarnessRegistry, failing loudly on an
unknown definition or harness name. An action with **no** binding raises
AgentBindingError: there is no built-in default wiring any more, because a
second, parallel resolution path is exactly what drifted (ibid. §2.4). The
provider harness/model table that used to back it now lives in
`providers/seeding.py`, where it is a *seed-value source* for the rows
`providers add` writes rather than a resolution path of its own.

Execution additionally resolves per escalation tier (execution-tier-agents spec
§4): SPRINTBATON_EXECUTION_TIER_AGENTS binds tiers to named definitions,
uncovered tiers fall through to SPRINTBATON_EXECUTION_AGENT (uniform). The
seeder writes both, so the Sonnet(E0-E2)/Opus(E3)/Fable(E4) split is now
persisted rows rather than a hardcoded branch (fable-coding-tier spec §4.2).

Resolution is per call, keyed by the requesting task's own userId
(claude-code-cli harness spec §4): every lookup starts from
UserService.settings_for(user_id) — so UserConfiguration's agent-wiring
overrides (which is where bindings live) take effect — and the AgentDefinition
ownership filter is the passed-in user_id, never the fixed process identity.

Auth mode is derived per call from the harness's capabilities and the
*owner's own* credentials — never from which branch built the config, and never
from who the owner is (hosted-sandbox-isolation spec §9.2): a subscription-
capable harness runs on the owner's stored Claude subscription token when they
have one; otherwise a metered-capable one runs on their API key; otherwise, in
tool mode only, a harness may authenticate from the host's on-disk CLI login.
There is no eligibility term and no operator subscription any more — each
owner spends what they gave SprintBaton and nothing else.

Per-call resolution also covers the model-provider credential (per-user-
provider-credentials spec §4): every ModelSpec built here carries the
requesting user's own API key for the metered providers (anthropic/openai/
google), resolved through the same three-tier chain git_for/task_adapter_for
already use — explicit Provider.credentialId -> the user's default Credential
for that provider -> the deployment Settings fallback. claude_code_cli declares
supports_metered_auth=False, so its no-forwarded-key invariant means api_key is
never populated for it.
"""

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sprintbaton.config.settings import Settings
from sprintbaton.entities.agent_definition import AgentDefinition
from sprintbaton.entities.enums import CredentialProvider, EscalationTier
from sprintbaton.harness.base import (
    AuthResolutionError,
    Harness,
    HarnessCapabilityError,
    HarnessUnavailableError,
    ModelSpec,
    sandbox_mode_of,
    unsandboxable_reason,
)
from sprintbaton.harness.registry import HarnessRegistry
from sprintbaton.storage.base import EntityDAO
from sprintbaton.users.service import UserService

if TYPE_CHECKING:
    from sprintbaton.providers.availability import ProviderAvailabilityService
    from sprintbaton.providers.service import ProviderService
    from sprintbaton.users.credentials import CredentialService

log = logging.getLogger(__name__)

# action name -> (env settings attr, default prompt template)
ACTION_PROMPTS: dict[str, str] = {
    "classification": "classification",
    "finalization": "clarification",
    "abstract_finalization": "abstract_clarification",
    "passing_criteria": "passing_criteria",
    "spec_classification": "spec_classification",
    "repo_scoping": "repo_scoping",
    # Card edits after work was derived (task-revisions spec §7)
    "revision_classification": "revision_classification",
    "planning": "planning",
    "plan_classification": "plan_classification",
    "execution": "execution",
    "review": "review",
    "conflict_resolution": "conflict_resolution",
    # The metadata init pass as resolved work (project-initialization-task
    # spec §6.1): per-repo, then the combined project index.
    "metadata_generation": "metadata_generation",
    "project_metadata_generation": "project_metadata_generation",
}

# Actions whose run edits a metadata directory in place and therefore needs a
# harness honoring HarnessTaskSpec.writable_paths (project-initialization-task
# spec §6.3). An incapable harness — named directly or anywhere in a fallback
# chain — is a configuration error, never silently skipped.
_WRITABLE_PATH_ACTIONS = frozenset({"metadata_generation", "project_metadata_generation"})

_EXECUTION_TIERS = (
    EscalationTier.E0, EscalationTier.E1, EscalationTier.E2,
    EscalationTier.E3, EscalationTier.E4,
)


# The closed metered-provider table (per-user-provider-credentials spec §4.3):
# ModelSpec routing type -> (CredentialProvider, deployment-fallback Settings
# field). A routing type not listed here (e.g. "open_hands_llm", or any custom
# type) resolves no key at all — api_key stays "".
_METERED_PROVIDERS: dict[str, tuple[CredentialProvider, str]] = {
    "anthropic": (CredentialProvider.ANTHROPIC, "anthropic_api_key"),
    "openai": (CredentialProvider.OPENAI, "openai_api_key"),
    "google": (CredentialProvider.GEMINI, "gemini_api_key"),
}


class AgentBindingError(AuthResolutionError):
    """No AgentDefinition is bound to a task action (provider-setup-cli spec
    §7.3). A configuration error, not a task failure — and deliberately a
    subclass of AuthResolutionError so the orchestrator's existing
    park-to-Blocked arm covers it: a misconfiguration the polling job would
    otherwise re-enqueue every cycle, forever.

    `sprintbaton serve` also checks for this at startup, so the normal way to
    meet it is one line on the console with the remedy — not a task dying at
    first dispatch.
    """


BINDING_REMEDY = ("run `sprintbaton setup`, or `sprintbaton providers add "
                  "<provider> --bind-roles`")


def parse_tier_agent_map(raw: str) -> dict[EscalationTier, str]:
    """Parse SPRINTBATON_EXECUTION_TIER_AGENTS: semicolon-separated groups of
    "<tier>[,<tier>...]:<agent-name>". Raises ValueError on any malformed or
    ambiguous group — never silently drops or overwrites a tier."""
    result: dict[EscalationTier, str] = {}
    for group in filter(None, (g.strip() for g in raw.split(";"))):
        tiers_part, sep, agent_name = group.partition(":")
        agent_name = agent_name.strip()
        if not sep or not agent_name:
            raise ValueError(
                f"SPRINTBATON_EXECUTION_TIER_AGENTS group {group!r} is missing "
                f"an ':<agent-name>' suffix"
            )
        for tier_token in filter(None, (t.strip() for t in tiers_part.split(","))):
            try:
                tier = EscalationTier(tier_token)
            except ValueError:
                raise ValueError(
                    f"SPRINTBATON_EXECUTION_TIER_AGENTS references unknown tier "
                    f"{tier_token!r} (valid: E0, E1, E2, E3, E4)"
                ) from None
            if tier == EscalationTier.EH:
                raise ValueError(
                    "SPRINTBATON_EXECUTION_TIER_AGENTS cannot target EH — "
                    "execution never runs at EH (human handoff)"
                )
            if tier in result:
                raise ValueError(
                    f"tier {tier.value} is assigned twice in SPRINTBATON_EXECUTION_TIER_AGENTS"
                )
            result[tier] = agent_name
    return result


def _split_chain(raw: str, sep: str = ",") -> list[str]:
    """An ordered fallback chain of AgentDefinition names from an env-var value
    (agent-fallback spec §3.2): a single name (today's only form) yields a
    one-element list — the backward-compatibility floor the whole feature sits
    on."""
    return [n for n in (part.strip() for part in raw.split(sep)) if n]


@dataclass(frozen=True)
class ResolvedAgentConfig:
    harness: Harness
    model: ModelSpec
    # The action's OWN template — always (agent-system-prompt spec §4.2). An
    # AgentDefinition can no longer replace it; it can only prepend a preamble.
    prompt_name: str
    # The agent's system preamble (AgentDefinition.systemPromptName), rendered
    # between metadata.md and the action template. None -> no preamble.
    system_prompt_name: str | None = None
    definition_id: str | None = None
    # The binding name this entry was resolved from, kept so a chain that
    # skipped an entry can still report the right name per surviving entry
    # (cli-subscription-auth-parity spec §4.4) — index alignment with the
    # binding string no longer holds.
    definition_name: str = ""
    # The quota-pool identity this config draws from (agent-fallback spec §3.1)
    # — the Provider name the fallback router checks availability against.
    provider_name: str = "anthropic"
    # Auth mode (hosted-sandbox-isolation spec §9.2): True = this call runs on
    # the owner's Claude subscription — `model.auth_token`, or in tool mode an
    # ambient on-disk login — instead of a metered key, so `model.api_key` is
    # always empty. Derived per call from harness capability + the owner's
    # own credentials.
    subscription_auth: bool = False


class AgentDefinitionResolver:
    def __init__(self, definitions: EntityDAO[AgentDefinition],
                 harnesses: HarnessRegistry, settings: Settings,
                 user_service: UserService,
                 providers: "ProviderService | None" = None,
                 availability: "ProviderAvailabilityService | None" = None,
                 credentials: "CredentialService | None" = None,
                 isolated: bool = False):
        self._definitions = definitions
        self._harnesses = harnesses
        self._settings = settings
        self._user_service = user_service  # per-user Settings
        # Optional (None in the many unit tests that build the resolver
        # directly): when present, modelProvider is validated against the
        # registered-provider set and ModelSpec.provider resolves to the
        # provider's routing type; availability filters the fallback chain;
        # credentials resolves the per-user API key onto ModelSpec.api_key
        # (per-user-provider-credentials spec §4.4 — without it, only the
        # deployment Settings fallback is ever used).
        self._providers = providers
        self._availability = availability
        self._credentials = credentials
        # Whether this process's runs are isolated (a remote sandbox — hosted
        # mode). A harness that can only run tools on the host is then refused
        # at resolution (hosted-sandbox-isolation spec §7). The container
        # passes SandboxRuntime.isolated; False is the tool-mode passthrough.
        self._isolated = isolated

    def resolve(self, action: str, user_id: str) -> ResolvedAgentConfig:
        """The first agent in the action's chain whose provider is currently
        available (agent-fallback spec §4). A one-element chain with an
        available provider is identical to today's single-config resolution."""
        chain = self.resolve_chain(action, user_id)
        config = self._first_available(chain, user_id)
        log.info("agent assigned", extra={
            "event": "agent_assigned", "action": action,
            "harness": config.harness.name, "model": config.model.model_id,
            "provider": config.provider_name,
            "definition": config.definition_id,
        })
        return config

    def resolve_chain(self, action: str,
                      user_id: str) -> list[ResolvedAgentConfig]:
        if action not in ACTION_PROMPTS:
            raise KeyError(
                f"unknown task action {action!r} (known: {sorted(ACTION_PROMPTS)})"
            )
        s = self._user_service.settings_for(user_id)
        names = _split_chain(getattr(s, f"sprintbaton_{action}_agent"))
        if not names:
            raise AgentBindingError(
                f"no agent is bound to task action {action!r} for user "
                f"{user_id!r} (SPRINTBATON_{action.upper()}_AGENT is empty) — "
                f"{BINDING_REMEDY}")
        chain = self._resolve_entries(action, names, user_id, s)
        for config in chain:
            self._check_capabilities(action, config, config.definition_name)
        return chain

    def _resolve_entries(self, action: str, names: list[str], user_id: str,
                         s: Settings) -> list[ResolvedAgentConfig]:
        """Resolve an ordered chain, skipping entries whose auth mode cannot be
        satisfied here (cli-subscription-auth-parity spec §4.4).

        Chains are the whole recovery story for a login-only harness — a
        `codex-cli-code,openai-sdk-code` binding is meant to mean "the CLI
        login where it exists, the metered SDK otherwise". Building every entry
        eagerly made that inert: the first entry raised and the second was
        never reached. So an AuthResolutionError on one entry is *expected*
        configuration, not a bug, and is recorded and stepped over.

        HarnessCapabilityError deliberately still propagates from
        _check_capabilities: a writable_paths mismatch is a wiring bug in every
        deployment, while an auth mismatch is a per-deployment,
        per-owner condition that the same chain is written to absorb.

        Only when no entry survives do we raise, with every reason, so the
        parked task's message names the whole chain rather than its head.
        """
        chain: list[ResolvedAgentConfig] = []
        failures: list[str] = []
        for name in names:
            try:
                config = self._resolve_definition(action, name, user_id, s)
            except AuthResolutionError as exc:
                failures.append(f"  - {name}: {exc}")
                continue
            chain.append(config)
        if not chain:
            raise AuthResolutionError(
                f"no agent bound to task action {action!r} for user "
                f"{user_id!r} could be authenticated; every entry in the chain "
                f"failed:\n" + "\n".join(failures))
        return chain

    @staticmethod
    def _check_capabilities(action: str, config: ResolvedAgentConfig,
                            definition_name: str) -> None:
        """Fail loudly when an action that edits in place resolved to a harness
        that cannot honor writable_paths (project-initialization-task spec
        §6.3) — checked for every chain entry, so a misconfigured fallback is
        caught before it is ever walked to."""
        if action not in _WRITABLE_PATH_ACTIONS:
            return
        if getattr(config.harness, "supports_writable_paths", False):
            return
        source = (f"AgentDefinition {definition_name!r} "
                  f"(SPRINTBATON_{action.upper()}_AGENT)")
        raise HarnessCapabilityError(
            f"task action {action!r} resolved to harness {config.harness.name!r} "
            f"via {source}, but that harness does not support writable_paths — "
            f"this action edits its metadata directory in place. Wire it to "
            f"claude_agent_sdk, claude_code_cli, openai_agent_sdk, or "
            f"gemini_agent_sdk.")

    def resolve_execution_tiers(self, user_id: str) -> dict[EscalationTier, ResolvedAgentConfig]:
        """First-available agent per execution tier (back-compat single-config
        shape). See resolve_execution_tier_chain for the full ordered chain."""
        return {tier: self.resolve_execution_tier(tier, user_id)
                for tier in _EXECUTION_TIERS}

    def resolve_execution_tier(self, tier: EscalationTier,
                               user_id: str) -> ResolvedAgentConfig:
        config = self._first_available(
            self.resolve_execution_tier_chain(tier, user_id), user_id)
        log.info("agent assigned", extra={
            "event": "agent_assigned", "action": "execution",
            "harness": config.harness.name, "model": config.model.model_id,
            "tier": tier.value, "provider": config.provider_name,
            "definition": config.definition_id,
        })
        return config

    def resolve_execution_tier_chain(self, tier: EscalationTier,
                                     user_id: str) -> list[ResolvedAgentConfig]:
        """Per-tier agent chain for execution. Precedence per tier:
        SPRINTBATON_EXECUTION_TIER_AGENTS[tier] (a `+`-separated chain) ->
        SPRINTBATON_EXECUTION_AGENT (uniform, `,`-separated chain, a single
        name still works). Unbound raises — the Sonnet/Opus/Fable split is
        seeded rows now, not a fallback branch (provider-setup-cli spec §7.1)."""
        s = self._user_service.settings_for(user_id)
        tier_map = parse_tier_agent_map(s.sprintbaton_execution_tier_agents)
        if tier in tier_map:
            names = _split_chain(tier_map[tier], sep="+")
        else:
            names = _split_chain(s.sprintbaton_execution_agent)
        if not names:
            raise AgentBindingError(
                f"no agent is bound to execution tier {tier.value} for user "
                f"{user_id!r} (neither SPRINTBATON_EXECUTION_TIER_AGENTS nor "
                f"SPRINTBATON_EXECUTION_AGENT covers it) — {BINDING_REMEDY}")
        # Same skip-and-record contract as resolve_chain (§4.4) — an execution
        # tier bound to `codex-cli-code+openai-sdk-code` must walk past the
        # entry this deployment cannot authenticate.
        return self._resolve_entries("execution", names, user_id, s)

    def _first_available(self, chain: list[ResolvedAgentConfig],
                         user_id: str) -> ResolvedAgentConfig:
        """The first chain entry whose provider is active, else the head — so a
        call is always attempted; a reactive-only or fully-exhausted chain then
        surfaces its signal through the normal pause path (agent-fallback §4)."""
        if self._availability is None or len(chain) == 1:
            return chain[0]
        return self._availability.first_available(chain, user_id) or chain[0]

    # ------------------------------------------------------------------ auth
    # Auth mode is DERIVED, once, from the harness's capabilities and the
    # owner's own credentials — never from which resolver branch built the
    # config (auth-mode-resolution spec §2), and never from who the owner is
    # (hosted-sandbox-isolation spec §9.2). Every ResolvedAgentConfig producer
    # routes through _resolve_auth.

    def _resolve_auth(self, action: str, harness: Harness, provider_type: str,
                      user_id: str, s: Settings,
                      provider_name: str | None = None) -> tuple[bool, str, str]:
        """(subscription_auth, api_key, auth_token) for one resolved config.

        The resolution table of hosted-sandbox-isolation spec §9.2, in order:

        1. a subscription-capable harness whose CLI reads a token from its
           environment, and an owner who resolves an ANTHROPIC_SUBSCRIPTION
           credential -> subscription, with that token;
        2. a metered-capable harness and an owner who resolves an API key
           (three-tier chain) -> metered, with that key;
        3. tool mode (`local_login_sessions`) and a subscription-capable
           harness -> subscription from the host's ambient on-disk login;
        4. otherwise AuthResolutionError — which a fallback chain steps past
           (cli-subscription-auth-parity §4.4).

        Preference needs no setting: a harness that can use a subscription
        uses one when the owner has one. An owner who wants metered spend on
        such a harness stores no subscription credential, or binds a
        metered-only harness.

        Validated at *resolution* time (auth-mode-resolution §2.1) so a
        misconfiguration surfaces before a workspace is prepared or a repo is
        cloned, naming the actual cause.
        """
        del s  # no term reads per-user Settings
        name = harness.name
        if self._isolated and sandbox_mode_of(harness) == "unsupported":
            raise HarnessUnavailableError(
                f"task action {action!r} resolved to harness {name!r}: "
                + unsandboxable_reason(harness))
        subscription_capable = getattr(harness, "supports_subscription_auth", False)
        token_var = getattr(harness, "subscription_token_var", "")
        if subscription_capable and token_var and provider_type == "anthropic":
            token = self._resolve_subscription_token(user_id, provider_name)
            if token:
                return True, "", token
        metered_capable = getattr(harness, "supports_metered_auth", True)
        key = ""
        if metered_capable:
            key = self._resolve_api_key(
                provider_type, user_id, name, provider_name=provider_name)
            if (key or not getattr(harness, "requires_explicit_key_when_metered", True)
                    or provider_type not in _METERED_PROVIDERS):
                return False, key, ""
        if subscription_capable and self._settings.local_login_sessions:
            return True, "", ""
        raise AuthResolutionError(self._no_credential_message(
            action, harness, provider_type, user_id, metered_capable,
            subscription_capable, token_var))

    def _no_credential_message(self, action: str, harness: Harness,
                               provider_type: str, user_id: str,
                               metered_capable: bool, subscription_capable: bool,
                               token_var: str) -> str:
        name = harness.name
        if not metered_capable and not token_var:
            # On-disk-only login (codex_cli/gemini_cli): nothing an owner can
            # store makes this work off a tool-mode install.
            return (f"task action {action!r} resolved to harness {name!r}, which "
                    f"authenticates only from a local CLI login session — that "
                    f"exists only on a tool-mode install (cli-subscription-auth-"
                    f"parity spec §4.2). Wire this action to a metered-capable "
                    f"harness — e.g. the same provider's agent-SDK or single-shot "
                    f"row — or add one as a fallback: SPRINTBATON_"
                    f"{action.upper()}_AGENT=<cli-row>,<sdk-row>.")
        wants = []
        if subscription_capable and token_var:
            wants.append("a Claude subscription credential (`sprintbaton "
                         "credentials create --provider anthropic_subscription`)")
        if metered_capable and provider_type in _METERED_PROVIDERS:
            credential_provider, settings_field = _METERED_PROVIDERS[provider_type]
            wants.append(f"a {provider_type} API key (`sprintbaton credentials "
                         f"create --provider {credential_provider}`, or the "
                         f"deployment fallback Settings.{settings_field})")
        return (f"task action {action!r} resolved to harness {name!r}, but user "
                f"{user_id!r} has no credential it can use: store "
                + " or ".join(wants or ["a credential for this provider"]) + ".")

    def _provider_credential(self, user_id: str, provider_name: str | None):
        """The Credential a persisted Provider row pins (tier 1), or None.
        A dangling or foreign id fails loudly — a config bug, not a fallback."""
        if not provider_name or self._providers is None or self._credentials is None:
            return None
        row = self._providers.get(user_id, provider_name)
        if row is None or not row.credentialId:
            return None
        credential = self._credentials.owned(user_id, row.credentialId)
        if credential is None:
            raise ValueError(f"no such credential: {row.credentialId}")
        return credential

    def _resolve_subscription_token(self, user_id: str,
                                    provider_name: str | None) -> str:
        """The owner's Claude subscription token (spec §9.1), three-tier like
        every other credential: a subscription Credential the Provider row
        pins -> the owner's default ANTHROPIC_SUBSCRIPTION credential -> the
        deployment fallback, which exists in tool mode only
        (Settings.subscription_token_fallback — hosted mode never lends the
        operator's subscription to a tenant). A Provider pinning a *metered*
        credential pins metered auth: no subscription is looked up."""
        fallback = self._settings.subscription_token_fallback
        pinned = self._provider_credential(user_id, provider_name)
        if pinned is not None:
            if pinned.provider == CredentialProvider.ANTHROPIC_SUBSCRIPTION:
                return self._credentials.decrypt(pinned)
            return ""
        if self._credentials is None:
            return fallback
        return self._credentials.resolve(
            user_id, CredentialProvider.ANTHROPIC_SUBSCRIPTION, None,
            fallback=fallback)

    def _resolve_api_key(self, provider_type: str, user_id: str,
                         harness_name: str,
                         provider_name: str | None = None,
                         subscription_auth: bool = False) -> str:
        """The per-user model-provider key for a ModelSpec (per-user-provider-
        credentials spec §4.4). Three-tier, mirroring git_for/task_adapter_for:
        Provider.credentialId (when a persisted row names one) -> the user's
        default Credential for the provider -> the Settings fallback. Returns
        "" (the harness falls back to its own ambient/login auth) for any
        routing type outside the metered table. `provider_name=None` skips the
        Provider-row lookup — kept for direct callers; the one real caller
        always passes the definition's own modelProvider.

        A subscription-authed call short-circuits: there is no metered key to
        resolve, and skipping the lookup also avoids decrypting credential
        material the run will never use.

        The former `harness_name == claude_code_cli` special case is gone
        (auth-mode-resolution spec §4.3) — that harness now declares
        `supports_metered_auth = False`, so `_resolve_auth` rejects the
        combination before ever reaching this method. `harness_name` is kept
        for callers and logging."""
        del harness_name  # no longer an input; capability flags decide (§4.3)
        if subscription_auth:
            return ""
        entry = _METERED_PROVIDERS.get(provider_type)
        if entry is None:
            return ""
        credential_provider, settings_field = entry
        # Deployment-wide fallback comes from the process Settings — tier 3
        # is deliberately not per-user-overridable (secrets never live on
        # UserConfiguration).
        fallback = getattr(self._settings, settings_field, "") or ""
        if self._credentials is None:
            return fallback
        pinned = self._provider_credential(user_id, provider_name)
        if pinned is not None and pinned.provider == credential_provider:
            return self._credentials.decrypt(pinned)
        # A pinned credential of another kind (a subscription token) is never
        # used as an API key; the owner's default key still applies.
        return self._credentials.resolve(
            user_id, credential_provider, None, fallback=fallback)

    def _resolve_definition(self, action: str, name: str, user_id: str,
                            s: Settings | None = None) -> ResolvedAgentConfig:
        # Owned by the requesting task's user, never the process identity —
        # a hosted user's own POST /agents definition must be findable from
        # the task-dispatch path (claude-code-cli harness spec §4.2).
        definition = self._definitions.find_one({"name": name, "userId": user_id})
        if definition is None:
            raise KeyError(
                f"SPRINTBATON_{action.upper()}_AGENT names AgentDefinition "
                f"{name!r}, but no such definition exists for user {user_id!r}"
            )
        harness = self._harnesses.get(definition.harnessName)  # raises on unknown name
        provider_name = definition.modelProvider
        model_provider = provider_name
        if self._providers is not None:
            # Validate at resolution time, not just creation time (provider-
            # registration spec §10): a provider deleted after the definition
            # was created fails loudly here, exactly as an unknown harnessName
            # already does.
            if provider_name not in self._providers.known_names(user_id):
                raise KeyError(
                    f"AgentDefinition {name!r} references provider "
                    f"{provider_name!r}, which is not registered for user "
                    f"{user_id!r}")
            model_provider = self._providers.provider_type(provider_name, user_id)
        # Auth mode is derived from (owner, harness), never from the fact that
        # an agent was named (auth-mode-resolution spec §4.2). `s` comes from
        # the caller, which already resolved it — the lookup is only repeated
        # for direct callers (tests).
        if s is None:
            s = self._user_service.settings_for(user_id)
        subscription_auth, api_key, auth_token = self._resolve_auth(
            action, harness, model_provider, user_id, s,
            provider_name=provider_name)
        base_url = (self._providers.base_url(provider_name, user_id)
                    if self._providers is not None else "")
        return ResolvedAgentConfig(
            harness=harness,
            model=ModelSpec(provider=model_provider,
                            model_id=definition.modelId,
                            api_key=api_key, auth_token=auth_token,
                            base_url=base_url),
            # The action's template always renders; the definition only ever
            # contributes a preamble in front of it (agent-system-prompt §4.2).
            prompt_name=ACTION_PROMPTS[action],
            system_prompt_name=definition.systemPromptName,
            definition_id=definition.id,
            definition_name=name,
            provider_name=provider_name,
            subscription_auth=subscription_auth,
        )

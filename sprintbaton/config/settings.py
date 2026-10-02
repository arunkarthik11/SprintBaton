from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Mode-derived per-concern backend defaults (zero-infra-storage spec §6): a
# per-concern SPRINTBATON_<CONCERN>_BACKEND var, when set, always wins; mode
# is consulted only where one is absent.
_MODE_DEFAULTS: dict[str, dict[str, str]] = {
    "tool": {"persistence": "sqlite", "blob": "filesystem",
             "queue": "in_process", "lock": "file", "sandbox": "none"},
    "hosted": {"persistence": "mongo", "blob": "s3",
               "queue": "redis", "lock": "redis", "sandbox": "remote"},
}

# Every accepted backend name per concern (pluggable-hosted-backends spec
# §4.6); backend_for fails loudly on anything else.
BACKENDS: dict[str, tuple[str, ...]] = {
    "persistence": ("sqlite", "mongo", "postgres"),
    "blob": ("filesystem", "s3", "gcs", "azure"),
    "queue": ("in_process", "redis"),
    "lock": ("file", "redis"),
    "sandbox": ("none", "remote"),
}


class EscalationConfig(BaseModel):
    """Tunable defaults from the escalation spec (§11). Calibrate from telemetry."""

    region_edit_limit: int = 3            # N — thrashing trigger
    fix_attempts: int = 2                 # M — correctness-stall trigger per tier
    noprogress_window: int = 4            # K — no-progress step window
    max_clarify_rounds: int = 3           # E1/EH loop cap
    global_token_cap: int = 2_000_000     # circuit breaker across E0-E3
    tier_token_budget: int = 400_000      # per-tier token budget
    tier_wall_clock_seconds: int = 1800   # per-tier wall-clock budget
    ai_review_max_rounds: int = 2         # coding<->review-agent bounces per tier before escalating
    human_review_max_rounds: int = 3      # human PR-comment bounces per tier before escalating —
                                           # PR review is inherently iterative, so a bounce alone
                                           # isn't evidence of a stall (escalation spec §5.6)
    conflict_resolution_max_rounds: int = 2  # PR-open-time conflict-resolution bounces before parking


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_ignore_empty=True,
        extra="ignore",
    )

    # Anthropic. Since the per-user-provider-credentials spec this is the
    # deployment-wide *fallback* (tier 3 of the credential resolve chain) —
    # genuinely optional when every active user has stored their own
    # Anthropic credential (`sprintbaton credentials create --provider
    # anthropic`), and how CLI-mode .env keys keep working unchanged.
    anthropic_api_key: str = ""
    # Claude subscription (OAuth) token, minted by `claude setup-token`.
    # TOOL MODE ONLY since hosted-sandbox-isolation spec §12.1: it is the
    # third tier of the subscription-token chain (`subscription_token_fallback`)
    # for a local install. In hosted mode it is ignored — each owner stores
    # their own subscription as a Credential
    # (CredentialProvider.ANTHROPIC_SUBSCRIPTION, spec §9.1).
    claude_code_oauth_token: str = ""
    # May this process authenticate harnesses whose login session lives only on
    # its own filesystem (`codex login`, `gemini auth login`, `claude login`)?
    # None derives from the mode: true in tool mode. Forced false in hosted
    # mode (hosted-sandbox-isolation spec §12.1) — see `local_login_sessions`.
    sprintbaton_local_login_sessions: bool | None = None
    sprintbaton_opus_model: str = "claude-opus-4-8"
    sprintbaton_sonnet_model: str = "claude-sonnet-5"
    sprintbaton_router_model: str = "claude-haiku-4-5"
    # The E4 escalation tier's model (fable-coding-tier spec §4.2) — reached
    # only by escalation from E3, and the Conflict Resolution Agent's default.
    sprintbaton_fable_model: str = "claude-fable-5"

    # Default model provider. Since provider-setup-cli spec §7.1 this is NOT a
    # resolution input: every action resolves through an explicit binding to a
    # persisted AgentDefinition, and nothing consults this field at task time.
    # What it records is which provider `sprintbaton setup` offers first and
    # which one a bare `providers update` targets — written by `providers add
    # <name> --bind-roles`. `anthropic` is the base install's only usable
    # provider until `sprintbaton providers add openai`/`add google` registers
    # another.
    sprintbaton_default_provider: str = "anthropic"

    # Per-provider tiered models (multi-provider-parity spec §4.5) — the
    # provider-neutral analogues of the four Anthropic tier settings above,
    # additively named so the Anthropic ones keep their exact meaning. These are
    # read at SEED time, by `sprintbaton providers add|update` (provider-setup-
    # cli spec §9.1): a model id that moves here reaches tasks on the next
    # `providers update <name>`, not on the next task. Named _GEMINI_ (not
    # _GOOGLE_) matching the GEMINI_API_KEY precedent. All placeholders — verify
    # each provider's current catalog before pinning.
    sprintbaton_openai_router_model: str = "gpt-5-mini"
    sprintbaton_openai_coding_model: str = "gpt-5-codex"
    sprintbaton_openai_planning_model: str = "gpt-5.1"
    sprintbaton_openai_escalation_model: str = "gpt-5.1-pro"
    sprintbaton_gemini_router_model: str = "gemini-3-flash"
    sprintbaton_gemini_coding_model: str = "gemini-3-pro"
    sprintbaton_gemini_planning_model: str = "gemini-3-pro"
    sprintbaton_gemini_escalation_model: str = "gemini-3-pro"

    # Coding Model execution harness: "raw_tool_loop" | "claude_agent_sdk"
    # (docs/agent-sdk-migration-spec.md §5). Read at seed time: when it names a
    # harness other than the chosen agentic runtime's, `providers add` seeds the
    # extra `<provider>-exec-*` rows and binds the write-capable roles
    # (execution, conflict_resolution) to those — leaving the read-only advisory
    # roles on the agentic runtime (provider-setup-cli spec, seeding.EXEC_RUNTIME).
    # Ignored for the `cli` runtime, which must stay free of metered keys.
    # Anthropic-only meaning, unchanged.
    sprintbaton_coding_harness: str = "raw_tool_loop"
    # Per-provider equivalents (multi-provider-parity spec §4.6): "" -> the
    # agentic runtime's own harness (openai_agent_sdk / gemini_agent_sdk), i.e.
    # no extra rows. Kept separate from sprintbaton_coding_harness so switching
    # provider never loses either choice.
    sprintbaton_openai_coding_harness: str = ""
    sprintbaton_gemini_coding_harness: str = ""

    # Agent bindings: each var names the AgentDefinition (entity `name` in the
    # agent_definitions collection) that runs that task action — or an ordered
    # fallback chain of them (agent-fallback spec §3.2). There is no built-in
    # wiring behind these since provider-setup-cli spec §7.1: empty means the
    # action cannot resolve, and `sprintbaton serve` says so at startup. The
    # normal way to fill them in is `sprintbaton providers add <provider>
    # --bind-roles` (or `sprintbaton setup`), which writes the per-user
    # UserConfiguration twins of these fields; an env var set here overrides
    # that, per action.
    sprintbaton_classification_agent: str = ""
    sprintbaton_finalization_agent: str = ""
    sprintbaton_abstract_finalization_agent: str = ""
    sprintbaton_passing_criteria_agent: str = ""
    sprintbaton_spec_classification_agent: str = ""
    # repo_scoping is a registered action (models/definitions.ACTION_PROMPTS)
    # and resolve_chain does an unguarded getattr for every action, so this
    # field is required for multi-repo projects to resolve at all
    # (auth-mode-resolution spec §4.4).
    sprintbaton_repo_scoping_agent: str = ""
    # The Revision Classification role (task-revisions spec §7.5): names the
    # rewind point when a card's content changes after work was derived.
    sprintbaton_revision_classification_agent: str = ""
    sprintbaton_planning_agent: str = ""
    sprintbaton_plan_classification_agent: str = ""
    sprintbaton_execution_agent: str = ""
    sprintbaton_review_agent: str = ""
    sprintbaton_conflict_resolution_agent: str = ""
    # The two init-pass actions (project-initialization-task spec §6.1) —
    # required fields, since resolve_chain does an unguarded getattr per action.
    sprintbaton_metadata_generation_agent: str = ""
    sprintbaton_project_metadata_generation_agent: str = ""

    # Project initialization run tuning (project-initialization-task spec §12).
    # Turn cap per harness run (a runaway/cost guard — placeholder value).
    sprintbaton_metadata_generation_max_turns: int = 150
    # Continuation rounds per pass after a turn/time cut-off (§8.6).
    sprintbaton_metadata_generation_max_continuations: int = 3
    # Contract-violation bounces per pass (§8.5).
    sprintbaton_metadata_validation_max_rounds: int = 2
    # Consecutive transient failures before an init run fails (§5.6), and the
    # exponential backoff between them (base doubles per attempt, capped).
    sprintbaton_init_retry_max_attempts: int = 6
    sprintbaton_init_retry_base_seconds: int = 60
    sprintbaton_init_retry_max_backoff_seconds: int = 1800

    # Per-escalation-tier execution agent overrides (execution-tier-agents
    # spec §4). Format: semicolon-separated groups of
    # "<tier>[,<tier>...]:<AgentDefinition name>", e.g.
    # "E0,E1,E2:sonnet-coder;E3:opus-coder". Tiers not covered here fall
    # through to sprintbaton_execution_agent (uniform), then the built-in
    # default (Sonnet at E0-E2, Opus at E3).
    sprintbaton_execution_tier_agents: str = ""

    # OpenAI / Google deployment-wide fallback keys (per-user-provider-
    # credentials spec §4.3) — tier 3 of the same three-tier resolve chain
    # anthropic_api_key sits at the bottom of, consumed by the codex_cli /
    # gemini_cli subprocess harnesses via ModelSpec.api_key. The field names
    # deliberately map to the bare OPENAI_API_KEY / GEMINI_API_KEY env vars
    # (mirroring anthropic_api_key -> ANTHROPIC_API_KEY), the same vars those
    # CLIs already read ambiently — one name, either consumption path.
    openai_api_key: str = ""
    gemini_api_key: str = ""

    # OpenHands harness: LLM credentials forwarded to the OpenHands runtime
    # (litellm-style; the model comes from the AgentDefinition)
    open_hands_llm_api_key: str = ""
    open_hands_llm_base_url: str = ""

    # Clarification conversations (conversation-lifecycle spec §8): how long a
    # paused role's harness session stays resumable after its last turn; a
    # later human reply takes the restart-with-prior-context branch instead.
    # One shared knob across every role (per-role windows are a §13 open
    # question pending resume-rate telemetry).
    conversation_resume_window_seconds: int = 300

    # Todolist provider credentials. The provider itself is per-repository
    # (Repository.todolistProvider, repository-onboarding spec §3.2); this
    # token is the deployment-wide fallback when neither a repo-level nor a
    # user-default Credential exists (spec §4.2).
    todolist_api_token: str = ""
    sprintbaton_agent_user_id: str = "sprintbaton-agent"

    # GitHub
    github_token: str = ""

    # Polling
    poll_interval_seconds: int = 60

    # Release windows: how often the ReleaseWindowJob checks whether a
    # repository's dev -> staging cutover is due (the cadence itself is
    # per-repository: Repository.releaseCadenceDays)
    release_check_interval_seconds: int = 3600

    # Usage-limit-aware execution (usage-limit-aware execution spec §7).
    # Default ON: a harness that reports a usage-limit signal pauses the task
    # until the recorded reset time rather than failing the turn outright.
    sprintbaton_usage_limit_aware: bool = True
    # A Task.labels value that opts a specific task out of the pause — the
    # harness call proceeds (or fails) on its own; this never grants extra
    # quota (spec §7.1).
    sprintbaton_usage_limit_urgent_label: str = "urgent"
    # Used only when a signal's resets_at is unknown — a conservative
    # fallback retry backoff.
    sprintbaton_usage_limit_default_backoff_seconds: int = 1800
    # UsageLimitWakeJob's poll cadence (spec §6) — independent of
    # release_check_interval_seconds: resumes are checked on the order of
    # minutes, release cutovers on the order of days.
    sprintbaton_usage_limit_wake_check_interval_seconds: int = 300

    # Token usage reporting (token-usage-reporting spec §10): the "last one
    # week" default for `sprintbaton reports` and every /reports/usage/* route.
    sprintbaton_report_default_days: int = 7

    # Classification provenance / context snapshots (docs/classification-
    # provenance-spec.md). The kill-switch: False disables all snapshotting and
    # ClassificationRecord writes. The exclude flag governs whether .sprintbaton/
    # is kept out of SprintBaton's commits to the user's own repo (Tree 1) —
    # default preserves today's behaviour; Tree 2 (the provenance store) captures
    # context regardless, so reproducibility never depends on this choice.
    sprintbaton_provenance_enabled: bool = True
    sprintbaton_provenance_exclude_from_user_repo: bool = True

    # Workspace root for cloned repositories. None => {local_storage_root}/
    # workspaces (workspace-mirrors-and-cleanup spec §3.2), filled in at
    # construction so every reader sees a str: the old cwd-relative
    # "workspaces" moved with whatever directory `serve` started from, and
    # crash recovery / pause-resume then found nothing. The hosted chart sets
    # WORKSPACE_ROOT explicitly (/app/workspaces).
    workspace_root: str | None = None
    # Where the per-(user, project) bare repo mirrors live (ibid. §4.2).
    # None => {workspace_root}/.mirrors — on the workspace's own filesystem and
    # mount on purpose, so clones seeded from a mirror can hardlink its objects.
    sprintbaton_mirror_root: str | None = None
    # How often WorkspaceSweepJob reclaims the directories of shipped tasks and
    # deleted projects/repos (ibid. §5.4). 0 disables the periodic pass; the
    # startup pass always runs.
    sprintbaton_workspace_sweep_interval_seconds: int = 3600

    # Git identity SprintBaton commits under (storage-layout-and-git-identity
    # spec §5.2, tier 3). Empty means "use the built-in constants in
    # vcs/git_service.py" (tier 4), so a zero-config install still commits.
    # Per-project / per-repo overrides (tiers 2 and 1) live on the entities.
    sprintbaton_git_author_name: str = ""
    sprintbaton_git_author_email: str = ""
    # Private key used for SSH remotes (ibid. §5.5). Unset = the host's
    # ambient SSH agent / ~/.ssh config, exactly today's behaviour.
    sprintbaton_git_ssh_key_path: str = ""

    # Deployment mode (zero-infra-storage spec §6): "tool" (zero-infra local
    # install — SQLite + filesystem blobs + in-process queue + flock) |
    # "hosted" (Mongo + S3/MinIO + Redis). Sets the default backend for every
    # concern below; any per-concern var set explicitly overrides it.
    sprintbaton_mode: str = "tool"

    # Per-concern backend overrides. None means "derive from sprintbaton_mode"
    # via backend_for(). Fails loudly on unknown names (in the build_* factories)
    # and on an unknown mode (here).
    #   persistence: "sqlite" | "mongo"   (persistence-abstraction spec §5)
    #   blob:        "filesystem" | "s3"  (zero-infra-storage spec §3)
    #   queue:       "in_process" | "redis"  (ibid. §4)
    #   lock:        "file" | "redis"        (ibid. §5)
    sprintbaton_persistence_backend: str | None = None
    sprintbaton_blob_backend: str | None = None
    sprintbaton_queue_backend: str | None = None
    sprintbaton_lock_backend: str | None = None
    #   sandbox:     "none" | "remote"       (hosted-sandbox-isolation spec §5.2)
    # Where model- and repo-driven commands run. "none" is tool mode's
    # passthrough (the clone on this machine); "remote" is the sandbox service.
    # Hosted mode fails closed: "none" there makes `serve` refuse to start.
    sprintbaton_sandbox_backend: str | None = None

    # The sandbox service (hosted-sandbox-isolation spec §16). URL and token
    # are required when the backend is "remote"; the token is a secret shared
    # by the worker and the sandbox pod and nothing else.
    sprintbaton_sandbox_url: str = ""
    sprintbaton_sandbox_token: str = ""
    # The port the worker's egress/credential broker listens on (§8.2). The
    # sandbox pod reaches it through a Service; NetworkPolicy admits only it.
    sprintbaton_sandbox_broker_port: int = 8081
    # Comma-separated hosts a run may CONNECT to ("*.suffix" matches
    # subdomains). Empty = the built-in public package registries (§8.2).
    sprintbaton_sandbox_egress_allowlist: str = ""
    # Commits of history shipped into a session's sandbox-side .git (§6.3).
    sprintbaton_sandbox_git_depth: int = 50
    # Upper bound on one collected change set, enforced by both the sandbox
    # service and the worker's apply (§6.4).
    sprintbaton_sandbox_max_changeset_bytes: int = 104857600

    # Tool-mode home directory (zero-infra-storage spec §7) — one directory
    # owns everything a local install writes: the .db file, blobs, and lock
    # files (the credential master key already defaults under it).
    sprintbaton_local_storage_root: str = "~/.sprintbaton"
    # None => {local_storage_root}/sprintbaton.db. Resolves the persistence
    # spec's §12 open question — the old CWD-relative "sprintbaton.db" default
    # was fragile across invocation directories. An explicit value still wins.
    sprintbaton_sqlite_path: str | None = None
    # None => {local_storage_root}/blobs (FilesystemBlobStore root).
    sprintbaton_blob_root: str | None = None

    # Crash recovery (zero-infra-storage spec §4.3): how old an in-flight
    # claim (Task.processingClaimedAt — which since the project-initialization-
    # task spec also covers metadata init runs) must be before startup reconciliation treats it as orphaned
    # by a crash rather than legitimately still running. Default: twice the
    # per-tier wall-clock budget (escalation spec §11), so a single
    # still-running process() call is never double-enqueued.
    sprintbaton_reconcile_stale_after_seconds: int = 3600

    # Multi-tenancy (user-multitenancy spec). The tenant identity CLI
    # commands operate as (repo/credential/agent commands and `init` stamp
    # and filter by it). The worker's polling/release loops are fully
    # multi-user — they enumerate active Repository rows across every tenant
    # (repository-onboarding spec §8) and never consult this value.
    sprintbaton_user_id: str = "local"
    # UserService config-resolution cache (spec §5): "memory" | "redis"
    sprintbaton_config_cache_backend: str = "memory"
    sprintbaton_config_cache_ttl_seconds: int = 60
    # Credential vault (spec §7): wraps per-credential data keys with a master
    # key held outside the entity database.
    #   "local"     — generated master key file (chmod 600); zero-infra default.
    #   "hashicorp" — master key read once from a HashiCorp Vault KV secret and
    #                 held in memory (never logged); the recommended hosted-mode
    #                 backend. Fails loudly on any other value.
    sprintbaton_vault_backend: str = "local"
    # local backend only:
    sprintbaton_master_key_path: str = "~/.sprintbaton/master.key"
    # hashicorp backend only. VAULT_ADDR / VAULT_TOKEN / VAULT_NAMESPACE follow
    # the standard HashiCorp env-var names (mount VAULT_TOKEN as a k8s secret).
    vault_addr: str = ""            # e.g. https://vault.internal:8200
    vault_token: str = ""
    vault_namespace: str = ""       # Vault Enterprise namespaces; optional
    vault_skip_verify: bool = False  # dev self-signed certs only
    # The KV secret path ("<mount>/<secret-path>"), the field within it holding
    # the Fernet master key, and the KV engine version (2 nests under data/data).
    sprintbaton_vault_key_path: str = "secrets/sprintbaton/masterKey"
    sprintbaton_vault_key_field: str = "value"
    sprintbaton_vault_kv_version: int = 2
    # Bearer-token lifetime for the CRUD API (spec §8)
    sprintbaton_session_ttl_seconds: int = 7 * 24 * 3600
    # `sprintbaton serve-api` bind address (spec §9)
    sprintbaton_api_host: str = "127.0.0.1"
    sprintbaton_api_port: int = 8080

    # MongoDB
    mongo_base_uri: str = "mongodb://localhost:27017"
    mongo_database: str = "sprintbaton"

    # PostgreSQL (SPRINTBATON_PERSISTENCE_BACKEND=postgres — pluggable-hosted-
    # backends spec §4.2). A libpq DSN/URI; TLS and every other connection
    # option ride it as standard libpq parameters (e.g. sslmode=verify-full).
    postgres_dsn: str = ""
    postgres_schema: str = ""          # "" = the connection's search_path
    postgres_pool_max_size: int = 10   # per process

    # Redis
    redis_uri: str = "redis://localhost:6379/0"

    # S3 blob driver (pluggable-hosted-backends spec §4.3): AWS S3, MinIO or
    # any S3-compatible store. Empty endpoint = the client's default for the
    # region (real AWS); empty keys = the default AWS credential chain.
    s3_endpoint: str = ""
    s3_region: str = ""
    s3_bucket: str = "sprintbaton"
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_create_bucket: bool = False     # off: a missing bucket fails startup
    s3_addressing_style: str = ""      # "" | path | virtual | auto

    # Google Cloud Storage blob driver (spec §4.4): application-default
    # credentials only (workload identity on GKE); never creates the bucket.
    gcs_bucket: str = ""
    gcs_project: str = ""              # "" = the credentials' default project

    # Azure Blob Storage driver (spec §4.5): DefaultAzureCredential by default;
    # a connection string for non-identity installs and Azurite. Never creates
    # the container. AZURE_ACCOUNT_URL overrides the derived
    # https://<account>.blob.core.windows.net (sovereign clouds, private
    # endpoints) — it never appears in stored azure:// URLs.
    azure_storage_account: str = ""
    azure_container: str = ""
    azure_storage_connection_string: str = ""
    azure_account_url: str = ""

    # Telemetry
    otel_exporter_otlp_endpoint: str = ""
    otel_service_name: str = "sprintbaton"
    otel_resource_attributes: str = ""
    log_level: str = "info"
    # Log verbosity (cli-logging spec §3): "silent" | "normal" | "verbose".
    # The env-var source of truth for hosted deployments; `sprintbaton
    # serve/init --verbose|-v / --quiet|-q` override it for one invocation.
    # Orthogonal to the console-vs-JSON output surface, which derives from
    # sprintbaton_mode (+ a TTY check and the --json flag).
    sprintbaton_log_verbosity: str = "normal"
    # `sprintbaton setup` types its logo banner out as a short animation on a
    # capable terminal; true prints the finished logo at once instead.
    sprintbaton_no_animation: bool = False

    # Escalation (flat env names per the README config reference)
    escalation_region_edit_limit: int = 3
    escalation_fix_attempts: int = 2
    escalation_noprogress_window: int = 4
    escalation_max_clarify_rounds: int = 3
    escalation_global_token_cap: int = 2_000_000
    escalation_tier_token_budget: int = 400_000
    escalation_tier_wall_clock_seconds: int = 1800
    escalation_ai_review_rounds: int = 2
    escalation_human_review_rounds: int = 3
    escalation_conflict_resolution_rounds: int = 2

    def backend_for(self, concern: str) -> str:
        """Two-level backend selection (zero-infra-storage spec §6): the
        explicit per-concern var wins; otherwise the mode's default. Fails
        loudly on an unknown mode or concern."""
        explicit = getattr(self, f"sprintbaton_{concern}_backend")
        if explicit is not None:
            if explicit not in BACKENDS[concern]:
                raise ValueError(
                    f"unknown {concern} backend {explicit!r} "
                    f"(SPRINTBATON_{concern.upper()}_BACKEND); expected one of "
                    f"{', '.join(BACKENDS[concern])}")
            return explicit
        defaults = _MODE_DEFAULTS.get(self.sprintbaton_mode)
        if defaults is None:
            raise ValueError(f"unknown mode: {self.sprintbaton_mode!r}")
        return defaults[concern]

    @property
    def local_login_sessions(self) -> bool:
        """The resolved value of sprintbaton_local_login_sessions: the explicit
        declaration when set, otherwise the mode default (tool -> True).
        Always False in hosted mode (hosted-sandbox-isolation spec §12.1): a
        hosted worker's on-disk login would be the operator's, and hosted mode
        never lends the operator's credentials to a tenant."""
        if self.sprintbaton_mode == "hosted":
            return False
        if self.sprintbaton_local_login_sessions is not None:
            return self.sprintbaton_local_login_sessions
        return self.sprintbaton_mode == "tool"

    @property
    def subscription_token_fallback(self) -> str:
        """Tier 3 of the subscription-token chain (hosted-sandbox-isolation
        spec §9.2): CLAUDE_CODE_OAUTH_TOKEN in tool mode, nothing in hosted
        mode — a tenant without their own subscription credential never runs
        on the operator's."""
        return "" if self.sprintbaton_mode == "hosted" else self.claude_code_oauth_token

    @model_validator(mode="after")
    def _derive_workspace_root(self) -> "Settings":
        if self.workspace_root is None:
            self.workspace_root = str(self.local_storage_root / "workspaces")
        return self

    @property
    def mirror_root(self) -> str:
        """The resolved mirror root (workspace-mirrors spec §4.2)."""
        if self.sprintbaton_mirror_root is not None:
            return self.sprintbaton_mirror_root
        return str(Path(self.workspace_root) / ".mirrors")

    @property
    def local_storage_root(self) -> Path:
        return Path(self.sprintbaton_local_storage_root).expanduser()

    @property
    def resolved_sqlite_path(self) -> str:
        if self.sprintbaton_sqlite_path is not None:
            return self.sprintbaton_sqlite_path
        return str(self.local_storage_root / "sprintbaton.db")

    @property
    def resolved_blob_root(self) -> str:
        if self.sprintbaton_blob_root is not None:
            return self.sprintbaton_blob_root
        return str(self.local_storage_root / "blobs")

    @property
    def escalation(self) -> EscalationConfig:
        return EscalationConfig(
            region_edit_limit=self.escalation_region_edit_limit,
            fix_attempts=self.escalation_fix_attempts,
            noprogress_window=self.escalation_noprogress_window,
            max_clarify_rounds=self.escalation_max_clarify_rounds,
            global_token_cap=self.escalation_global_token_cap,
            tier_token_budget=self.escalation_tier_token_budget,
            tier_wall_clock_seconds=self.escalation_tier_wall_clock_seconds,
            ai_review_max_rounds=self.escalation_ai_review_rounds,
            human_review_max_rounds=self.escalation_human_review_rounds,
            conflict_resolution_max_rounds=self.escalation_conflict_resolution_rounds,
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()

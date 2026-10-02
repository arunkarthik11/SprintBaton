"""The SprintBaton CRUD API (user-multitenancy spec §9) — the first HTTP
surface in this codebase, meant to back a web UI in hosted mode.

Every route is a thin adapter over the same EntityDAO-backed entities the CLI
already writes: the API only ever writes entities; the unmodified polling job
and orchestrator (in the separate `sprintbaton serve` deployable) are what
actually act on them — a normal control-plane/worker-pool split sharing one
storage backend.
"""

from dataclasses import dataclass

from fastapi import FastAPI

from sprintbaton.analytics.recorder import INDEXED_FIELDS
from sprintbaton.analytics.reporting import TokenUsageQueryEngine
from sprintbaton.config.settings import Settings, get_settings
from sprintbaton.entities.agent_definition import AgentDefinition
from sprintbaton.entities.credential import Credential
from sprintbaton.entities.events import TaskActionEvent
from sprintbaton.entities.project import Project
from sprintbaton.entities.provider import Provider
from sprintbaton.entities.repository import Repository
from sprintbaton.entities.task import Task
from sprintbaton.entities.user import Session, User
from sprintbaton.entities.user_config import UserConfiguration
from sprintbaton.storage import build_cache_client, build_lock, build_storage
from sprintbaton.storage.base import DistributedLock, EntityDAO, PersistenceBackend
from sprintbaton.users.auth import AuthService
from sprintbaton.users.config_cache import build_config_cache
from sprintbaton.users.credentials import CredentialService
from sprintbaton.users.service import UserService
from sprintbaton.users.vault import build_vault


@dataclass
class ApiState:
    """Everything the routes need, hung off app.state."""

    settings: Settings
    auth: AuthService
    users: UserService
    credentials: CredentialService
    agent_definitions: EntityDAO[AgentDefinition]
    repositories: EntityDAO[Repository]
    projects: EntityDAO[Project]
    harness_names: list[str]
    # The metadata routes only persist or read init-run Task rows and take the
    # project-init lock (project-initialization-task spec §5.1-§5.2): the API
    # pod never runs a model, clones a repo, or enqueues — the polling lane of
    # `sprintbaton serve` discovers the row.
    lock: DistributedLock
    # Token usage reporting (token-usage-reporting spec §5/§7): the read-only
    # query engine over task_action_events, plus the task repo the by-task
    # route joins titles from (presentation-only, §8).
    usage_reports: TokenUsageQueryEngine
    tasks: EntityDAO[Task]
    # Provider rows, read by POST /agents for the base-URL advisory
    # (hosted-sandbox-isolation spec §9.3). None in states built by hand.
    providers: EntityDAO[Provider] | None = None


def _harness_classes() -> tuple[type, ...]:
    """Every harness class container.build_container registers, by class — the
    API never executes a harness, so it reads class attributes without
    constructing any.

    One list feeding both callers below. It was previously duplicated and had
    drifted: six registered harnesses (codex_cli, gemini_cli and the four
    OpenAI/Gemini in-process ones) were missing, so POST /agents rejected them
    as unknown while `sprintbaton agents create` accepted them.
    """
    from sprintbaton.harness import (
        ChainedHarness,
        ClaudeAgentSdkHarness,
        ClaudeCodeCliHarness,
        CodexCliHarness,
        GeminiAgentSdkHarness,
        GeminiCliHarness,
        GeminiSingleShotHarness,
        OpenAiAgentSdkHarness,
        OpenAiSingleShotHarness,
        OpenHandsHarness,
        RawToolLoopHarness,
        SingleShotHarness,
    )

    del ChainedHarness  # registered under its own name by the caller that uses it
    return (SingleShotHarness, RawToolLoopHarness, ClaudeAgentSdkHarness,
            OpenHandsHarness, ClaudeCodeCliHarness, CodexCliHarness,
            GeminiCliHarness, OpenAiSingleShotHarness, OpenAiAgentSdkHarness,
            GeminiSingleShotHarness, GeminiAgentSdkHarness)


def _known_harness_names() -> list[str]:
    """Registered harness names for POST /agents validation."""
    return sorted({h.name for h in _harness_classes()})


def subscription_only_harnesses() -> frozenset[str]:
    """Harnesses that declare supports_metered_auth = False (auth-mode-
    resolution spec §3) — they authenticate only from a subscription or CLI
    login. Since cli-subscription-auth-parity §4.1 this is every harness that
    spawns a logged-in CLI, not just claude_code_cli."""
    return frozenset(
        h.name for h in _harness_classes()
        if not getattr(h, "supports_metered_auth", True)
    )


def harness_class(name: str):
    """The registered harness class for `name`, or None. Class attributes only
    — the API never constructs or executes a harness."""
    for cls in _harness_classes():
        if cls.name == name:
            return cls
    return None


def build_api_state(settings: Settings,
                    storage: PersistenceBackend | None = None) -> ApiState:
    storage = storage if storage is not None else build_storage(settings)

    user_repo = storage.repository(User, "users")
    user_repo.ensure_indexes("email")
    session_repo = storage.repository(Session, "sessions")
    session_repo.ensure_indexes("tokenHash")
    credential_repo = storage.repository(Credential, "credentials")
    credential_repo.ensure_indexes("userId", "provider")
    user_config_repo = storage.repository(UserConfiguration, "user_configurations")
    user_config_repo.ensure_indexes("userId")
    agent_def_repo = storage.repository(AgentDefinition, "agent_definitions")
    agent_def_repo.ensure_indexes("name", "userId")
    repo_repo = storage.repository(Repository, "repositories")
    repo_repo.ensure_indexes("userId", "projectId")
    project_repo = storage.repository(Project, "projects")
    project_repo.ensure_indexes("userId", "active")
    event_repo = storage.repository(TaskActionEvent, "task_action_events")
    event_repo.ensure_indexes(*INDEXED_FIELDS)
    task_repo = storage.repository(Task, "tasks")
    task_repo.ensure_indexes("userId", "kind", "externalId", "isCurrentRound")

    credentials = CredentialService(credential_repo, build_vault(settings))

    return ApiState(
        settings=settings,
        auth=AuthService(user_repo, session_repo,
                         settings.sprintbaton_session_ttl_seconds),
        users=UserService(user_config_repo, settings,
                          build_config_cache(settings, build_cache_client(settings))),
        credentials=credentials,
        agent_definitions=agent_def_repo,
        repositories=repo_repo,
        projects=project_repo,
        harness_names=_known_harness_names(),
        lock=build_lock(settings),
        usage_reports=TokenUsageQueryEngine(event_repo),
        tasks=task_repo,
        providers=storage.repository(Provider, "providers"),
    )


def create_app(settings: Settings | None = None,
               state: ApiState | None = None) -> FastAPI:
    from sprintbaton.api.routes import (
        accounts,
        agents,
        config,
        credentials,
        reports,
        repositories,
    )

    settings = settings if settings is not None else get_settings()
    app = FastAPI(title="SprintBaton API", version="0.1.0")
    app.state.api = state if state is not None else build_api_state(settings)
    app.include_router(accounts.router)
    app.include_router(agents.router)
    app.include_router(credentials.router)
    app.include_router(repositories.router)
    app.include_router(config.router)
    app.include_router(reports.router)
    return app

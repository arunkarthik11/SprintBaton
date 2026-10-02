"""Dependency wiring for the SprintBaton runtime."""

import logging
import os
from dataclasses import dataclass

from sprintbaton.analytics.recorder import TaskActionEventRecorder
from sprintbaton.analytics.reporting import TokenUsageQueryEngine
from sprintbaton.clarification.conversations import ConversationRunner
from sprintbaton.clarification.translator import ClarificationTranslator
from sprintbaton.config.settings import Settings, get_settings
from sprintbaton.entities.agent_definition import AgentDefinition
from sprintbaton.entities.credential import Credential
from sprintbaton.entities.events import TaskActionEvent
from sprintbaton.entities.message import Conversation
from sprintbaton.entities.project import Project
from sprintbaton.entities.release import Release
from sprintbaton.entities.repository import Repository
from sprintbaton.entities.task import Task
from sprintbaton.harness import (
    ClaudeAgentSdkHarness,
    ClaudeCodeCliHarness,
    CodexCliHarness,
    GeminiAgentSdkHarness,
    GeminiCliHarness,
    GeminiSingleShotHarness,
    HarnessRegistry,
    OpenAiAgentSdkHarness,
    OpenAiSingleShotHarness,
    OpenHandsHarness,
    RawToolLoopHarness,
    SingleShotHarness,
)
from sprintbaton.models.definitions import AgentDefinitionResolver
from sprintbaton.models.usage_limits import DefaultUsageLimitPolicy
from sprintbaton.entities.provenance import ClassificationRecord
from sprintbaton.entities.provider import Provider
from sprintbaton.providers.availability import ProviderAvailabilityService
from sprintbaton.providers.service import ProviderService
from sprintbaton.observer.metrics import Observer
from sprintbaton.observer.telemetry import setup_logging, setup_telemetry
from sprintbaton.orchestrator.orchestrator import TaskOrchestrator
from sprintbaton.polling.polling_job import PendingTaskPollingJob
from sprintbaton.prompts.registry import PromptRegistry
from sprintbaton.provenance.git_store import ProvenanceGitStore
from sprintbaton.provenance.snapshot import ContextSnapshotService
from sprintbaton.release.window_job import ReleaseWindowJob
from sprintbaton.scheduling.usage_limit_wake_job import UsageLimitWakeJob
from sprintbaton.scheduling.workspace_sweep import WorkspaceSweepJob
from sprintbaton.entities.task_revision import CardSnapshot, TaskRevision
from sprintbaton.workspace.cleanup import WorkspaceCleaner
from sprintbaton.entities.user import User
from sprintbaton.entities.user_config import UserConfiguration
from sprintbaton.sandbox.binding import SandboxRuntime
from sprintbaton.sandbox.factory import build_sandbox_runtime
from sprintbaton.services.base import ServiceContext, TaskActionServiceFactory
from sprintbaton.storage import (
    build_blob_store,
    build_cache_client,
    build_lock,
    build_storage,
    build_task_queue,
)
from sprintbaton.storage.base import DistributedLock, EntityDAO, TaskQueue
from sprintbaton.users.config_cache import build_config_cache
from sprintbaton.users.credentials import CredentialService
from sprintbaton.users.service import UserService
from sprintbaton.users.vault import build_vault
from sprintbaton.workspace.task_workspace import TaskWorkspaceService

log = logging.getLogger(__name__)


@dataclass
class Container:
    settings: Settings
    ctx: ServiceContext
    factory: TaskActionServiceFactory
    orchestrator: TaskOrchestrator
    polling_job: PendingTaskPollingJob
    release_job: ReleaseWindowJob
    usage_limit_wake_job: UsageLimitWakeJob
    # Reclaims shipped tasks' and deleted projects'/repos' directories
    # (workspace-mirrors-and-cleanup spec §5.4).
    workspace_sweep_job: WorkspaceSweepJob
    task_queue: TaskQueue
    # The project-init lock onboarding and `sprintbaton init` take
    # (project-initialization-task spec §5.1).
    lock: DistributedLock
    agent_definitions: EntityDAO[AgentDefinition]
    harness_registry: HarnessRegistry
    user_service: UserService
    usage_reports: TokenUsageQueryEngine
    provider_service: ProviderService
    provider_availability: ProviderAvailabilityService
    providers: EntityDAO[Provider]
    # Where model- and repo-driven commands run (hosted-sandbox-isolation spec
    # §5): the tool-mode passthrough or the remote sandbox service, plus the
    # egress broker `serve` starts for the latter.
    sandbox: SandboxRuntime


# Ambient Anthropic credentials a locally spawned `claude` could otherwise
# inherit (the Agent SDK's transport overlays options.env onto os.environ and
# can never delete a key).
_AMBIENT_AUTH_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


def strip_ambient_model_credentials() -> None:
    """Tool-mode env hygiene for locally spawned CLIs (hosted-sandbox-isolation
    spec §12.1 — the one part of the old process-auth normalization that
    survives).

    Removes the ambient Anthropic keys from this process's environment once,
    at startup, before any harness runs. Nothing is lost: Settings has already
    captured the value into settings.anthropic_api_key, which is tier 3 of the
    credential chain every metered call resolves through and injects
    explicitly — so a run is billed to exactly the credential the resolver
    chose, never to whatever the shell happened to export.

    Hosted mode needs none of this: a sandboxed run's environment is built
    from nothing (§8.3), and CLAUDE_CODE_OAUTH_TOKEN is no longer a hosted
    secret at all — each owner stores their own subscription credential (§9).
    """
    for var in _AMBIENT_AUTH_VARS:
        os.environ.pop(var, None)


def build_harness_registry(settings: Settings,
                           sandbox: SandboxRuntime | None = None) -> HarnessRegistry:
    """Every registered harness, by name — the set an AgentDefinition (and the
    provider-seeding runtime table) may reference.

    Extracted from build_container so the seeding table can be asserted against
    the real registry without standing up storage (provider-setup-cli spec §11
    invariant 3).

    Every harness is constructed regardless of which are selected — they are
    cheap to construct; only execute() does real work (migration spec §5). The
    metered harnesses take no key at construction (per-user-provider-credentials
    spec §4.7): the per-user credential rides ModelSpec.api_key, resolved per
    call by AgentDefinitionResolver, so the singletons are safe to share across
    every user's calls.

    The harnesses that run tools or a CLI process take the sandbox runtime
    (hosted-sandbox-isolation spec §7); None is tool mode's passthrough.
    """
    return HarnessRegistry({
        harness.name: harness
        for harness in (
            SingleShotHarness(),
            RawToolLoopHarness(sandbox=sandbox),
            ClaudeAgentSdkHarness(sandbox=sandbox),
            OpenHandsHarness(settings.open_hands_llm_api_key,
                             settings.open_hands_llm_base_url),
            # Deliberately no API key parameter — it inherits the host's own
            # `claude login` session and nothing else (claude-code-cli spec §5.4)
            ClaudeCodeCliHarness(),
            # Subprocess wrappers around already-installed CLIs (provider-
            # registration spec §6). Registered unconditionally — registration
            # costs nothing; only *use* of a provider never registered via
            # `providers add` fails, at resolution time (spec §6.3).
            CodexCliHarness(),
            GeminiCliHarness(),
            # In-process agent-framework + single-shot harnesses for OpenAI /
            # Google (multi-provider-parity spec §4.1/§4.2). Registered
            # unconditionally — construction is free and lazy-imports the
            # optional SDKs only inside execute(); only *use* of an
            # uninstalled provider SDK fails, at run time.
            OpenAiSingleShotHarness(),
            OpenAiAgentSdkHarness(sandbox=sandbox),
            GeminiSingleShotHarness(),
            GeminiAgentSdkHarness(sandbox=sandbox),
        )
    })


def build_container() -> Container:
    settings = get_settings()
    setup_logging(settings)
    # Before any harness can run, and after Settings captured the ambient keys.
    if settings.sprintbaton_mode != "hosted":
        strip_ambient_model_credentials()
    sandbox = build_sandbox_runtime(settings)
    harness_registry = build_harness_registry(settings, sandbox)
    setup_telemetry(settings)

    storage = build_storage(settings)
    task_repo = storage.repository(Task, "tasks")
    # "kind": init-run discovery and lookup (project-initialization-task §5.2)
    task_repo.ensure_indexes("userId", "kind", "externalId", "isCurrentRound")
    # The poller's observations (task-revisions spec §4-§5): written only by
    # the poller, reconciled by the orchestrator — never the Task row itself.
    snapshot_repo = storage.repository(CardSnapshot, "card_snapshots")
    snapshot_repo.ensure_indexes("userId", "taskId")
    revision_repo = storage.repository(TaskRevision, "task_revisions")
    revision_repo.ensure_indexes("userId", "taskId", "revision")
    repo_repo = storage.repository(Repository, "repositories")
    repo_repo.ensure_indexes("userId", "projectId")
    project_repo = storage.repository(Project, "projects")
    project_repo.ensure_indexes("userId", "active")
    event_repo = storage.repository(TaskActionEvent, "task_action_events")
    release_repo = storage.repository(Release, "releases")
    release_repo.ensure_indexes("projectId", "status", "userId")
    agent_def_repo = storage.repository(AgentDefinition, "agent_definitions")
    agent_def_repo.ensure_indexes("name", "userId")
    conversation_repo = storage.repository(Conversation, "conversations")
    conversation_repo.ensure_indexes("taskId", "action", "userId")
    classification_record_repo = storage.repository(
        ClassificationRecord, "classification_records")
    classification_record_repo.ensure_indexes("taskId", "action", "category", "userId")
    user_config_repo = storage.repository(UserConfiguration, "user_configurations")
    user_config_repo.ensure_indexes("userId")
    user_repo = storage.repository(User, "users")
    user_repo.ensure_indexes("email")
    credential_repo = storage.repository(Credential, "credentials")
    credential_repo.ensure_indexes("userId", "provider")
    provider_repo = storage.repository(Provider, "providers")
    provider_repo.ensure_indexes("userId", "name", "active")
    # Backend-selected infra (zero-infra-storage spec §10): tool mode gets
    # the in-process queue, filesystem blobs, and flock; hosted mode Redis +
    # S3/MinIO. Every Redis-backed consumer (queue, lock, config cache)
    # shares one per-process client (pluggable-hosted-backends spec §4.1).
    task_queue = build_task_queue(settings)
    object_storage = build_blob_store(settings)
    lock = build_lock(settings)
    config_cache = build_config_cache(settings, build_cache_client(settings))
    user_service = UserService(user_config_repo, settings, config_cache)
    # Per-repo tokens (repository-onboarding spec §4): the worker resolves
    # GitService/TaskAdapter per repository via ctx.git_for/task_adapter_for
    credentials = CredentialService(credential_repo, build_vault(settings))
    # No model provider is constructed here any more: every model call —
    # the metadata init pass included, since the project-initialization-task
    # spec — goes through a harness resolved per call.
    credentials_provider_service = ProviderService(
        provider_repo, credentials, hosted=settings.sprintbaton_mode == "hosted")
    provider_availability = ProviderAvailabilityService(
        provider_repo, agent_def_repo, event_repo)
    agent_resolver = AgentDefinitionResolver(
        agent_def_repo, harness_registry, settings, user_service,
        providers=credentials_provider_service, availability=provider_availability,
        credentials=credentials, isolated=sandbox.isolated)
    prompts = PromptRegistry()
    observer = Observer()
    analytics = TaskActionEventRecorder(event_repo)
    usage_reports = TokenUsageQueryEngine(event_repo)  # shares the repo, read-only
    workspace_cleaner = WorkspaceCleaner(
        settings.workspace_root, settings.mirror_root,
        harness_names=harness_registry.names())
    task_workspace = TaskWorkspaceService(object_storage, cleaner=workspace_cleaner)
    provenance = ContextSnapshotService(
        ProvenanceGitStore(object_storage, settings.workspace_root, locker=lock),
        task_workspace, classification_record_repo,
        enabled=settings.sprintbaton_provenance_enabled,
    )

    ctx = ServiceContext(
        settings=settings,
        prompts=prompts,
        task_repo=task_repo,
        repo_repo=repo_repo,
        project_repo=project_repo,
        release_repo=release_repo,
        object_storage=object_storage,
        translator=ClarificationTranslator(),
        observer=observer,
        analytics=analytics,
        agents=agent_resolver,
        conversations=ConversationRunner(
            conversation_repo=conversation_repo,
            object_storage=object_storage,
            resume_window_seconds=settings.conversation_resume_window_seconds,
            agent_user_id=settings.sprintbaton_agent_user_id,
        ),
        task_workspace=task_workspace,
        credentials=credentials,
        provenance=provenance,
        usage_limits=DefaultUsageLimitPolicy(
            aware=settings.sprintbaton_usage_limit_aware,
            urgent_label=settings.sprintbaton_usage_limit_urgent_label,
            default_backoff_seconds=settings.sprintbaton_usage_limit_default_backoff_seconds,
        ),
        provider_availability=provider_availability,
        task_queue=task_queue,
        lock=lock,
        user_service=user_service,
        sandbox=sandbox,
        snapshot_repo=snapshot_repo,
        revision_repo=revision_repo,
    )
    factory = TaskActionServiceFactory(ctx)
    orchestrator = TaskOrchestrator(ctx, factory)
    polling_job = PendingTaskPollingJob(
        adapter_for=ctx.task_adapter_for,
        task_repo=task_repo,
        project_repo=project_repo,
        queue=task_queue,
        interval_seconds=settings.poll_interval_seconds,
        snapshot_repo=snapshot_repo,
        revision_repo=revision_repo,
    )
    release_job = ReleaseWindowJob(
        ctx, interval_seconds=settings.release_check_interval_seconds
    )
    usage_limit_wake_job = UsageLimitWakeJob(
        task_repo, task_queue,
        interval_seconds=settings.sprintbaton_usage_limit_wake_check_interval_seconds,
        provider_availability=provider_availability,
    )
    workspace_sweep_job = WorkspaceSweepJob(
        workspace_cleaner, task_repo, project_repo, repo_repo,
        interval_seconds=settings.sprintbaton_workspace_sweep_interval_seconds)
    return Container(
        settings=settings, ctx=ctx, factory=factory, orchestrator=orchestrator,
        polling_job=polling_job, release_job=release_job,
        usage_limit_wake_job=usage_limit_wake_job,
        workspace_sweep_job=workspace_sweep_job,
        task_queue=task_queue,
        lock=lock,
        agent_definitions=agent_def_repo, harness_registry=harness_registry,
        user_service=user_service, usage_reports=usage_reports,
        provider_service=credentials_provider_service,
        provider_availability=provider_availability, providers=provider_repo,
        sandbox=sandbox,
    )

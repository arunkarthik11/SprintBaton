"""TaskActionService interface, shared service context, and the factory that
maps a TaskStatus to the service that acts on it (docs/entities.md)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from sprintbaton.adaptors import create_adapter
from sprintbaton.adaptors.base import TaskAdapter
from sprintbaton.analytics.recorder import TaskActionEventRecorder
from sprintbaton.clarification.conversations import ConversationRunner
from sprintbaton.clarification.translator import ClarificationTranslator
from sprintbaton.config.settings import Settings
from sprintbaton.entities.enums import CredentialProvider, TaskStatus
from sprintbaton.entities.project import Project
from sprintbaton.entities.release import Release
from sprintbaton.entities.repository import Repository
from sprintbaton.entities.task import Task
from sprintbaton.models.definitions import AgentDefinitionResolver
from sprintbaton.models.usage_limits import UsageLimitPolicy
from sprintbaton.observer.metrics import Observer
from sprintbaton.prompts.registry import PromptRegistry
from sprintbaton.providers.availability import ProviderAvailabilityService
from sprintbaton.provenance.snapshot import ContextSnapshotService
from sprintbaton.storage.base import EntityDAO
from sprintbaton.storage.base import BlobStore, DistributedLock, TaskQueue
from sprintbaton.users.credentials import CredentialService
from sprintbaton.users.service import UserService
from sprintbaton.vcs.git_service import GitService, build_git_service
from sprintbaton.workspace.locations import (
    project_location_guide,
    repo_location_guide,
)
from sprintbaton.workspace.task_workspace import TaskWorkspaceService

if TYPE_CHECKING:
    from sprintbaton.entities.actions import TaskActionResponse
    from sprintbaton.entities.message import Conversation
    from sprintbaton.sandbox.binding import SandboxRuntime


@dataclass
class ServiceContext:
    settings: Settings
    prompts: PromptRegistry
    task_repo: EntityDAO[Task]
    repo_repo: EntityDAO[Repository]
    project_repo: EntityDAO[Project]
    release_repo: EntityDAO[Release]
    object_storage: BlobStore
    translator: ClarificationTranslator
    observer: Observer
    analytics: TaskActionEventRecorder
    agents: AgentDefinitionResolver  # per-action (harness, model, prompt) resolution
    conversations: ConversationRunner  # shared human-pause lifecycle (conversation-lifecycle spec §7)
    task_workspace: TaskWorkspaceService  # .sprintbaton/tasks/<task_id>/ materialization (task-workspace spec §5)
    credentials: CredentialService  # three-tier token resolution (repository-onboarding spec §4)
    # Classification provenance / context snapshots (classification-provenance
    # spec). Nullable — a None means snapshotting is off; every orchestrator
    # call site guards on it so the SimpleNamespace-based tests are unaffected.
    provenance: ContextSnapshotService | None = None
    # Usage-limit pause policy (usage-limit-aware execution spec §4.4). The
    # container always wires DefaultUsageLimitPolicy (aware=False is itself a
    # complete no-op); typed optional only so SimpleNamespace-based tests —
    # whose responses never carry signals — need no change.
    usage_limits: UsageLimitPolicy | None = None
    # Provider quota-pool availability + the task queue, for the fallback
    # router (agent-fallback spec §4): on a usage-limit signal the orchestrator
    # marks the ran provider inactive and, if the chain has another available
    # agent, re-enqueues the task to walk to it instead of pausing. Both
    # optional so SimpleNamespace-based tests are unaffected.
    provider_availability: ProviderAvailabilityService | None = None
    task_queue: TaskQueue | None = None
    # The cross-process lock the metadata init pass swaps revision pointers
    # under (project-initialization-task spec §5.1/§9.2), and per-user Settings
    # for its tuning knobs (§12). Optional for the SimpleNamespace-based tests.
    lock: DistributedLock | None = None
    user_service: UserService | None = None
    # Where model- and repo-driven commands run (hosted-sandbox-isolation
    # spec §5). The orchestrator closes a task's session at Shipped/Blocked
    # (§6.6); harnesses hold their own reference. None in SimpleNamespace tests.
    sandbox: "SandboxRuntime | None" = None
    # The poller's observations (task-revisions spec §4-§5): the orchestrator
    # reconciles them at the start of every process(). None in the
    # SimpleNamespace/unit contexts, where the reconcile pass is skipped.
    snapshot_repo: EntityDAO | None = None
    revision_repo: EntityDAO | None = None

    # GitService/TaskAdapter are constructed per repo per call — thin,
    # stateless HTTP wrappers, cheap enough that not having to invalidate a
    # cache on credential rotation wins (repository-onboarding spec §4.3).

    def git_for(self, repo: Repository, project: Project | None = None) -> GitService:
        token = self.credentials.resolve(
            repo.userId, CredentialProvider.GITHUB, repo.githubCredentialId,
            fallback=self.settings.github_token)
        # The project supplies tier 2 of the commit identity (storage-layout-
        # and-git-identity spec §5.2); callers already holding it pass it in
        # rather than paying for the lookup again.
        if project is None:
            project = self.project_of(repo)
        return build_git_service(token, self.settings, repo, project)

    def task_adapter_for(self, project: Project) -> TaskAdapter:
        # The todolist board lives on the Project now (multi-repo-project spec
        # §4.1), so the adapter/token resolve per project, not per repo.
        token = self.credentials.resolve(
            project.userId, CredentialProvider(project.todolistProvider),
            project.todolistCredentialId, fallback=self.settings.todolist_api_token)
        return create_adapter(project.todolistProvider, token)

    def project_of(self, task_or_repo) -> Project | None:
        """Resolve the Project for a task or repository (multi-repo-project
        spec §4). None only on a dangling reference."""
        pid = getattr(task_or_repo, "projectId", "")
        return self.project_repo.get(pid) if pid else None

    def repositories_for(self, project: Project) -> list[Repository]:
        """Every member repo of a project (multi-repo-project spec §4.2)."""
        return list(self.repo_repo.find(
            {"projectId": project.id, "deleted": False}))


def settings_for(ctx, user_id: str) -> Settings:
    """Per-user Settings when a UserService is wired (UserConfiguration
    overrides apply), else the deployment Settings — tolerant of the
    SimpleNamespace contexts unit tests build."""
    user_service = getattr(ctx, "user_service", None)
    if user_service is not None:
        return user_service.settings_for(user_id)
    return ctx.settings


def read_only_workspaces(ctx: ServiceContext, agent, task: Task,
                         project: Project, repos: list[Repository]
                         ) -> tuple[str, str]:
    """Provision a read-only clone of EVERY member repo a project-level
    tool-loop agent browses (multi-repo-project spec §6), plus materialize the
    combined project-metadata index outside all clones.

    Returns (harness cwd, location guide) — ("", "") when the resolved harness
    is single_shot or no repo has a remote, so the harness offers no filesystem
    tools and the prompt promises no paths. The guide is built here rather than
    re-derived later because this is the one place that already holds the clone
    path prepare_read_only_workspace actually returned for each repo
    (storage-layout spec §10 q4).

    The agent navigates from the project index (which says what each repo is)
    plus the guide (which says where each one currently sits) into any repo it
    needs (spec §5.3)."""
    from sprintbaton.harness.single_shot import SingleShotHarness  # avoid cycle

    clonable = [r for r in repos if r.remoteUrl]
    if agent.harness.name == SingleShotHarness.name or not clonable:
        return "", ""
    git = None
    clones: list[tuple[Repository, Path]] = []
    for repo in clonable:
        git = ctx.git_for(repo)
        workspace = git.prepare_read_only_workspace(
            task.id, repo.remoteUrl, repo.devBranch, repo_id=repo.id)
        ctx.task_workspace.materialize(workspace, project.id, task)
        ctx.task_workspace.materialize_repo_metadata(workspace, repo)
        clones.append((repo, Path(workspace)))
    # The combined index sits outside every clone (spec §6)
    if git is not None:
        index_dir = git.project_index_dir(task.id)
        ctx.task_workspace.materialize_project_metadata(index_dir, project, repos)
        task_root = git.task_root(task.id)
        return str(task_root), project_location_guide(task_root, clones, index_dir)
    return "", ""


# Back-compat single-repo shim: some callers/tests still ask for one repo's
# read-only clone. Retained as a thin wrapper (multi-repo-project spec §6).
def read_only_workspace_path(ctx: ServiceContext, agent, task: Task,
                             repo: Repository) -> str:
    from sprintbaton.harness.single_shot import SingleShotHarness

    if agent.harness.name == SingleShotHarness.name or not repo.remoteUrl:
        return ""
    workspace = ctx.git_for(repo).prepare_read_only_workspace(
        task.id, repo.remoteUrl, repo.devBranch, repo_id=repo.id)
    ctx.task_workspace.materialize(workspace, repo.projectId or repo.id, task)
    ctx.task_workspace.materialize_repo_metadata(workspace, repo)
    return str(workspace)


class TaskActionService(ABC):
    # The AgentDefinitionResolver action-name string this service runs as —
    # also the Conversation episode key (conversation-lifecycle spec §4.1).
    action_name: str = ""

    def __init__(self, ctx: ServiceContext):
        self.ctx = ctx

    @abstractmethod
    def run(self, task: Task, repo: Repository, **kwargs) -> "TaskActionResponse": ...

    def _begin_turn(self, task: Task, project: Project,
                    action: str | None = None
                    ) -> tuple["Conversation", str | None, str | None]:
        """Resolve this turn's clarification conversation (conversation-
        lifecycle spec §7.2). Returns (conversation, reply_text,
        clarification_context) — thread the latter two plus the conversation's
        harnessSessionId (resume branch only) into the request. Keyed by the
        project (the board owner), one episode per (taskId, action)."""
        conversation, reply_text, clarification_context = self.ctx.conversations.resolve(
            task, action or self.action_name, project.id,
            self.ctx.task_adapter_for(project))
        if conversation.id not in task.conversationIds:
            task.conversationIds.append(conversation.id)
        return conversation, reply_text, clarification_context

    def _turn_kwargs(self, conversation: "Conversation",
                     reply_text: str | None,
                     clarification_context: str | None) -> dict:
        """The uniform request kwargs for _begin_turn's resolved triple:
        the resume-branch session id plus, when the reply mapped onto the
        paused turn's structured options, the matched selection
        (clarification-options spec §4.4/§6)."""
        return {
            "conversation_id": (conversation.harnessSessionId
                                if reply_text is not None else None),
            "reply_text": reply_text,
            "clarification_context": clarification_context,
            "selected_answer": (conversation.selectedAnswer
                                if reply_text is not None else None),
            "selected_answers": (conversation.selectedAnswers
                                 if reply_text is not None else None),
        }

    def _amendment_kwargs(self, task: Task, action: str | None = None) -> dict:
        """The stage's pending amendment, if it is rerunning because of a card
        edit or a backward move (task-revisions spec §7.6)."""
        return {"amendment": task.amendments.get(action or self.action_name)}

    def _end_turn(self, conversation: "Conversation",
                  response: "TaskActionResponse",
                  question: str | None = None) -> None:
        """Record the turn; close the episode unless it paused on a question."""
        question = question if question is not None else response.clarificationQuestion
        if question is None and response.usageLimitSignals:
            # A usage-limit interruption is not an answered turn: record it
            # (capturing the harness session id, the resume handle) and leave
            # the episode open so the wake-job re-entry can resume the same
            # session (usage-limit-aware execution spec §8).
            self.ctx.conversations.record_turn(
                conversation, harness_session_id=response.conversationId,
                turn_text="(interrupted by a usage limit)")
            return
        self.ctx.conversations.record_turn(
            conversation, harness_session_id=response.conversationId,
            turn_text=question or "(answered)",
            # Persisted with the pause so the async reply can be matched
            # against the offered options (clarification-options spec §6)
            pending_options=response.clarificationOptions if question else None,
        )
        if question is None:
            self.ctx.conversations.close(conversation, reason="answered")

    def metadata_summary(self, task: Task, project: Project, *,
                         repo: Repository | None = None,
                         workspace_path: str = "",
                         location_guide: str = "") -> str:
        """The project context injected into every role's prompt: the combined
        project-metadata index (multi-repo-project spec §5.3), which names each
        member repo and points at that repo's own .sprintbaton/ metadata, over
        a role-specific guide to where those things currently sit on disk.

        The index itself is generated once at init time and states no paths —
        it cannot, because the browsing roles run in the task root over
        read-only clones while the per-repo roles run inside one clone, one
        level deeper (storage-layout spec §10 q4). A workspace-less role
        (single_shot) passes neither, and gets the prose alone."""
        guide = location_guide
        if not guide and workspace_path:
            guide = (repo_location_guide(task.id, repo, workspace_path)
                     if repo is not None else "")
        return f"{guide}\n{self._project_index(project)}" if guide \
            else self._project_index(project)

    def _project_index(self, project: Project) -> str:
        if project.metadataUrl:
            text = self.ctx.object_storage.get_text_by_url(project.metadataUrl)
            if text:
                return text[:20_000]
        return ("(no project metadata generated yet — it is produced by the "
                "project's initialization run; see `sprintbaton init --status`)")


class TaskActionServiceFactory:
    """Returns the appropriate TaskActionService for a TaskStatus."""

    def __init__(self, ctx: ServiceContext):
        # Imported here to avoid circular imports
        from sprintbaton.services.classification import TaskClassificationService
        from sprintbaton.services.conflict_resolution import TaskConflictResolutionService
        from sprintbaton.services.execution import TaskExecutionService
        from sprintbaton.services.finalization import TaskFinalizationService
        from sprintbaton.services.metadata_generation import (
            TaskMetadataGenerationService,
            TaskProjectMetadataGenerationService,
        )
        from sprintbaton.services.passing_criteria import TaskPassingCriteriaService
        from sprintbaton.services.plan_classification import TaskPlanClassificationService
        from sprintbaton.services.planning import TaskPlanningService
        from sprintbaton.services.repo_scoping import TaskRepoScopingService
        from sprintbaton.services.review import TaskReviewService
        from sprintbaton.services.revision_classification import (
            TaskRevisionClassificationService,
        )
        from sprintbaton.services.spec_classification import TaskSpecClassificationService

        self.classification = TaskClassificationService(ctx)
        self.finalization = TaskFinalizationService(ctx)
        self.passing_criteria = TaskPassingCriteriaService(ctx)
        self.spec_classification = TaskSpecClassificationService(ctx)
        self.planning = TaskPlanningService(ctx)
        self.plan_classification = TaskPlanClassificationService(ctx)
        self.execution = TaskExecutionService(ctx)
        self.review = TaskReviewService(ctx)
        # Deliberately not in _by_status: the role has no TaskStatus of its
        # own — only _resolve_conflict ever calls it (conflict-resolution
        # spec §3, §7.3).
        self.conflict_resolution = TaskConflictResolutionService(ctx)
        # No TaskStatus of its own — computed inside the InProgress-entry path
        # before the serial per-repo loop (multi-repo-project spec §7.3).
        self.repo_scoping = TaskRepoScopingService(ctx)
        # Run by the reconcile pass when a card's content changed (task-
        # revisions spec §7.1) — no TaskStatus of its own.
        self.revision_classification = TaskRevisionClassificationService(ctx)
        # The metadata init pass (project-initialization-task spec §8.3): run
        # only by the orchestrator's _initialize_project for a
        # ProjectInitialization task — never in _by_status.
        self.metadata_generation = TaskMetadataGenerationService(ctx)
        self.project_metadata_generation = TaskProjectMetadataGenerationService(ctx)

        self._by_status: dict[TaskStatus, TaskActionService] = {
            TaskStatus.TaskPending: self.classification,
            TaskStatus.TaskFinalization: self.finalization,
            TaskStatus.PassingCriteria: self.passing_criteria,
            TaskStatus.TaskFinalized: self.spec_classification,
            TaskStatus.PlanFinalization: self.planning,
            TaskStatus.PlanFinalized: self.plan_classification,
            TaskStatus.InProgress: self.execution,
            TaskStatus.CodeReview: self.review,
            TaskStatus.InReview: self.review,
        }

    def for_status(self, status: TaskStatus) -> TaskActionService:
        service = self._by_status.get(status)
        if service is None:
            raise KeyError(f"no action service for status {status}")
        return service

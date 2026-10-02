from typing import Literal

from pydantic import BaseModel, Field, model_validator

from sprintbaton.entities.base import LOCAL_USER_ID, DescriptionEntity
from sprintbaton.entities.task_revision import AmendmentContext, PriorRoundContext
from sprintbaton.entities.enums import (
    EscalationTier,
    InitializationTrigger,
    MetadataScope,
    RepoWorkStatus,
    TaskKind,
    TaskStatus,
    TaskType,
)


class Comment(DescriptionEntity):
    taskId: str = ""
    authorUserId: str = ""
    externalId: str | None = None  # provider-side comment id


class RepoWork(BaseModel):
    """Per-repo execution state for a task that spans several repositories
    (multi-repo-project spec §4.3). A single-repo project produces a
    one-element list, so this is the uniform path."""

    repoId: str
    status: RepoWorkStatus = RepoWorkStatus.Pending
    branchName: str | None = None
    prUrl: str | None = None
    escalationTier: EscalationTier | None = None
    reviewFailures: int = 0            # human PR-comment bounces (resets on tier change)
    aiReviewRounds: int = 0            # pre-PR Review-Agent bounces (resets on tier change)
    conflictResolutionRounds: int = 0  # PR-open-time merge-conflict bounces
    taskExecutionDiffAction: str | None = None  # last completed diff (CodeReview crash recovery)
    # The card revision execution last ran against (task-revisions spec §5.4)
    builtFromRevision: int | None = None


class MetadataRepoProgress(BaseModel):
    """One member repo's position in a ProjectInitialization run
    (project-initialization-task spec §4.1). Filled once, on the run's first
    process() pass, from the project's members at that moment."""

    repoId: str
    status: Literal["Pending", "Completed", "Skipped", "Failed"] = "Pending"
    validationRounds: int = 0      # contract-violation bounces this attempt
    continuationRounds: int = 0    # turn/time cut-off continuations this attempt
    revision: str | None = None    # set when this pass published
    error: str | None = None
    completedAt: int | None = None


# Per-repo statuses at or beyond which the repo's PR exists / work is done —
# used by derive_task_status (spec §7.5). NoOp/Merged/Blocked are terminal.
_REPO_ORDER = {
    RepoWorkStatus.Pending: 0,
    RepoWorkStatus.InProgress: 1,
    RepoWorkStatus.CodeReview: 2,
    RepoWorkStatus.PrOpen: 3,
    RepoWorkStatus.NoOp: 4,
    RepoWorkStatus.Merged: 4,
    RepoWorkStatus.Blocked: 4,
}


class Task(DescriptionEntity):
    # Tenant owner (user-multitenancy spec §6); LOCAL_USER_ID in CLI mode
    userId: str = LOCAL_USER_ID
    # The owning Project (multi-repo-project spec §4.3; was repoId).
    projectId: str = ""
    externalId: str = ""  # provider-side task id
    boardId: str = ""
    priority: int | None = None
    labels: list[str] | None = None
    attachments: list[str] | None = None  # URLs attached to this task
    comments: list[Comment] | None = None
    status: TaskStatus = TaskStatus.TaskPending
    type: TaskType | None = None
    # Orthogonal to type (classification-taxonomy spec §3.2); set by the
    # Router's importanceFlags or the DiscoveredImportance hard trigger;
    # never cleared.
    important: bool = False
    childTaskIds: list[str] | None = None
    # URLs to object-storage files with the detailed description / plan
    taskFinalizationAction: str | None = None
    taskPassingCriteriaAction: str | None = None  # exhaustive acceptance criteria
    taskPlanningAction: str | None = None

    # --- multi-repo execution state (multi-repo-project spec §4.3) ---
    # The repos this task touches (set once by repo scoping, §7.3); None until
    # scoped. repoWork carries one entry per affected repo.
    affectedRepoIds: list[str] | None = None
    repoWork: list[RepoWork] = Field(default_factory=list)

    # Runtime execution state (orchestrator-owned)
    # In-flight claim marker (zero-infra-storage spec §4.3): stamped when
    # TaskOrchestrator.process() picks the task up, cleared when it returns.
    processingClaimedAt: int | None = None
    # Project-level escalation for the pre-execution phases; per-repo execution
    # tier lives on RepoWork.escalationTier.
    escalationTier: EscalationTier | None = None
    clarifyRounds: int = 0
    tokensSpent: int = 0
    # Every Conversation (clarification episode) ever created for this task
    conversationIds: list[str] = Field(default_factory=list)
    # Usage-limit pause state (usage-limit-aware execution spec §4.3).
    usageLimitPaused: bool = False
    usageLimitPausedUntil: int | None = None   # epoch millis
    usageLimitScope: str | None = None
    releaseId: str | None = None  # release window this task was swept into
    mandatoryHumanReview: bool = False  # set whenever `important` becomes True

    # --- project initialization (project-initialization-task spec §4.1) ---
    # Every field below is inert for a Development task. An init task has
    # externalId/boardId "" and never touches a board.
    kind: TaskKind = TaskKind.Development
    metadataScope: MetadataScope | None = None
    initializationTrigger: InitializationTrigger | None = None
    # Snapshot of Project.metadataGuidance at creation, so editing the project
    # mid-run never changes a running pass.
    metadataGuidance: str | None = None
    metadataRepoProgress: list[MetadataRepoProgress] = Field(default_factory=list)
    projectPassRevision: str | None = None
    # Transient-failure retry state (§5.6): consecutive failures, the epoch-
    # millis the polling lane may rediscover the row at, and the last error.
    transientFailures: int = 0
    retryAfter: int | None = None
    lastTransientError: str | None = None
    # The permanent failure reason — there is no card to comment on (§5.7).
    initializationError: str | None = None

    # --- revisions & board-driven workflow (task-revisions spec §5.1) ---
    # The card revision the workflow has *accepted*. title/description always
    # equal that revision's content (invariant 2).
    currentRevision: int = 1
    # action -> revision its current artifact was built from (finalization,
    # abstract_finalization, passing_criteria, planning).
    artifactRevisions: dict[str, int] = Field(default_factory=dict)
    # The column SprintBaton itself last put the card in (§8.1) — a snapshot
    # column that differs from it (and from the status's own column) is a
    # human move.
    lastSyncedColumnId: str | None = None
    # When SprintBaton last moved the card (epoch millis). A snapshot observed
    # before it predates our own move — the poller may have caught the card
    # mid-process() in an intermediate column — so it says nothing about
    # where the human wants the card.
    lastSyncedAt: int | None = None
    parkedColumnId: str | None = None   # set while parked (§6)
    # A revision awaiting the human's late-edit decision (§8.6), with the
    # classifier's recommended rewind point.
    pendingRevisionDecision: int | None = None
    pendingRevisionRewind: str | None = None
    revisionDecisionRounds: int = 0
    # The last revision a "this round is merged" notice was posted for (§7.2),
    # so it is posted once, not every poll cycle.
    revisionNoticeSent: int = 0
    # While set, SprintBaton does not move the card until the task reaches
    # this status (§8.2 "derived silently"): the human's placement is
    # respected while prerequisites run.
    cardHoldStatus: TaskStatus | None = None
    # Stage action -> why it is rerunning (§7.6); consumed by the stage when
    # it writes its artifact.
    amendments: dict[str, AmendmentContext] = Field(default_factory=dict)
    # Rounds (§9): one Task row per pass of work on a card.
    round: int = 1
    previousTaskId: str | None = None
    isCurrentRound: bool = True
    priorRound: PriorRoundContext | None = None

    @model_validator(mode="before")
    @classmethod
    def _shim_legacy_fields(cls, data):
        if not isinstance(data, dict):
            return data
        # Legacy category shim (classification-taxonomy spec §11)
        if data.get("type") == "Important":
            data = {**data, "type": TaskType.Complex, "important": True}
        # Legacy repoId -> projectId (multi-repo-project spec §14): tasks
        # persisted before the Project split, and tests that still construct
        # with repoId=, map onto projectId.
        if "repoId" in data and "projectId" not in data:
            data = {**data, "projectId": data["repoId"]}
            data.pop("repoId", None)
        return data

    @property
    def repoId(self) -> str:
        """Back-compat read alias — analytics/provenance still key on this
        name; it now means the owning project id (multi-repo-project spec §14)."""
        return self.projectId

    # --- per-repo work helpers (spec §7) ---

    def work_for(self, repo_id: str) -> RepoWork | None:
        for w in self.repoWork:
            if w.repoId == repo_id:
                return w
        return None


def derive_task_status(works: list[RepoWork]) -> TaskStatus:
    """The single card column, derived from the *slowest* (least-advanced)
    repo (multi-repo-project spec §7.5). Blocked wins (a human is needed);
    otherwise the card sits at the minimum repo stage."""
    if not works:
        return TaskStatus.InProgress
    if any(w.status == RepoWorkStatus.Blocked for w in works):
        return TaskStatus.Blocked
    if all(w.status in (RepoWorkStatus.Merged, RepoWorkStatus.NoOp) for w in works):
        return TaskStatus.QA  # release-ready; the release window sweeps it
    lowest = min(_REPO_ORDER[w.status] for w in works)
    if lowest <= _REPO_ORDER[RepoWorkStatus.InProgress]:
        return TaskStatus.InProgress
    if lowest == _REPO_ORDER[RepoWorkStatus.CodeReview]:
        return TaskStatus.CodeReview
    return TaskStatus.InReview

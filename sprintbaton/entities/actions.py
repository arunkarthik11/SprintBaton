"""TaskActionRequest / TaskActionResponse hierarchy (docs/entities.md)."""

from pydantic import Field, model_validator

from sprintbaton.entities.base import BaseEntity
from sprintbaton.entities.clarification import ClarificationOptions
from sprintbaton.entities.enums import (
    PlanClassificationVerdict,
    RewindPoint,
    SpecClassificationVerdict,
    TaskType,
)
from sprintbaton.entities.task_revision import AmendmentContext, PriorRoundContext
from sprintbaton.entities.usage import TokenUsage
from sprintbaton.harness.base import UsageLimitSignal


# --- Requests ---------------------------------------------------------------

class TaskActionRequest(BaseEntity):
    taskId: str = ""
    # The task's owner (Task.userId): keys the task's sandbox session and its
    # per-tenant cache, and binds every run token to that owner
    # (hosted-sandbox-isolation spec §6.1, §8.2). Never a credential.
    userId: str = ""
    promptId: str = ""
    agentId: str = ""
    # The id of whatever entity scopes this request: a Project for the seven
    # project-scoped roles, a Repository for the three per-repo ones
    # (execution/review/conflict_resolution). Named repoId for historical
    # reasons — it predates the Project split (multi-repo-project spec §4) —
    # and read by nothing today; treat it as provenance, not as a repo handle.
    repoId: str = ""
    taskTitle: str = ""
    taskDescription: str = ""
    # The project context rendered into the prompt: a role-specific "Where
    # things are on disk" guide over the combined project index
    # (TaskActionService.metadata_summary; storage-layout spec §10 q4).
    metadataSummary: str = ""
    # Human-clarification continuity (conversation-lifecycle spec §4.3):
    # conversationId/replyText are set together on a resume attempt;
    # clarificationContext carries the rendered prior Q&A on the fresh/restart
    # branch. Exactly one of replyText/clarificationContext is non-None, or
    # both are None on a task's very first turn for this action.
    conversationId: str | None = None
    replyText: str | None = None
    clarificationContext: str | None = None
    # Best-effort conveniences derived from replyText when the paused turn
    # offered structured options (clarification-options spec §4.4): the
    # matched Answer.option string(s), or None when the reply didn't map onto
    # an offered option — replyText stays the always-populated authority.
    selectedAnswer: str | None = None
    selectedAnswers: list[str] | None = None
    # Set when this stage reruns over its own previous output because of a
    # card edit or a backward move (task-revisions spec §7.6); adopted by
    # finalization, abstract_finalization, passing_criteria, planning and
    # execution.
    amendment: AmendmentContext | None = None
    # What the previous round of this card did, when there was one (§9.3).
    priorRound: PriorRoundContext | None = None


class TaskClassificationRequest(TaskActionRequest):
    pass


class TaskFinalizationRequest(TaskActionRequest):
    priorQuestionsAndAnswers: str = ""  # rendered comment thread
    workspacePath: str = ""  # read-only clone; empty on single_shot (§9.6)


class TaskSpecClassificationRequest(TaskActionRequest):
    finalizedSpec: str = ""
    # Importance context for the classifier (classification-taxonomy spec §11):
    # the orchestrator floors Simple -> Compound regardless, but a model that
    # knows the task is important may independently reach Complex (plan first),
    # which the floor cannot produce on its own.
    important: bool = False


class TaskPassingCriteriaRequest(TaskActionRequest):
    specText: str = ""  # the finalized spec, or title+description when none exists


class TaskPlanningRequest(TaskActionRequest):
    situationReport: str | None = None  # set on E2 replanning escalation
    # The artifacts the plan must implement and satisfy — empty when the task
    # skipped clarification / before criteria exist.
    finalizedSpec: str = ""
    passingCriteria: str = ""
    workspacePath: str = ""  # read-only clone; empty on single_shot (§9.6)


class TaskPlanClassificationRequest(TaskActionRequest):
    finalizedSpec: str = ""  # empty when the task skipped clarification
    plan: str = ""
    important: bool = False  # same importance context as the spec checkpoint


class TaskRevisionClassificationRequest(TaskActionRequest):
    """A card's content changed after SprintBaton derived work from it
    (task-revisions spec §7.3): name the earliest stage the edit invalidates.
    taskTitle/taskDescription carry the NEW content."""
    previousTitle: str = ""
    previousDescription: str = ""
    diff: str = ""                    # unified diff of the two revisions
    currentStatus: str = ""
    taskType: str = ""
    important: bool = False
    # The current artifacts, truncated, each with its revision stamp
    finalizedSpec: str = ""
    passingCriteria: str = ""
    plan: str = ""
    repoSummary: str = ""             # per repo: branch exists? PR open?
    openQuestion: str | None = None   # a clarification pending when the edit landed
    guidance: str | None = None       # the human's free-text reply to a decision


class TaskRepoScopingRequest(TaskActionRequest):
    """Which member repos a multi-repo task touches (multi-repo-project spec
    §7.3). Reads the spec/plan (or raw description) + the candidate repos."""
    specText: str = ""
    plan: str = ""
    # [{"repoId", "title", "role"}] — the candidate member repos to pick from
    candidateRepos: list[dict] = Field(default_factory=list)


class TaskExecutionRequest(TaskActionRequest):
    workspacePath: str = ""
    plan: str | None = None
    spec: str | None = None
    passingCriteria: str | None = None  # from Task.taskPassingCriteriaAction
    situationReport: str | None = None  # set on E0 clean-context retries
    modelId: str = ""                   # tier-parameterised (Sonnet default, Opus on E3)


class TaskReviewRequest(TaskActionRequest):
    workspacePath: str = ""
    diff: str = ""
    passingCriteria: str | None = None  # from Task.taskPassingCriteriaAction


class TaskConflictResolutionRequest(TaskActionRequest):
    workspacePath: str = ""
    conflictedFiles: list[str] = Field(default_factory=list)
    executionSummary: str = ""          # what the task's own diff was trying to do
    passingCriteria: str | None = None  # from Task.taskPassingCriteriaAction
    situationReport: str | None = None  # prior attempt's stated reason, on a bounce retry


class MetadataGenerationRequest(TaskActionRequest):
    """One per-repo metadata init-pass run (project-initialization-task spec
    §8.1). The agent explores the clone read-only and edits `writableRoot` —
    the clone's pre-seeded `.sprintbaton/` — in place."""
    workspacePath: str = ""       # the repository clone (read-only)
    writableRoot: str = ""        # <clone>/.sprintbaton — the one writable directory
    repoTitle: str = ""
    repoRole: str = ""
    locationGuide: str = ""       # the "Where things are on disk" section (§8.4)
    metadataGuidance: str | None = None   # Task.metadataGuidance snapshot
    situationReport: str | None = None    # validation violations or a continuation note
    continuation: bool = False    # the prior run was cut off by a turn/time limit
    maxTurns: int = 0


class ProjectMetadataGenerationRequest(TaskActionRequest):
    """The project-index init-pass run (spec §8.2): the working directory IS
    the writable project metadata directory; each member repo's current
    `info` travels in the prompt — this pass maps repos, it does not browse
    code."""
    workspacePath: str = ""       # == the writable project metadata directory
    projectTitle: str = ""
    repoSummaries: str = ""       # id/title/role/remote + current info (<= 4000 chars each)
    locationGuide: str = ""
    metadataGuidance: str | None = None
    situationReport: str | None = None
    continuation: bool = False
    maxTurns: int = 0


# --- Responses ---------------------------------------------------------------

class TaskActionResponse(BaseEntity):
    taskId: str = ""
    usage: TokenUsage = Field(default_factory=TokenUsage)
    modelId: str = ""   # model that produced this response (analytics dimension)
    promptId: str = ""  # versioned prompt id, e.g. "execution@v1"
    # This turn's harness session id, if the harness gave one (resume handle)
    conversationId: str | None = None
    # Non-null => the role pauses in place and asks the human via a task
    # comment (conversation-lifecycle spec §4.3); every other field on the
    # response must then be treated as incomplete/ignorable.
    clarificationQuestion: str | None = None
    # Optional structured options offered alongside the question
    # (clarification-options spec §4.3) — only ever set together with it.
    clarificationOptions: ClarificationOptions | None = None
    # Non-empty => a harness detected a usage/rate/budget constraint this
    # turn (usage-limit-aware execution spec §5.1). Orthogonal to
    # clarificationQuestion — a role only ever sets one of the two on a given
    # turn (the harness call either completed enough to ask a question, or it
    # didn't run/complete at all because of the constraint).
    usageLimitSignals: list[UsageLimitSignal] = Field(default_factory=list)
    # False => this turn's harness ran WITHOUT guard.py enforcement, because a
    # hook-capability probe established that the CLI does not execute our hook
    # (subprocess-cli-write-parity spec §7.3). The run proceeds by explicit
    # product decision and warns every time; this flag is what makes "was this
    # diff produced under a guard?" answerable from the event stream afterwards
    # rather than inferred from logs. True everywhere else, so nothing changes
    # for any existing role.
    guardrailEnforced: bool = True

    @model_validator(mode="after")
    def _options_require_question(self) -> "TaskActionResponse":
        if self.clarificationOptions is not None and self.clarificationQuestion is None:
            raise ValueError(
                "clarificationOptions without a clarificationQuestion is "
                "meaningless (clarification-options spec §4.3)")
        return self

    @property
    def inputTokens(self) -> int:
        return self.usage.inputTokens

    @property
    def outputTokens(self) -> int:
        return self.usage.outputTokens

    @property
    def tokensUsed(self) -> int:
        return self.usage.totalTokens


class TaskClassificationResponse(TaskActionResponse):
    category: TaskType = TaskType.Simple
    rationale: str = ""
    importanceFlags: list[str] = Field(default_factory=list)  # auth/payments/migration/core


class TaskFinalizationResponse(TaskActionResponse):
    questions: list[str] = Field(default_factory=list)
    finalizedSpec: str | None = None  # set once answers are sufficient


class TaskSpecClassificationResponse(TaskActionResponse):
    # Safer branch is the default: always plan rather than guess a model tier
    verdict: SpecClassificationVerdict = SpecClassificationVerdict.Complex
    rationale: str = ""


class TaskPassingCriteriaResponse(TaskActionResponse):
    criteria: list[str] = Field(default_factory=list)  # individually-checkable statements
    rationale: str = ""


class TaskPlanningResponse(TaskActionResponse):
    plan: str = ""


class TaskPlanClassificationResponse(TaskActionResponse):
    # Safer branch is the default: over-tier rather than stall
    verdict: PlanClassificationVerdict = PlanClassificationVerdict.Compound
    rationale: str = ""


class TaskRevisionClassificationResponse(TaskActionResponse):
    # Malformed-output fallback is Classification (redo everything) — the
    # safe, expensive branch (spec §7.4)
    rewindTo: RewindPoint = RewindPoint.Classification
    changeSummary: str = ""   # one line, used verbatim in the card comment
    rationale: str = ""


class TaskRepoScopingResponse(TaskActionResponse):
    affectedRepoIds: list[str] = Field(default_factory=list)
    rationale: str = ""


class TaskExecutionResponse(TaskActionResponse):
    summary: str = ""
    diff: str = ""
    completed: bool = False
    askedQuestion: str | None = None      # discovered-ambiguity signal (§3 — distinct
                                          # from the base clarificationQuestion)
    planBroken: bool = False              # plan-breakage signal
    importanceFlags: list[str] = Field(default_factory=list)
    filesEdited: dict[str, int] = Field(default_factory=dict)  # path -> edit count
    consecutiveCheckFailures: int = 0


class TaskReviewResponse(TaskActionResponse):
    approved: bool = False
    findings: list[str] = Field(default_factory=list)


class TaskConflictResolutionResponse(TaskActionResponse):
    # No `diff` field — the "diff" here is a merge commit, never handed to a
    # downstream reviewer separately (conflict-resolution spec §7.1, §9)
    resolved: bool = False
    summary: str = ""
    filesEdited: dict[str, int] = Field(default_factory=dict)


class MetadataGenerationResponse(TaskActionResponse):
    """The files the agent edited are the output; this carries only run
    metadata (METADATA_RUN_SCHEMA, spec §8.3)."""
    summary: str = ""
    removedFiles: list[str] = Field(default_factory=list)
    # completed | turn_limit | time_limit | error (HarnessResult.stop_reason)
    stopReason: str = "completed"


class ProjectMetadataGenerationResponse(MetadataGenerationResponse):
    pass

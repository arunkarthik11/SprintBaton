"""Project — the todolist-board unit that groups one or more git repositories
(multi-repo-project spec §4.1). Absorbs the board-level fields that used to live
on Repository (columns, cadence, provider, board id) now that a single board can
drive changes across N repos. A single-repo project is the same shape with one
member Repository — there is no separate simple mode."""

from pydantic import BaseModel

from sprintbaton.entities.base import LOCAL_USER_ID, DescriptionEntity


class ColumnConfig(BaseModel):
    """Provider column ids mapped to SprintBaton semantic states. Lives on the
    Project (the board), not the Repository (multi-repo-project spec §4.1)."""

    icebox: str
    task_finalization: str
    passing_criteria: str    # acceptance-criteria checkpoint column
    task_finalized: str      # spec classification checkpoint column
    plan_finalization: str
    plan_finalized: str      # plan classification checkpoint column
    in_progress: str
    code_review: str         # pre-PR Review Agent gate column
    in_review: str
    qa: str
    shipped: str
    blocked: str | None = None  # EH halts; falls back to task_finalization + label


def column_to_status(columns: ColumnConfig, column_id: str | None):
    """The stage a board column means, or None for a column that maps to no
    stage (task-revisions spec §6/§8.8). There is deliberately no default:
    an unmapped column *parks* a card instead of silently restarting it from
    TaskPending. Called only by first-time creation in the poller and by the
    orchestrator's reconcile pass."""
    from sprintbaton.entities.enums import TaskStatus

    mapping = {
        columns.icebox: TaskStatus.TaskPending,
        columns.task_finalization: TaskStatus.TaskFinalization,
        columns.passing_criteria: TaskStatus.PassingCriteria,
        columns.task_finalized: TaskStatus.TaskFinalized,
        columns.plan_finalization: TaskStatus.PlanFinalization,
        columns.plan_finalized: TaskStatus.PlanFinalized,
        columns.in_progress: TaskStatus.InProgress,
        columns.code_review: TaskStatus.CodeReview,
        columns.in_review: TaskStatus.InReview,
        columns.qa: TaskStatus.QA,
        columns.shipped: TaskStatus.Shipped,
    }
    if columns.blocked:
        mapping[columns.blocked] = TaskStatus.Blocked
    return mapping.get(column_id) if column_id else None


def status_to_column(columns: ColumnConfig, status) -> str | None:
    """The column a status lives in (the inverse of column_to_status). A
    Blocked task without a dedicated Blocked column sits in Task Finalization
    (the orchestrator's `_park_blocked` fallback)."""
    from sprintbaton.entities.enums import TaskStatus

    return {
        TaskStatus.TaskPending: columns.icebox,
        TaskStatus.TaskFinalization: columns.task_finalization,
        TaskStatus.PassingCriteria: columns.passing_criteria,
        TaskStatus.TaskFinalized: columns.task_finalized,
        TaskStatus.PlanFinalization: columns.plan_finalization,
        TaskStatus.PlanFinalized: columns.plan_finalized,
        TaskStatus.InProgress: columns.in_progress,
        TaskStatus.CodeReview: columns.code_review,
        TaskStatus.InReview: columns.in_review,
        TaskStatus.QA: columns.qa,
        TaskStatus.Shipped: columns.shipped,
        TaskStatus.Blocked: columns.blocked or columns.task_finalization,
    }.get(status)


# (ColumnConfig field, human-readable section name) for provisioned template
# boards (repository-onboarding spec §12.2) — the single source of truth for
# the createTodolistProject flow and any future WebUI default.
TEMPLATE_SECTIONS: list[tuple[str, str]] = [
    ("icebox", "Icebox"),
    ("task_finalization", "Task Finalization"),
    ("passing_criteria", "Passing Criteria"),
    ("task_finalized", "Task Finalized"),
    ("plan_finalization", "Plan Finalization"),
    ("plan_finalized", "Plan Finalized"),
    ("in_progress", "In Progress"),
    ("code_review", "Code Review"),
    ("in_review", "In Review"),
    ("qa", "QA"),
    ("shipped", "Shipped"),
    ("blocked", "Blocked"),
]


class Project(DescriptionEntity):
    """A todolist board mapped to N git repositories (multi-repo-project spec §4.1)."""

    # Tenant owner (user-multitenancy spec §6); LOCAL_USER_ID in CLI mode
    userId: str = LOCAL_USER_ID
    boardId: str = ""
    # Per-project (a board lives on one provider); repos are git-only now.
    todolistProvider: str = "todoist"
    # Required, no default: a board that can't map every semantic state is a
    # configuration error (repository-onboarding spec §3.3).
    columns: ColumnConfig
    # Release-window cadence for the dev -> staging cutover.
    releaseCadenceDays: int = 7
    # Intake toggle: False pauses polling for this project only.
    active: bool = True
    todolistCredentialId: str | None = None
    # Git identity SprintBaton commits under for every member repo (tier 2 of
    # storage-layout-and-git-identity spec §5.2). None = fall through to the
    # deployment Settings default, then the built-in constants.
    gitAuthorName: str | None = None
    gitAuthorEmail: str | None = None

    # Combined project-level metadata index (multi-repo-project spec §5.2): the
    # info file that names each member repo, its role, and where its own
    # .sprintbaton/ metadata lives, so agents navigate project -> repo.
    # Always written in the same save as metadataRevision: the current
    # revision's `info` URL (project-initialization-task spec §4.3).
    metadataUrl: str | None = None
    # Manifest opt-out: False = never auto-generate metadata, never gate board
    # tasks on it (spec §4.3, §5.4).
    generateMetadata: bool = True
    # Optional operator guidance passed to both init agents (spec §8.4).
    metadataGuidance: str | None = None
    # The pointer: the init task id whose immutable revision is current (§9).
    metadataRevision: str | None = None
    # Stamped by the first successful init run, never cleared — what opens
    # the TaskPending gate (§5.4).
    metadataInitializedAt: int | None = None
    # Operational, never in the manifest (build_project only assigns spec
    # fields, so a re-apply preserves it): lets board tasks run before the
    # first successful init. Cleared automatically by that first success.
    metadataGateOverride: bool = False

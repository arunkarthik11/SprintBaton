"""Card moves SprintBaton makes itself (task-revisions spec §8.1).

Every column change SprintBaton makes goes through `move_card` (invariant 6),
which records `Task.lastSyncedColumnId` and saves the task at once — that is
what lets the reconcile pass tell a human move from our own. It also honours
`Task.cardHoldStatus` (§8.2): while prerequisite stages run "silently" for a
human's forward move, the card stays where the human put it.
"""

import logging

from sprintbaton.entities.base import now_millis
from sprintbaton.entities.enums import TaskStatus
from sprintbaton.entities.project import Project, column_to_status
from sprintbaton.entities.task import Task

log = logging.getLogger(__name__)

# Pipeline position of each board status (task-revisions spec §3/§8.2):
# comparing two ranks is how a human move is classified as forward or backward.
STATUS_RANK: dict[TaskStatus, int] = {
    TaskStatus.TaskPending: 0,
    TaskStatus.TaskFinalization: 1,
    TaskStatus.PassingCriteria: 2,
    TaskStatus.TaskFinalized: 3,
    TaskStatus.PlanFinalization: 4,
    TaskStatus.PlanFinalized: 5,
    TaskStatus.InProgress: 6,
    TaskStatus.CodeReview: 7,
    TaskStatus.InReview: 8,
    TaskStatus.QA: 9,
    TaskStatus.Shipped: 10,
}


def move_card(ctx, task: Task, project: Project, column: str, *,
              force: bool = False) -> bool:
    """Move the card to `column` unless a hold suppresses it. `force` is for
    moves that must be visible whatever the hold says (parking to Blocked).
    Returns whether the card is now in `column`."""
    hold = task.cardHoldStatus
    if hold is not None and not force:
        target = column_to_status(project.columns, column)
        # Held while the task is still short of the human's placement; a move
        # to (or past) it releases the hold — a hold must never outlive the
        # stage it was placed for, or the card would freeze there.
        if STATUS_RANK.get(target, -1) < STATUS_RANK.get(hold, -1):
            log.debug("card move held", extra={"task_id": task.id, "column": column,
                                               "hold": str(hold)})
            return False
        # Reached the stage the human placed the card at: release the hold.
        task.cardHoldStatus = None
        if column == task.lastSyncedColumnId:
            _save(ctx, task)
            return True
    task.cardHoldStatus = None
    ctx.task_adapter_for(project).move_task(task.externalId, column)
    task.lastSyncedColumnId = column
    task.lastSyncedAt = now_millis()
    _save(ctx, task)
    return True


def _save(ctx, task: Task) -> None:
    repo = getattr(ctx, "task_repo", None)
    if repo is not None:
        task.touch()
        repo.save(task)

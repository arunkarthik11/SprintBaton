"""The reconcile pass: turning what the poller observed into workflow decisions
(docs/task-revisions-and-board-driven-workflow-spec.md §4, §6-§10).

The poller only records observations (a `CardSnapshot` and, on a content
change, a `TaskRevision`). At the start of every `process()`, inside the claim
bracket, `_reconcile_card` compares them with the task's own state and applies
the precedence of §8.7:

1. **Reopen** — a human moved a Shipped card back: start a new round (§9).
2. **Park / unpark** — the card sits in a column that maps to no stage, or a
   human moved it into Blocked: hold it, state untouched (§6).
3. **Human move** — the target column's stage is where the task continues,
   forward (deriving only strict prerequisites, silently) or backward (a
   rewind) (§8.2-§8.5).
4. **Content change** — the Revision Classification role names a rewind
   point; applied automatically before execution, asked about after (§7, §8.6).
5. Neither — dispatch as before.

A mixin over TaskOrchestrator: it drives the same stage methods the dispatch
arms use, so a rewind is nothing more than clearing superseded state and
setting the status the next dispatch acts on.
"""

from __future__ import annotations

import logging

from sprintbaton.entities.clarification import Answer, ClarificationOptions
from sprintbaton.entities.enums import (
    EscalationTier,
    RepoWorkStatus,
    RewindPoint,
    TaskStatus,
    TaskType,
)
from sprintbaton.entities.project import Project, column_to_status, status_to_column
from sprintbaton.entities.repository import Repository
from sprintbaton.entities.task import Task
from sprintbaton.entities.task_revision import (
    AmendmentContext,
    CardSnapshot,
    PriorRoundContext,
    TaskRevision,
    content_fingerprint,
    latest_revision,
    revision_of,
)
from sprintbaton.orchestrator.board import STATUS_RANK
from sprintbaton.services.revision_classification import content_diff

log = logging.getLogger(__name__)

ACTION = "revision_classification"

# Rewind point -> (pipeline rank, the status the task re-enters at).
_REWIND = {
    RewindPoint.Classification: (0, TaskStatus.TaskPending),
    RewindPoint.Finalization: (1, TaskStatus.TaskFinalization),
    RewindPoint.PassingCriteria: (2, TaskStatus.PassingCriteria),
    RewindPoint.Planning: (4, TaskStatus.PlanFinalization),
    RewindPoint.Execution: (6, TaskStatus.InProgress),
}

# A backward board move to a status rewinds to the stage producing it.
_STATUS_REWIND = {
    TaskStatus.TaskPending: RewindPoint.Classification,
    TaskStatus.TaskFinalization: RewindPoint.Finalization,
    TaskStatus.PassingCriteria: RewindPoint.PassingCriteria,
    TaskStatus.PlanFinalization: RewindPoint.Planning,
    TaskStatus.InProgress: RewindPoint.Execution,
    TaskStatus.CodeReview: RewindPoint.Execution,
    TaskStatus.InReview: RewindPoint.Execution,
}

# The statuses whose rewind reruns a classification checkpoint that produces
# no artifact of its own (rank between two producing stages).
_CHECKPOINT_STATUS = {TaskStatus.TaskFinalized: 3, TaskStatus.PlanFinalized: 5}

# The next-earlier rewind point, for clamping.
_EARLIER = {
    RewindPoint.Execution: RewindPoint.Planning,
    RewindPoint.Planning: RewindPoint.PassingCriteria,
    RewindPoint.PassingCriteria: RewindPoint.Finalization,
    RewindPoint.Finalization: RewindPoint.Classification,
}

_POST_EXECUTION = (TaskStatus.InProgress, TaskStatus.CodeReview, TaskStatus.InReview)

OPTION_REDO = "Redo from {stage}"
OPTION_AMEND = "Amend the existing code only"
OPTION_KEEP = "Keep going with the current plan"


class CardReconcileMixin:
    """Mixed into TaskOrchestrator. Every method assumes `self.ctx` /
    `self.factory` and the orchestrator's stage methods."""

    # ------------------------------------------------------------------ entry

    def _reconcile_card(self, task: Task, project: Project,
                        repos: list[Repository]) -> bool:
        """Apply what the poller observed. Returns True when process() should
        go on to dispatch on `task.status`, False when it must stop (parked,
        paused on a decision, a new round started, or the reconcile already
        drove the work itself)."""
        snapshots = getattr(self.ctx, "snapshot_repo", None)
        snap = snapshots.get(task.id) if snapshots is not None else None
        if snap is None:
            return True
        # Not content: refreshed on every reconcile, never a revision (§5.2)
        # — which is how a label added later reaches the usage-limit policy.
        task.labels = snap.labels
        task.priority = snap.priority

        column = snap.columnId
        if task.lastSyncedAt is not None and snap.observedAt <= task.lastSyncedAt:
            # Observed before our own latest move (§8.1): the poller caught the
            # card mid-process() — not evidence of anything the human did.
            column = task.lastSyncedColumnId
        target = column_to_status(project.columns, column) if column else None
        revisions = getattr(self.ctx, "revision_repo", None)
        latest = latest_revision(revisions, task) if revisions is not None else None
        pending = latest if latest is not None and latest.revision > task.currentRevision \
            else None
        # §8.1: a column we did not put the card in, which also isn't the
        # column its status already lives in (that second clause absorbs a
        # crash between a move and its save, and a late-reported move).
        human_move = (bool(column) and column != task.lastSyncedColumnId
                      and target != task.status)

        # 1. Reopen (§9) — and a merged round never changes (invariant 8).
        if task.status == TaskStatus.Shipped:
            if human_move and target not in (None, TaskStatus.Shipped):
                self._start_new_round(task, project, snap, target)
            elif pending is not None:
                self._merged_revision_notice(task, project, pending)
            return False

        # 2. Park / unpark (§6)
        if target is None or (human_move and target == TaskStatus.Blocked):
            if task.parkedColumnId != column:
                task.parkedColumnId = column
                log.info("task parked", extra={"event": "task_parked",
                                               "column": column, "status": str(task.status)})
            return False
        if task.parkedColumnId is not None:
            task.parkedColumnId = None
            log.info("task unparked", extra={"event": "task_unparked", "column": column})

        if task.status == TaskStatus.QA:
            if human_move:
                self._move_out_of_qa(task, project, repos, target)
                return False
            if pending is not None:
                self._merged_revision_notice(task, project, pending)
            return True

        # 3. Human move (§8.2-§8.5, with §8.7's combined edit)
        if human_move:
            return self._apply_human_move(task, project, repos, target, column, pending)

        # 4. Content change (§7, §8.6)
        if pending is not None:
            return self._apply_revision(task, project, repos, pending)
        return True

    # --------------------------------------------------------------- helpers

    def _comment(self, task: Task, project: Project, text: str) -> None:
        self.ctx.task_adapter_for(project).add_comment(
            task.externalId, self.ctx.translator.render_revision_notice(text))

    def _current_revision(self, task: Task) -> TaskRevision:
        revisions = getattr(self.ctx, "revision_repo", None)
        found = revision_of(revisions, task, task.currentRevision) if revisions else None
        return found or TaskRevision(
            userId=task.userId, taskId=task.id, revision=task.currentRevision,
            title=task.title, description=task.description,
            fingerprint=content_fingerprint(task.title, task.description))

    def _accept_revision(self, task: Task, revision: TaskRevision, *,
                         restamp: bool) -> None:
        """Make `revision` the one the workflow runs on (invariant 2). With
        `restamp`, every current artifact is judged compatible with it — the
        classifier's (or the human's) NoChange (§7.4)."""
        task.currentRevision = revision.revision
        task.title = revision.title
        task.description = revision.description
        task.pendingRevisionDecision = None
        task.pendingRevisionRewind = None
        task.revisionDecisionRounds = 0
        if restamp:
            for action in task.artifactRevisions:
                task.artifactRevisions[action] = revision.revision
            for work in task.repoWork:
                if work.builtFromRevision is not None:
                    work.builtFromRevision = revision.revision

    def _merged_revision_notice(self, task: Task, project: Project,
                                revision: TaskRevision) -> None:
        """An edit on a merged round is recorded but never accepted (§7.2)."""
        if task.revisionNoticeSent >= revision.revision:
            return
        task.revisionNoticeSent = revision.revision
        self._comment(task, project,
                      "The card was edited, but this round's code is already merged, "
                      "so nothing was changed. Once it ships, move the card back "
                      "to start a new round with the new description.")

    @staticmethod
    def _has_code(task: Task) -> bool:
        return any(w.branchName for w in task.repoWork)

    def _progress_rank(self, task: Task) -> int:
        """Where a task is in the pipeline. A Blocked task keeps no stage of
        its own, so its rank comes from what it has produced."""
        if task.status != TaskStatus.Blocked:
            return STATUS_RANK.get(task.status, 0)
        if self._has_code(task):
            return STATUS_RANK[TaskStatus.InProgress]
        if task.taskPlanningAction:
            return STATUS_RANK[TaskStatus.PlanFinalized]
        if task.taskPassingCriteriaAction:
            return STATUS_RANK[TaskStatus.PassingCriteria]
        if task.taskFinalizationAction:
            return STATUS_RANK[TaskStatus.TaskFinalization]
        return 0

    def _applicable(self, task: Task, point: RewindPoint) -> RewindPoint:
        """Map a stage the task never went through to the next one downstream,
        then clamp to the task's current stage — a rewind point can never be
        later than where the task is (§7.4)."""
        if point == RewindPoint.NoChange:
            return point
        if point == RewindPoint.Finalization and task.type not in (
                TaskType.Ambiguous, TaskType.Abstract) and not task.taskFinalizationAction:
            point = RewindPoint.PassingCriteria
        if point == RewindPoint.Planning and not task.taskPlanningAction:
            point = RewindPoint.Execution
        current = self._progress_rank(task)
        while _REWIND[point][0] > current:
            point = _EARLIER[point]
        return point

    def _human_guidance(self, task: Task, project: Project) -> str:
        """Human comments posted since SprintBaton last spoke on the card — the
        explanation (if any) accompanying a backward move (§8.4)."""
        try:
            comments = self.ctx.task_adapter_for(project).list_comments(task.externalId)
        except Exception:
            return ""
        from sprintbaton.clarification.translator import AGENT_COMMENT_HEADERS

        tail: list[str] = []
        for comment in comments:
            if comment.body.startswith(AGENT_COMMENT_HEADERS):
                tail = []
            else:
                tail.append(comment.body)
        return "\n\n".join(tail)

    # ---------------------------------------------------------------- rewind

    def _rewind(self, task: Task, project: Project, *, rank: int, status: TaskStatus,
                reason: str, guidance: str = "",
                previous: TaskRevision | None = None,
                new: TaskRevision | None = None, move_card: bool = True) -> None:
        """Supersede everything from pipeline position `rank` on and re-enter
        at `status` (§8.4). Artifacts' revision-suffixed files stay in the blob
        store; only the Task.*Action pointers and their stamps are cleared.
        Branches and PRs are kept (§8.5, invariant 7)."""
        storage = self.ctx.object_storage
        conversations = getattr(self.ctx, "conversations", None)
        diff = content_diff(previous, new) if previous is not None and new is not None \
            else ""

        def amend(action: str, url: str | None) -> None:
            previous_artifact = (storage.get_text_by_url(url) or "") if url else ""
            task.amendments[action] = AmendmentContext(
                previousArtifact=previous_artifact,
                previousContent=_content(previous) if previous else "",
                newContent=_content(new) if new else "",
                diff=diff, reason=reason, guidance=guidance)

        def close(*actions: str) -> None:
            if conversations is None:
                return
            for action in actions:
                conversations.close_open(task, action, "rewind")

        spec_action = ("abstract_finalization" if task.type == TaskType.Abstract
                       else "finalization")
        # The target stage reruns in amend mode; classification never amends.
        if rank == 1:
            amend(spec_action, task.taskFinalizationAction)
        elif rank == 2:
            amend("passing_criteria", task.taskPassingCriteriaAction)
        elif rank == 4:
            amend("planning", task.taskPlanningAction)
        if rank <= 6 and self._has_code(task):
            # Code exists: re-execution continues on the existing branch
            # rather than starting over (§8.5).
            amend("execution", None)
            task.amendments["execution"].guidance = "\n".join(filter(None, [
                guidance,
                "The task's branch already holds work built from an earlier version "
                "of this card; change it where the new requirements differ, keep "
                "the rest."]))

        if rank <= 0:
            task.type = None
            task.escalationTier = None
            task.affectedRepoIds = None
            task.clarifyRounds = 0
            # `important` is monotonic within a round (§8.4): never lowered.
        if rank <= 1:
            task.taskFinalizationAction = None
            task.artifactRevisions.pop("finalization", None)
            task.artifactRevisions.pop("abstract_finalization", None)
            task.clarifyRounds = 0
            close("finalization", "abstract_finalization")
        if rank <= 2:
            task.taskPassingCriteriaAction = None
            task.artifactRevisions.pop("passing_criteria", None)
            close("passing_criteria")
        if rank <= 3:
            close("spec_classification")
        if rank <= 4:
            task.taskPlanningAction = None
            task.artifactRevisions.pop("planning", None)
            close("planning")
        if rank <= 5:
            close("plan_classification")
        if rank <= 6:
            for work in task.repoWork:
                if work.status == RepoWorkStatus.Merged:
                    continue
                work.status = RepoWorkStatus.Pending
                work.aiReviewRounds = 0
                work.reviewFailures = 0
                work.conflictResolutionRounds = 0
                work.taskExecutionDiffAction = None
            close("execution", "review", "conflict_resolution")
        # Tier is required downstream of classification; never de-escalated.
        if rank > 0 and task.escalationTier is None and task.type is not None:
            task.escalationTier = EscalationTier.E0
        # Stale amendments for stages that will not rerun are dropped.
        self._set_status(task, status)
        log.info("rewind", extra={"event": "rewind", "status": str(status),
                                  "reason": reason})
        if move_card:
            column = status_to_column(project.columns, status)
            if column:
                self._move_card(task, project, column)

    def _rewind_to(self, task: Task, project: Project, point: RewindPoint, **kw) -> None:
        rank, status = _REWIND[point]
        if point == RewindPoint.Finalization and task.type not in (
                TaskType.Ambiguous, TaskType.Abstract):
            # A non-clarifying task only reaches Finalization via a rewind;
            # the generic finalization agent runs it.
            status = TaskStatus.TaskFinalization
        self._rewind(task, project, rank=rank, status=status, **kw)

    # ------------------------------------------------------------- revisions

    def _run_revision_classifier(self, task: Task, project: Project,
                                 repos: list[Repository], revision: TaskRevision,
                                 guidance: str | None = None):
        from_status = task.status
        response = self.factory.revision_classification.run(
            task, project, repos, previous=self._current_revision(task), new=revision,
            guidance=guidance)
        task.tokensSpent += response.tokensUsed
        self.ctx.analytics.record(task, action=ACTION, from_status=from_status,
                                  response=response)
        self._record_note(task, project, ACTION,
                          f"revision {revision.revision}: {response.rewindTo} — "
                          f"{response.rationale}")
        self._record_classification_provenance(task, project, action=ACTION,
                                               response=response)
        return response

    def _apply_revision(self, task: Task, project: Project,
                        repos: list[Repository], revision: TaskRevision) -> bool:
        """A content change with no board move (§7.2, §8.6)."""
        if task.type is None:
            # Nothing derived from the old text yet: accept directly.
            self._accept_revision(task, revision, restamp=False)
            return True
        if task.pendingRevisionDecision == revision.revision:
            return self._resolve_revision_decision(task, project, repos, revision)
        if task.pendingRevisionDecision is not None:
            # A newer edit superseded the one awaiting a decision.
            self.ctx.conversations.close_open(task, ACTION, "superseded_by_new_episode")
            task.pendingRevisionDecision = None
            task.pendingRevisionRewind = None

        response = self._run_revision_classifier(task, project, repos, revision)
        if self._usage_limit_pause(task, project, action=ACTION, response=response):
            return False
        if response.clarificationQuestion:
            self._ask_human(task, project, action=ACTION,
                            question=response.clarificationQuestion,
                            options=response.clarificationOptions)
            return False
        point = self._applicable(task, response.rewindTo)
        if point == RewindPoint.NoChange:
            self._accept_revision(task, revision, restamp=True)
            return True
        if task.status in _POST_EXECUTION or (
                task.status == TaskStatus.Blocked and self._has_code(task)):
            self._ask_revision_decision(task, project, revision, point,
                                        response.changeSummary)
            return False
        previous = self._current_revision(task)
        self._accept_revision(task, revision, restamp=False)
        self._comment(task, project,
                      f"The description changed ({response.changeSummary or 'edited'}); "
                      f"redoing from {point}.")
        self._rewind_to(task, project, point, reason="edit",
                        guidance=response.rationale, previous=previous, new=revision)
        return True

    def _ask_revision_decision(self, task: Task, project: Project,
                               revision: TaskRevision, point: RewindPoint,
                               summary: str) -> None:
        """Edits after code exists: ask first (§8.6). The card stays in its
        column and routes to the human, like every other pause."""
        options = ClarificationOptions(header="Card edited", answers=[
            Answer(option=OPTION_REDO.format(stage=point), isRecommended=True,
                   description="rerun that stage and everything after it"),
            Answer(option=OPTION_AMEND,
                   description="keep the spec and plan, change the code"),
            Answer(option=OPTION_KEEP,
                   description="treat the edit as not changing the work"),
        ])
        question = (f"The card changed after code was written"
                    f"{f' ({summary})' if summary else ''}. How should I proceed?")
        task.pendingRevisionDecision = revision.revision
        task.pendingRevisionRewind = str(point)
        self.ctx.conversations.open_question(task, ACTION, project.id, question, options)
        self._ask_human(task, project, action=ACTION, question=question, options=options)

    def _resolve_revision_decision(self, task: Task, project: Project,
                                   repos: list[Repository],
                                   revision: TaskRevision) -> bool:
        """The human handed a late-edit decision back (§8.6 step 4)."""
        reply, selected = self.ctx.conversations.take_reply(
            task, ACTION, self.ctx.task_adapter_for(project))
        if reply is None:
            return False  # handed back without an answer: keep waiting
        recommended = RewindPoint(task.pendingRevisionRewind or RewindPoint.Classification)
        if selected == OPTION_REDO.format(stage=recommended):
            point = recommended
        elif selected == OPTION_AMEND:
            point = RewindPoint.Execution
        elif selected == OPTION_KEEP:
            point = RewindPoint.NoChange
        else:
            # Free text: the classifier reads the reply as guidance.
            task.revisionDecisionRounds += 1
            response = self._run_revision_classifier(task, project, repos, revision,
                                                     guidance=reply)
            if self._usage_limit_pause(task, project, action=ACTION, response=response):
                return False
            cap = self.ctx.settings.escalation.max_clarify_rounds
            if response.clarificationQuestion:
                if task.revisionDecisionRounds >= cap:
                    self._park_blocked(task, project,
                                       "Could not settle how to handle the card edit.",
                                       approval_required=False)
                    return False
                self._ask_revision_decision(
                    task, project, revision,
                    self._applicable(task, response.rewindTo), response.changeSummary)
                return False
            point = self._applicable(task, response.rewindTo)

        previous = self._current_revision(task)
        if point == RewindPoint.NoChange:
            self._accept_revision(task, revision, restamp=True)
            return True
        self._accept_revision(task, revision, restamp=False)
        self._rewind_to(task, project, point, reason="late_edit_decision",
                        guidance=reply, previous=previous, new=revision)
        return True

    # ----------------------------------------------------------- board moves

    def _apply_human_move(self, task: Task, project: Project, repos: list[Repository],
                          target: TaskStatus, column: str,
                          revision: TaskRevision | None) -> bool:
        """The human moved the card: target stage T is authoritative (§8.7)."""
        log.info("human move", extra={"event": "human_move",
                                      "from_status": str(task.status),
                                      "to_status": str(target)})
        # The human's placement is now the baseline, not a pending move.
        task.lastSyncedColumnId = column
        if task.pendingRevisionDecision is not None:
            self.ctx.conversations.close_open(task, ACTION, "superseded_by_human_move")
            task.pendingRevisionDecision = None
            task.pendingRevisionRewind = None

        # An edit observed together with the move (§8.7 step 4).
        edit_point: RewindPoint | None = None
        previous = self._current_revision(task)
        if revision is not None:
            if task.type is not None:
                response = self._run_revision_classifier(task, project, repos, revision)
                if not response.usageLimitSignals and not response.clarificationQuestion:
                    edit_point = self._applicable(task, response.rewindTo)
            self._accept_revision(task, revision, restamp=edit_point in (
                None, RewindPoint.NoChange))

        if target == TaskStatus.Shipped:
            self._declare_shipped(task, project)
            return False
        if target == TaskStatus.QA:
            return self._move_to_qa(task, project, repos)

        from_rank = self._progress_rank(task)
        to_rank = STATUS_RANK[target]
        amended = ""
        if edit_point is not None and edit_point != RewindPoint.NoChange \
                and _REWIND[edit_point][0] < min(to_rank, from_rank + 1):
            # The edit invalidates something before T: redo from there,
            # silently up to T (§8.7 — the rerun stages are the amended ones).
            self._rewind_to(task, project, edit_point, reason="edit",
                            previous=previous, new=revision, move_card=False)
            amended = f" The edit also changed earlier work, so it is redone from {edit_point}."
            task.cardHoldStatus = target
            self._comment(task, project, f"Continuing from {target} as moved.{amended}")
            return True

        if target == TaskStatus.TaskPending:
            # Icebox: start over.
            self._rewind(task, project, rank=0, status=TaskStatus.TaskPending,
                         reason="backward_move",
                         guidance=self._human_guidance(task, project), move_card=False)
            return True
        if to_rank < from_rank or (to_rank == from_rank
                                   and task.status == TaskStatus.Blocked):
            # A Blocked task moved to the stage it stalled at is a redo of it.
            self._backward_move(task, project, target)
            return True
        if to_rank == from_rank:
            return True
        return self._forward_move(task, project, repos, target)

    def _backward_move(self, task: Task, project: Project, target: TaskStatus) -> None:
        guidance = self._human_guidance(task, project)
        if target in _CHECKPOINT_STATUS:
            # Rerun a classification checkpoint: supersede what it decided.
            self._rewind(task, project, rank=_CHECKPOINT_STATUS[target], status=target,
                         reason="backward_move", guidance=guidance, move_card=False)
            return
        self._rewind_to(task, project, _STATUS_REWIND[target], reason="backward_move",
                        guidance=guidance, move_card=False)
        if target in (TaskStatus.CodeReview, TaskStatus.InReview):
            # Both mean "rework the code" going backward; the card follows
            # the task as it runs.
            task.cardHoldStatus = target

    def _forward_move(self, task: Task, project: Project,
                      repos: list[Repository], target: TaskStatus) -> bool:
        """Continue from T, deriving only what T strictly requires, without
        walking the card back through the skipped columns (§8.2-§8.3)."""
        task.cardHoldStatus = target
        if task.type is None:
            self._run_classification(task, project, repos)
        if task.escalationTier is None:
            task.escalationTier = EscalationTier.E0
        match target:
            case TaskStatus.TaskFinalization | TaskStatus.PassingCriteria \
                    | TaskStatus.TaskFinalized | TaskStatus.PlanFinalization:
                self._set_status(task, target)
                return True
            case TaskStatus.PlanFinalized:
                self._set_status(task, TaskStatus.PlanFinalized if task.taskPlanningAction
                                 else TaskStatus.PlanFinalization)
                return True
            case TaskStatus.InProgress | TaskStatus.CodeReview | TaskStatus.InReview:
                if not self._define_criteria(task, project, repos):
                    return False  # paused on a criteria question
                self._begin_execution(task, project, repos,
                                      tier=task.escalationTier or EscalationTier.E0)
                return False
        return True

    def _move_to_qa(self, task: Task, project: Project,
                    repos: list[Repository]) -> bool:
        """QA cannot be derived — merging is a human act on GitHub (§8.2)."""
        merged = bool(task.repoWork) and all(
            w.status in (RepoWorkStatus.Merged, RepoWorkStatus.NoOp) or self._pr_merged(
                w, repos) for w in task.repoWork)
        if merged:
            for work in task.repoWork:
                if work.status != RepoWorkStatus.NoOp:
                    work.status = RepoWorkStatus.Merged
            self._set_status(task, TaskStatus.QA)
            self._route_to_human(task, project)
            return False
        column = status_to_column(project.columns, task.status)
        if column:
            self._move_card(task, project, column)
        self._comment(task, project,
                      "This card can't move to QA yet: QA means its pull requests are "
                      "merged, and merging happens on GitHub. Moved it back.")
        return False

    def _pr_merged(self, work, repos: list[Repository]) -> bool:
        repo = next((r for r in repos if r.id == work.repoId), None)
        if repo is None or not work.prUrl:
            return False
        try:
            git = self.ctx.git_for(repo)
            number = git.pr_number_from_url(work.prUrl)
            return bool(number) and git.pull_request_merged(repo.githubRepo, number)
        except Exception:
            return False

    def _declare_shipped(self, task: Task, project: Project) -> None:
        """The human declares the round done, outside any release (§8.2)."""
        open_prs = [w.prUrl for w in task.repoWork
                    if w.prUrl and w.status == RepoWorkStatus.PrOpen]
        self._mark_shipped(task, project)
        if open_prs:
            self._comment(task, project,
                          "Marked shipped as moved. These pull requests are still "
                          "open and were left as they are:\n"
                          + "\n".join(f"- {url}" for url in open_prs))

    def _move_out_of_qa(self, task: Task, project: Project,
                        repos: list[Repository], target: TaskStatus) -> None:
        """A QA card is merged and sits in a release batch. Shipped is honored;
        any other move is refused, since the merged code cannot be revised in
        this round (§17 q1, answered conservatively: ship it, then move it back
        to start a new round)."""
        if target == TaskStatus.Shipped:
            self._declare_shipped(task, project)
            return
        self._move_card(task, project, project.columns.qa)
        self._comment(task, project,
                      "This round's code is already merged and waiting in a release, "
                      "so it can't be reworked here. Ship it (or move it to Shipped), "
                      "then move the card back to start a new round.")

    # ---------------------------------------------------------------- rounds

    def _start_new_round(self, task: Task, project: Project, snap: CardSnapshot,
                         target: TaskStatus) -> None:
        """A shipped card moved back: the same card starts a new round (§9.2).
        Idempotent across a crash between saving the new row and flipping the
        old flag: an existing successor is reused."""
        successor = self.ctx.task_repo.find_one(
            {"previousTaskId": task.id, "userId": task.userId})
        if successor is None:
            successor = Task(
                userId=task.userId, projectId=task.projectId,
                externalId=task.externalId, boardId=task.boardId,
                title=snap.title or task.title,
                description=snap.description if snap.title else task.description,
                labels=snap.labels, priority=snap.priority,
                round=task.round + 1, previousTaskId=task.id, isCurrentRound=True,
                status=TaskStatus.TaskPending,
                # None, so the new round's first reconcile treats the card's
                # column as a human move and derives the minimum for it (§9.2).
                lastSyncedColumnId=(snap.columnId if target == TaskStatus.TaskPending
                                    else None),
                priorRound=PriorRoundContext(
                    previousTaskId=task.id, round=task.round, title=task.title,
                    description=task.description,
                    finalizedSpecUrl=task.taskFinalizationAction,
                    planUrl=task.taskPlanningAction,
                    prUrls=[w.prUrl for w in task.repoWork if w.prUrl],
                    shippedAt=task.modifiedTime),
            )
            # Saved before the old flag flips (§9.2 step 3).
            self.ctx.task_repo.save(successor)
            revisions = getattr(self.ctx, "revision_repo", None)
            if revisions is not None:
                revisions.save(TaskRevision(
                    userId=successor.userId, taskId=successor.id, revision=1,
                    title=successor.title, description=successor.description,
                    fingerprint=content_fingerprint(successor.title,
                                                    successor.description)))
            snapshots = getattr(self.ctx, "snapshot_repo", None)
            if snapshots is not None:
                snapshots.save(snap.model_copy(update={
                    "id": successor.id, "taskId": successor.id}))
        task.isCurrentRound = False
        log.info("round started", extra={"event": "round_started",
                                         "round": successor.round,
                                         "previous_task_id": task.id,
                                         "new_task_id": successor.id})
        queue = getattr(self.ctx, "task_queue", None)
        if queue is not None:
            queue.enqueue_task(successor.id)


def _content(revision: TaskRevision | None) -> str:
    if revision is None:
        return ""
    return f"{revision.title}\n\n{revision.description or ''}".strip()

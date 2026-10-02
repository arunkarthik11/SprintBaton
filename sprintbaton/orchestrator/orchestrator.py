"""Workflow Orchestrator — owns task state, assignment, column transitions, and
the escalation state machine (main spec Architecture table; escalation spec §1, §8).
"""

import functools
import logging
import shutil
import time
from pathlib import Path

from sprintbaton.entities.actions import TaskExecutionResponse, TaskReviewResponse
from sprintbaton.entities.base import new_id, now_millis
from sprintbaton.entities.clarification import ClarificationOptions
from sprintbaton.entities.enums import (
    EscalationTier,
    MetadataScope,
    PlanClassificationVerdict,
    ReleaseStatus,
    SpecClassificationVerdict,
    TaskKind,
    TaskStatus,
    TaskType,
    TriggerType,
)
from sprintbaton.entities.project import Project
from sprintbaton.entities.repository import Repository
from sprintbaton.entities.task import (
    MetadataRepoProgress,
    RepoWork,
    Task,
    derive_task_status,
)
from sprintbaton.entities.enums import RepoWorkStatus
from sprintbaton.harness.base import AuthResolutionError
from sprintbaton.metadata.contract import (
    validate_project_metadata,
    validate_repo_metadata,
)
from sprintbaton.metadata.initialization import (
    MetadataPassFailed,
    NoMetadataProduced,
    latest_initialization_task,
    project_metadata_ready,
    remedies_for,
)
from sprintbaton.metadata.revisions import MetadataRevisionPublisher
from sprintbaton.orchestrator.retry import (
    TRANSIENT,
    backoff_millis,
    classify_initialization_error,
)
from sprintbaton.escalation.ladder import entry_tier, next_tier
from sprintbaton.escalation.situation_report import build_situation_report
from sprintbaton.escalation.triggers import ExecutionSignals, evaluate_triggers
from sprintbaton.observer.context import log_context
from sprintbaton.services.adapters import TaskPassingCriteriaResponseAdapter
from sprintbaton.services.base import (
    ServiceContext,
    TaskActionServiceFactory,
    settings_for,
)
from sprintbaton.orchestrator.board import move_card
from sprintbaton.orchestrator.reconcile import CardReconcileMixin
from sprintbaton.storage.keys import revision_filename
from sprintbaton.vcs.git_service import project_init_metadata_dir

log = logging.getLogger(__name__)

BLOCKED_LABEL = "sprintbaton-blocked"


def _with_action(action: str):
    """Tag every log line emitted anywhere below this dispatch method with the
    action name (cli-logging spec §4.1) — the exact vocabulary
    TaskActionEventRecorder.record(action=...) already established, so log
    lines and TaskActionEvent rows join on the same key."""
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(self, *args, **kwargs):
            with log_context(action=action):
                return fn(self, *args, **kwargs)
        return wrapper
    return decorator


class TaskOrchestrator(CardReconcileMixin):
    def __init__(self, ctx: ServiceContext, factory: TaskActionServiceFactory):
        self.ctx = ctx
        self.factory = factory
        # project id -> monotonic time the awaiting-initialization notice was
        # last logged, rate-limiting it to once per project per poll cycle
        # (project-initialization-task spec §5.7).
        self._awaiting_logged: dict[str, float] = {}

    # ------------------------------------------------------------------ entry

    def process(self, task_id: str) -> None:
        task = self.ctx.task_repo.get(task_id)
        if task is None:
            log.warning("task not found", extra={"task_id": task_id})
            return
        if not task.isCurrentRound:
            # A stale queue entry for a superseded round (task-revisions spec
            # §10): the card's work now lives on its successor.
            return
        project = self.ctx.project_repo.get(task.projectId)
        if project is None:
            log.error("project not found for task", extra={"task_id": task_id})
            if (task.kind == TaskKind.ProjectInitialization
                    and task.status == TaskStatus.ProjectInitialization):
                # Otherwise polling would rediscover an orphan run forever.
                self._fail_initialization(
                    task, None, "the project was deleted before its "
                                "initialization run could complete")
                task.touch()
                self.ctx.task_repo.save(task)
            return
        # The TaskPending gate (project-initialization-task spec §5.4): a board
        # task waits — unclaimed, unclassified — until the project has published
        # metadata at least once. Only TaskPending is gated, so work already in
        # flight never freezes; the polling job re-enqueues the card every
        # cycle, so no wake-up machinery is needed once the gate opens.
        if (task.kind == TaskKind.Development
                and task.status == TaskStatus.TaskPending
                and not project_metadata_ready(project)):
            with log_context(task_id=task.id, user_id=task.userId,
                             repo_id=task.projectId):
                self._report_awaiting_initialization(task, project)
            return
        repos = self.ctx.repositories_for(project)

        # One request_id per process() call — a single pass through the state
        # machine for one task, exactly the unit the processingClaimedAt claim
        # brackets (cli-logging spec §4.1). A task resuming after a
        # clarification pause gets a new one: nothing was in flight during
        # the wait.
        with log_context(request_id=new_id("req"), task_id=task.id,
                         user_id=task.userId, repo_id=task.projectId):
            log.info("processing task", extra={"event": "task_started",
                                               "status": str(task.status)})
            # In-flight claim (zero-infra-storage spec §4.3): persisted before any
            # work so a crash mid-process() leaves a visibly stale claim for the
            # startup reconciliation pass to re-enqueue. Cleared in the finally —
            # a human pause returns from process() and so releases it too.
            task.processingClaimedAt = now_millis()
            self.ctx.task_repo.save(task)
            try:
                if task.kind == TaskKind.ProjectInitialization:
                    # Inside the claim bracket, outside the board state machine
                    # (spec §5.5): an init task never reaches a board helper.
                    self._process_initialization(task, project, repos)
                    return
                # The poller only observes; decisions happen here, inside the
                # claim bracket (task-revisions spec §4/§10).
                if not self._reconcile_card(task, project, repos):
                    return
                match task.status:
                    case TaskStatus.TaskPending:
                        self._classify(task, project, repos)
                    case TaskStatus.TaskFinalization | TaskStatus.Blocked:
                        self._resume_clarification(task, project, repos)
                    case TaskStatus.PassingCriteria:
                        self._resume_criteria(task, project, repos)
                    case TaskStatus.TaskFinalized:
                        self._classify_spec(task, project, repos)
                    case TaskStatus.PlanFinalization:
                        self._plan_then_execute(task, project, repos)
                    case TaskStatus.PlanFinalized:
                        self._classify_plan(task, project, repos)
                    case TaskStatus.InProgress | TaskStatus.CodeReview:
                        self._run_execution_loop(task, project, repos)
                    case TaskStatus.InReview:
                        self._review_reentry(task, project, repos)
                    case TaskStatus.QA:
                        self._ship(task, project, repos)
                    case _:
                        log.info("no action for status",
                                 extra={"status": str(task.status)})
            except AuthResolutionError as e:
                # A config error, not a task failure (auth-mode-resolution spec
                # §9 q3). Left to propagate it would escape process(), and the
                # polling job would re-enqueue the task every cycle — a
                # misconfiguration retrying forever. Park it instead, with the
                # resolver's own message (which names the action, harness and
                # credential tier) in front of the human who can fix it.
                # AgentBindingError (provider-setup-cli spec §7.3 — no agent
                # bound to this action at all) is a subclass precisely so it
                # lands here: same shape of problem, same remedy channel.
                log.error("agent resolution failed", extra={"event": "error",
                                                            "error": str(e)})
                self._park_blocked(task, project, str(e), approval_required=False)
            finally:
                task.processingClaimedAt = None
                task.touch()
                self.ctx.task_repo.save(task)

    # ------------------------------------------------- multi-repo work helpers

    def _repo_by_id(self, repos: list[Repository], repo_id: str) -> Repository | None:
        return next((r for r in repos if r.id == repo_id), None)

    def _sync_task_status(self, task: Task, project: Project,
                          repos: list[Repository]) -> None:
        """Recompute the single card status from the slowest repo and move the
        todolist card when it changes (multi-repo-project spec §7.5)."""
        derived = derive_task_status(task.repoWork)
        # QA/Blocked are handled explicitly by the loop's finalizers; here we
        # only reflect the pre-release execution/review span.
        if derived in (TaskStatus.InProgress, TaskStatus.CodeReview,
                       TaskStatus.InReview) and derived != task.status:
            column = {
                TaskStatus.InProgress: project.columns.in_progress,
                TaskStatus.CodeReview: project.columns.code_review,
                TaskStatus.InReview: project.columns.in_review,
            }[derived]
            self._move_card(task, project, column)
            self._set_status(task, derived)

    def _set_status(self, task: Task, status: TaskStatus) -> None:
        """The one place a task's status changes — every transition emits the
        state_transition key event (cli-logging spec §5)."""
        # task_id is stamped explicitly (not left to the ambient context)
        # because _ship marks release *siblings* shipped while the context
        # still carries the triggering task's id.
        log.info("state transition", extra={"event": "state_transition",
                                            "task_id": task.id,
                                            "from_status": str(task.status),
                                            "to_status": str(status)})
        task.status = status
        if status in (TaskStatus.Shipped, TaskStatus.Blocked):
            self._close_sandbox_session(task)

    def _close_sandbox_session(self, task: Task) -> None:
        """Close the task's sandbox session eagerly at Shipped/Blocked
        (hosted-sandbox-isolation spec §6.6). Best-effort: the worker's clone
        is authoritative (invariant 7), so a failed close loses nothing and
        the session's idle TTL reclaims it."""
        runtime = getattr(self.ctx, "sandbox", None)
        if runtime is None or not runtime.isolated:
            return
        try:
            runtime.sandbox.open_session(task.userId, task.id).close()
        except Exception:
            log.warning("could not close the task's sandbox session",
                        extra={"task_id": task.id}, exc_info=True)

    # --------------------------------------------------------------- routing

    @_with_action("classification")
    def _classify(self, task: Task, project: Project,
                  repos: list[Repository]) -> None:
        tier = self._run_classification(task, project, repos)

        match tier:
            case EscalationTier.E0:
                if self._define_criteria(task, project, repos):
                    self._begin_execution(task, project, repos, tier=EscalationTier.E0)
            case EscalationTier.E1:
                self._clarify(task, project, repos)
            case EscalationTier.E2:
                if self._define_criteria(task, project, repos):
                    self._plan_then_execute(task, project, repos)
            case EscalationTier.E3:
                # Unreachable from entry routing since Abstract moved to E1
                # (finalization-passing-criteria spec §5.1) — kept defensively.
                self._begin_execution(task, project, repos, tier=EscalationTier.E3)

    @_with_action("classification")
    def _run_classification(self, task: Task, project: Project,
                            repos: list[Repository]) -> EscalationTier:
        """The Router call alone — category, importance, entry tier — without
        the onward routing, so a human's forward board move can derive it
        silently as a prerequisite (task-revisions spec §8.2)."""
        from_status = task.status
        response = self.factory.classification.run(task, project, repos)
        task.type = response.category          # always one of the 4 real categories
        task.tokensSpent += response.tokensUsed

        # Importance is orthogonal to category (classification-taxonomy spec
        # §3.2) — the category is never overwritten. Monotonic within a round
        # (task-revisions spec §8.4): a rerun after a rewind can raise it,
        # never lower it.
        task.important = task.important or bool(response.importanceFlags)
        if task.important:
            task.mandatoryHumanReview = True

        tier = entry_tier(task.type, task.important)
        task.escalationTier = tier
        self.ctx.observer.task_processed(str(task.type), "classified")
        self.ctx.analytics.record(task, action="classification",
                                  from_status=from_status, response=response)
        self._record_note(task, project, "classification", response.rationale)
        self._record_classification_provenance(
            task, project, action="classification", response=response)
        return tier

    # ---------------------------------------------------------- clarification

    @_with_action("finalization")
    def _clarify(self, task: Task, project: Project,
                 repos: list[Repository]) -> None:
        adapter = self.ctx.task_adapter_for(project)
        if task.clarifyRounds >= self.ctx.settings.escalation.max_clarify_rounds:
            self.ctx.conversations.close_open(
                task, self.factory.finalization.action_for(task),
                "clarify_cap_reached")
            self._park_blocked(task, project,
                               "Clarification round cap reached; summarizing and parking.",
                               approval_required=False)
            return

        from_status = task.status
        response = self.factory.finalization.run(task, project, repos)
        task.tokensSpent += response.tokensUsed

        # An eighth guard site beyond the spec's seven clarificationQuestion
        # ones: finalization pauses on a `questions` list instead, but its
        # harness run can hit a usage limit all the same (it is a first-class
        # claude_code_cli consumer in CLI/admin mode).
        if response.usageLimitSignals and self._usage_limit_pause(
                task, project, action=self.factory.finalization.action_for(task),
                response=response):
            self.ctx.analytics.record(task, action="finalization",
                                      from_status=from_status, response=response)
            return

        if response.finalizedSpec:
            self.ctx.analytics.record(task, action="finalization",
                                      from_status=from_status, response=response)
            # Consumed only after the event recorded it as rework (§12).
            task.amendments.pop("abstract_finalization" if task.type == TaskType.Abstract
                                else "finalization", None)
            # Criteria derive from the finalized spec text alone, so they are
            # generated the moment that text becomes available — before the
            # unrelated plan-vs-direct routing decision
            # (finalization-passing-criteria spec §6.4)
            if not self._define_criteria(task, project, repos):
                return  # paused on a criteria clarification; resumes via PassingCriteria
            # Answers were sufficient — the Spec Classification checkpoint
            # decides plan-vs-direct from the finalized spec itself
            # (implementability spec §5.1)
            self._set_status(task, TaskStatus.TaskFinalized)
            self._move_card(task, project, project.columns.task_finalized)
            self._classify_spec(task, project, repos)
            return

        task.clarifyRounds += 1
        comment = self.ctx.translator.render_questions(response.questions)
        adapter.add_comment(task.externalId, comment)
        self._move_card(task, project, project.columns.task_finalization)
        self._route_to_human(task, project)
        self._set_status(task, TaskStatus.TaskFinalization)
        self.ctx.analytics.record(task, action="finalization",
                                  from_status=from_status, response=response)
        log.info("awaiting clarification", extra={"event": "clarification_paused",
                                                  "action": "finalization",
                                                  "round": task.clarifyRounds})

    def _resume_clarification(self, task: Task, project: Project,
                              repos: list[Repository]) -> None:
        """User answered (task reassigned to the agent): run the Spec loop again."""
        self._clarify(task, project, repos)

    # --------------------------------------------------------- passing criteria

    @_with_action("passing_criteria")
    def _define_criteria(self, task: Task, project: Project,
                         repos: list[Repository]) -> bool:
        """Passing Criteria checkpoint (finalization-passing-criteria spec §6):
        from the spec alone (raw description for Simple/Complex, finalized spec
        for Ambiguous/Abstract), enumerate exhaustive acceptance criteria before
        any planning or execution begins. Machine-only, idempotent — a task only
        ever gets criteria once. Returns False when the checkpoint paused on a
        clarification question (conversation-lifecycle spec §6) — the caller
        must then stop; the reply re-enters via the PassingCriteria dispatch arm."""
        if task.taskPassingCriteriaAction:
            return True
        from_status = task.status
        self._move_card(task, project, project.columns.passing_criteria)
        self._set_status(task, TaskStatus.PassingCriteria)

        response = self.factory.passing_criteria.run(task, project, repos)
        task.tokensSpent += response.tokensUsed
        self.ctx.analytics.record(task, action="passing_criteria",
                                  from_status=from_status, response=response)
        self._record_note(task, project, "passing_criteria", response.rationale)
        if self._usage_limit_pause(task, project, action="passing_criteria",
                                   response=response):
            return False
        if response.clarificationQuestion:
            self._ask_human(task, project, action="passing_criteria",
                            question=response.clarificationQuestion,
                            options=response.clarificationOptions)
            return False
        key = self.ctx.object_storage.task_key(
            task.userId, project.id, task.id, "artifacts",
            revision_filename("passing-criteria.md", task.currentRevision))
        task.taskPassingCriteriaAction = self.ctx.object_storage.put_text(
            key, TaskPassingCriteriaResponseAdapter.to_markdown(response)
        )
        task.artifactRevisions["passing_criteria"] = task.currentRevision
        task.amendments.pop("passing_criteria", None)
        return True

    def _resume_criteria(self, task: Task, project: Project,
                         repos: list[Repository]) -> None:
        """Crash recovery / clarification re-entry: a task re-queued
        mid-checkpoint re-runs _define_criteria (idempotent) and continues down
        its category's own path (finalization-passing-criteria spec §4)."""
        if not self._define_criteria(task, project, repos):
            return
        match task.type:
            case TaskType.Ambiguous | TaskType.Abstract:
                self._set_status(task, TaskStatus.TaskFinalized)
                self._move_card(task, project, project.columns.task_finalized)
                self._classify_spec(task, project, repos)
            case TaskType.Complex:
                self._plan_then_execute(task, project, repos)
            case _:  # Simple
                if task.important:
                    # Entered at E2 via the importance floor (classification-
                    # taxonomy spec §6.1) — planning path, like Complex.
                    self._plan_then_execute(task, project, repos)
                else:
                    self._begin_execution(task, project, repos,
                                          tier=task.escalationTier or EscalationTier.E0)

    # ---------------------------------------------- classification checkpoints

    @_with_action("spec_classification")
    def _classify_spec(self, task: Task, project: Project,
                       repos: list[Repository]) -> None:
        """Spec Classification checkpoint (classification-taxonomy spec §4.3):
        3-way verdict — Simple (Sonnet direct), Compound (Opus direct), or
        Complex (needs a plan first). Importance and Abstract stay
        Opus-execution floors on the no-plan branch."""
        from_status = task.status
        response = self.factory.spec_classification.run(task, project, repos)
        task.tokensSpent += response.tokensUsed
        self.ctx.analytics.record(task, action="spec_classification",
                                  from_status=from_status, response=response)
        self._record_note(task, project, "spec_classification", response.rationale)
        self._record_classification_provenance(
            task, project, action="spec_classification", response=response)
        if self._usage_limit_pause(task, project, action="spec_classification",
                                   response=response):
            return
        if response.clarificationQuestion:
            self._ask_human(task, project, action="spec_classification",
                            question=response.clarificationQuestion,
                            options=response.clarificationOptions)
            return
        verdict = response.verdict
        # Importance floor: only ever escalates Simple -> Compound. A genuine
        # Complex verdict still plans, regardless of importance — importance
        # never skips planning outright, it only rules out the cheap model in
        # the no-plan branch. Abstract keeps its own inherent floor,
        # independent of importance (classification-taxonomy spec §3.1).
        if verdict == SpecClassificationVerdict.Simple and (
                task.important or task.type == TaskType.Abstract):
            verdict = SpecClassificationVerdict.Compound

        match verdict:
            case SpecClassificationVerdict.Complex:
                self._plan_then_execute(task, project, repos)
            case SpecClassificationVerdict.Compound:
                self._begin_execution(task, project, repos, tier=EscalationTier.E3)
            case SpecClassificationVerdict.Simple:
                self._begin_execution(task, project, repos, tier=EscalationTier.E0)

    @_with_action("plan_classification")
    def _classify_plan(self, task: Task, project: Project,
                       repos: list[Repository]) -> None:
        """Plan Classification checkpoint (classification-taxonomy spec §5.3):
        2-way verdict — Simple (Sonnet) or Compound (Opus) execution, from the
        actual plan. Importance and Abstract are floors the classifier can
        never lower."""
        from_status = task.status
        response = self.factory.plan_classification.run(task, project, repos)
        task.tokensSpent += response.tokensUsed
        self.ctx.analytics.record(task, action="plan_classification",
                                  from_status=from_status, response=response)
        self._record_note(task, project, "plan_classification", response.rationale)
        self._record_classification_provenance(
            task, project, action="plan_classification", response=response)
        if self._usage_limit_pause(task, project, action="plan_classification",
                                   response=response):
            return
        if response.clarificationQuestion:
            self._ask_human(task, project, action="plan_classification",
                            question=response.clarificationQuestion,
                            options=response.clarificationOptions)
            return
        verdict = response.verdict
        # Same importance/Abstract floor as Spec Classification (§4.3) — only
        # ever escalates Simple -> Compound.
        if verdict == PlanClassificationVerdict.Simple and (
                task.important or task.type == TaskType.Abstract):
            verdict = PlanClassificationVerdict.Compound

        tier = (EscalationTier.E3 if verdict == PlanClassificationVerdict.Compound
                else EscalationTier.E0)
        self._begin_execution(task, project, repos, tier=tier)

    # -------------------------------------------------------------- planning

    @_with_action("planning")
    def _plan_then_execute(self, task: Task, project: Project,
                           repos: list[Repository],
                           situation_report: str | None = None) -> None:
        adapter = self.ctx.task_adapter_for(project)
        from_status = task.status
        self._move_card(task, project, project.columns.plan_finalization)
        self._set_status(task, TaskStatus.PlanFinalization)

        response = self.factory.planning.run(task, project, repos,
                                             situation_report=situation_report)
        task.tokensSpent += response.tokensUsed
        self.ctx.analytics.record(task, action="planning",
                                  from_status=from_status, response=response)
        if self._usage_limit_pause(task, project, action="planning", response=response):
            return
        if response.clarificationQuestion:
            # The Planning Model hit a tradeoff only a human can settle
            # (conversation-lifecycle spec §2): pause in PlanFinalization; the
            # answer re-enters through this same dispatch arm.
            self._ask_human(task, project, action="planning",
                            question=response.clarificationQuestion,
                            options=response.clarificationOptions)
            return
        task.amendments.pop("planning", None)  # after the event recorded it
        if task.taskPlanningAction:
            adapter.add_comment(
                task.externalId,
                self.ctx.translator.render_plan_notice(task.taskPlanningAction),
            )

        # E2 hands back to the Coding Model via the Plan Classification
        # checkpoint, which picks the execution tier from the actual plan
        # (implementability spec §5.3)
        task.escalationTier = max(task.escalationTier or EscalationTier.E0,
                                  EscalationTier.E2)
        self._set_status(task, TaskStatus.PlanFinalized)
        self._move_card(task, project, project.columns.plan_finalized)
        self._classify_plan(task, project, repos)

    # -------------------------------------------------------------- execution

    def _begin_execution(self, task: Task, project: Project,
                         repos: list[Repository], *, tier: EscalationTier) -> None:
        """Scope the repos this task touches (once), seed one RepoWork per
        affected repo, then run the serial per-repo execution loop
        (multi-repo-project spec §7.3-§7.4)."""
        task.escalationTier = max(task.escalationTier or tier, tier)
        if task.affectedRepoIds is None:
            scoping = self.factory.repo_scoping.run(task, project, repos)
            task.tokensSpent += scoping.tokensUsed
            if self._usage_limit_pause(task, project, action="repo_scoping",
                                       response=scoping):
                return
            affected = scoping.affectedRepoIds or [r.id for r in repos]
            task.affectedRepoIds = affected
            # A re-scope after a rewind keeps each repo's existing branch/PR
            # (task-revisions spec §8.5): re-execution amends on top of it.
            existing = {w.repoId: w for w in task.repoWork}
            dropped = [w.repoId for w in task.repoWork
                       if w.repoId not in affected and w.prUrl]
            if dropped:
                log.warning("re-scoping dropped repos with an open PR; the PRs "
                            "are left as they are", extra={"repo_ids": dropped})
            task.repoWork = [existing.get(rid) or RepoWork(repoId=rid, escalationTier=tier)
                             for rid in affected]
            self._record_note(task, project, "repo_scoping", scoping.rationale)
        self._run_execution_loop(task, project, repos)

    def _run_execution_loop(self, task: Task, project: Project,
                            repos: list[Repository]) -> None:
        """Process each affected repo serially (multi-repo-project spec §7.4):
        one repo runs to a PR (or a no-op, or a block) before the next begins.
        Stops the moment a repo pauses the whole task (clarification / usage
        limit / human handoff / block)."""
        if not task.repoWork:
            # Re-entered InProgress with no scoping (e.g. crash recovery):
            # scope defensively from all member repos.
            tier = task.escalationTier or EscalationTier.E0
            task.affectedRepoIds = [r.id for r in repos]
            task.repoWork = [RepoWork(repoId=r.id, escalationTier=tier) for r in repos]
        for work in task.repoWork:
            if work.status not in (RepoWorkStatus.Pending, RepoWorkStatus.InProgress,
                                   RepoWorkStatus.CodeReview):
                continue
            repo = self._repo_by_id(repos, work.repoId)
            if repo is None:
                work.status = RepoWorkStatus.NoOp
                continue
            if work.status == RepoWorkStatus.CodeReview and work.taskExecutionDiffAction:
                # Crash recovery mid-review (multi-repo-project spec §8.2): re-
                # review the persisted diff instead of re-executing from scratch.
                proceed = self._review_repo(
                    task, project, repos, repo, work,
                    self._last_execution_response(task, work))
            else:
                proceed = self._execute_repo(
                    task, project, repos, repo, work,
                    tier=work.escalationTier or task.escalationTier or EscalationTier.E0)
            if not proceed:
                return  # task paused / blocked / awaiting human — stop serially
        self._finalize_execution(task, project, repos)

    def _finalize_execution(self, task: Task, project: Project,
                            repos: list[Repository]) -> None:
        """Every affected repo has reached a terminal-for-now state. Move the
        card once, based on the slowest repo (multi-repo-project spec §7.5)."""
        derived = derive_task_status(task.repoWork)
        if derived == TaskStatus.Blocked:
            return  # a per-repo park already set Blocked + reassigned
        if derived == TaskStatus.QA:
            # Every affected repo was a no-op — nothing to review or ship.
            self._park_blocked(
                task, project,
                "Execution finished but produced no changes to commit.",
                approval_required=False)
            return
        self._enter_review(task, project, repos)

    def _enter_review(self, task: Task, project: Project,
                      repos: list[Repository]) -> None:
        """All of the task's PRs are open — hand the whole change set to the
        human once (multi-repo-project spec §8.1)."""
        adapter = self.ctx.task_adapter_for(project)
        pr_lines = [f"- {self._repo_title(repos, w.repoId)}: {w.prUrl}"
                    for w in task.repoWork if w.prUrl]
        if pr_lines:
            adapter.add_comment(
                task.externalId,
                "SprintBaton opened the following pull request(s) for this task:\n"
                + "\n".join(pr_lines))
        self._move_card(task, project, project.columns.in_review)
        self._route_to_human(task, project)
        self._set_status(task, TaskStatus.InReview)
        log.info("all PRs opened", extra={
            "event": "pr_opened",
            "prs": [w.prUrl for w in task.repoWork if w.prUrl]})

    @_with_action("execution")
    def _execute_repo(self, task: Task, project: Project, repos: list[Repository],
                      repo: Repository, work: RepoWork, *, tier: EscalationTier,
                      situation_report: str | None = None) -> bool:
        """Run one repo's execution turn (multi-repo-project spec §7.4).
        Returns True when the serial loop should continue to the next repo
        (this repo reached a PR / no-op), False when the whole task paused,
        blocked, or is awaiting a human."""
        from_status = task.status
        work.status = RepoWorkStatus.InProgress
        # Hysteresis: never de-escalate mid-task (escalation spec §8)
        work.escalationTier = max(work.escalationTier or tier, tier)
        self._sync_task_status(task, project, repos)

        response, signals = self.factory.execution.run(
            task, project, repo, tier=tier, situation_report=situation_report,
            work=work,
        )
        task.tokensSpent += response.tokensUsed

        if self._usage_limit_pause(task, project, action="execution", response=response):
            self.ctx.analytics.record(
                task, action="execution", from_status=from_status, response=response,
                duration_millis=int(signals.tier_wall_clock_seconds * 1000),
            )
            return False

        if response.clarificationQuestion:
            self.ctx.analytics.record(
                task, action="execution", from_status=from_status, response=response,
                duration_millis=int(signals.tier_wall_clock_seconds * 1000),
            )
            self._ask_human(task, project, action="execution",
                            question=response.clarificationQuestion,
                            options=response.clarificationOptions)
            return False

        triggers = evaluate_triggers(signals, self.ctx.settings.escalation)
        fired = (triggers[0] if triggers
                 else None if response.completed
                 else TriggerType.CorrectnessStall)
        self.ctx.analytics.record(
            task, action="execution", from_status=from_status, response=response,
            trigger=fired,
            duration_millis=int(signals.tier_wall_clock_seconds * 1000),
        )
        if triggers:
            return self._apply_escalation(task, project, repos, repo, work,
                                          response, signals, triggers[0])
        if response.completed:
            work.builtFromRevision = task.currentRevision
            task.amendments.pop("execution", None)
            self._record_note(task, project, "execution", response.summary)
            return self._review_repo(task, project, repos, repo, work, response)
        # Not complete and no trigger fired — treat as a correctness stall
        return self._apply_escalation(task, project, repos, repo, work,
                                      response, signals, TriggerType.CorrectnessStall)

    # ---------------------------------------------------------- AI review gate

    @_with_action("review")
    def _review_repo(self, task: Task, project: Project, repos: list[Repository],
                     repo: Repository, work: RepoWork,
                     response: TaskExecutionResponse) -> bool:
        """Gate one repo's completed diff through the Review Agent before its PR
        exists (AI review spec §5.2; multi-repo-project spec §8.2). Returns True
        when the loop should continue (PR opened / no-op), False when the task
        paused/blocked/escalated laterally."""
        from_status = task.status
        work.status = RepoWorkStatus.CodeReview
        self._sync_task_status(task, project, repos)
        # Persisted per-repo so a restart mid-review re-reviews this exact diff.
        work.taskExecutionDiffAction = self.ctx.object_storage.put_text(
            self.ctx.object_storage.task_key(
                task.userId, project.id, task.id, "artifacts",
                revision_filename(f"execution-diff-{repo.id}.md", task.currentRevision)),
            response.diff,
        )

        review = self.factory.review.run(task, project, repo, diff=response.diff)
        task.tokensSpent += review.tokensUsed
        self.ctx.analytics.record(task, action="review", from_status=from_status,
                                  response=review)

        if self._usage_limit_pause(task, project, action="review", response=review):
            return False

        if review.clarificationQuestion:
            self._ask_human(task, project, action="review",
                            question=review.clarificationQuestion,
                            options=review.clarificationOptions)
            return False

        if review.approved:
            return self._open_pull_request(task, project, repos, repo, work,
                                           response, review=review)

        work.aiReviewRounds += 1
        if review.findings:
            self.ctx.task_workspace.append_review_comments(
                project.id, task,
                round_number=work.aiReviewRounds, findings=review.findings)
        cap = self.ctx.settings.escalation.ai_review_max_rounds
        if work.aiReviewRounds > cap:
            # Bounce budget exhausted at this tier — fall through to the ladder.
            findings = "; ".join(review.findings) or "no specific findings."
            escalation_response = response.model_copy(update={
                "summary": f"AI review did not approve after {cap} round(s): {findings}",
            })
            return self._apply_escalation(task, project, repos, repo, work,
                                          escalation_response,
                                          ExecutionSignals(review_failed=True),
                                          TriggerType.ReviewFailure)

        # Bounce back to the Coding Model, same tier, no PR, no human visibility.
        report = build_situation_report(
            task_title=task.title, task_description=task.description or "",
            plan=self._current_plan(task), diff=response.diff,
            failure_evidence="\n".join(review.findings),
            hypothesis=None, attempts_ruled_out=[],
        )
        return self._execute_repo(
            task, project, repos, repo, work,
            tier=work.escalationTier or EscalationTier.E0, situation_report=report)

    def _last_execution_response(self, task: Task,
                                 work: RepoWork) -> TaskExecutionResponse:
        """Crash-recovery re-entry into a repo's CodeReview: rebuild a minimal
        completed response from the per-repo diff persisted to object storage."""
        diff = ""
        if work.taskExecutionDiffAction:
            diff = self.ctx.object_storage.get_text_by_url(
                work.taskExecutionDiffAction) or ""
        return TaskExecutionResponse(
            taskId=task.id, completed=True, diff=diff,
            summary="Re-reviewed after a restart; the original execution summary "
                    "was not persisted.",
        )

    # -------------------------------------------------------------- escalation

    def _apply_escalation(self, task: Task, project: Project,
                          repos: list[Repository], repo: Repository, work: RepoWork,
                          response: TaskExecutionResponse, signals: ExecutionSignals,
                          trigger: TriggerType) -> bool:
        """Escalate one repo's stalled execution (escalation spec §8;
        multi-repo-project spec §8.2 — per-repo tier on RepoWork). Returns True
        when the serial loop should continue (a same-repo higher-tier retry
        that itself reached a PR), False when the task paused/blocked or took a
        project-level lateral detour (clarify/replan) that drives its own loop."""
        current = work.escalationTier or EscalationTier.E0
        target = next_tier(current, trigger)
        work.escalationTier = max(work.escalationTier or target, target)
        task.escalationTier = max(task.escalationTier or target, target)
        if target != current:
            # Each tier gets its own fresh AI-review, human-review, and
            # conflict-resolution bounce budgets (per repo).
            work.aiReviewRounds = 0
            work.reviewFailures = 0
            work.conflictResolutionRounds = 0
        self.ctx.observer.escalation(task.id, str(task.type), str(trigger),
                                     str(current), str(target))
        if trigger in (TriggerType.IrreversibleOperation, TriggerType.DiscoveredImportance,
                       TriggerType.GlobalCircuitBreaker):
            self.ctx.observer.hard_trigger(task.id, str(trigger))

        report = build_situation_report(
            task_title=task.title,
            task_description=task.description or "",
            plan=self._current_plan(task),
            diff=response.diff,
            failure_evidence=response.summary,
            hypothesis=response.askedQuestion,
            attempts_ruled_out=[],
        )
        report_url = self.ctx.object_storage.put_text(
            self.ctx.object_storage.task_key(
                task.userId, project.id, task.id, "agent",
                f"situation-report-{repo.id}-{task.modifiedTime}.md"),
            report,
        )

        if trigger == TriggerType.DiscoveredImportance:
            # task.type stays whatever the Router said — a mid-execution
            # discovery no longer erases the category (classification-taxonomy
            # spec §6.3). The eventual _classify_plan applies the importance
            # floor, guaranteeing Opus execution. Re-plan resets scoping so the
            # new plan re-scopes; the current loop stops (the replan drives).
            task.important = True
            task.mandatoryHumanReview = True
            self._plan_then_execute(task, project, repos, situation_report=report)
            return False

        match target:
            case EscalationTier.E0:
                # In-tier retry with clean context (many stalls are context rot)
                return self._execute_repo(task, project, repos, repo, work,
                                          tier=current, situation_report=report)
            case EscalationTier.E1:
                self._clarify(task, project, repos)
                return False
            case EscalationTier.E2:
                self._plan_then_execute(task, project, repos, situation_report=report)
                return False
            case EscalationTier.E3 | EscalationTier.E4:
                # E4 is the Fable coding tier — one more autonomous attempt
                # after Opus (fable-coding-tier spec §4.1)
                return self._execute_repo(task, project, repos, repo, work,
                                          tier=target, situation_report=report)
            case EscalationTier.EH:
                self.ctx.observer.human_handoff(task.id, str(task.type), str(trigger))
                approval = trigger == TriggerType.IrreversibleOperation
                work.status = RepoWorkStatus.Blocked
                self._park_blocked(task, project,
                                   response.summary or "Escalated to human.",
                                   approval_required=approval, report_url=report_url)
                return False
        return False

    def _usage_limit_pause(self, task: Task, project: Project, *, action: str,
                           response) -> bool:
        """The shared dispatch-time guard (usage-limit-aware execution spec
        §5.2), checked before the clarificationQuestion guard at every site —
        a usage-limit signal always takes precedence: if the harness didn't
        get to run, there is no clarification to have surfaced either.

        Fallback-aware (agent-fallback spec §4): a signal first marks the
        provider that ran inactive; if the action's chain still has another
        available agent, the task is re-enqueued to walk to it within the
        loop rather than paused — the terminal pause is reached only once
        every agent in the chain is unavailable. A one-element chain has no
        next entry, so it pauses on the first hit, identical to today.

        Returns True when the caller must stop (paused OR falling back)."""
        if not response.usageLimitSignals:
            return False
        policy = getattr(self.ctx, "usage_limits", None)
        if policy is None:
            return False
        decision = policy.evaluate(task, response.usageLimitSignals)
        if not decision.should_pause:
            return False

        availability = getattr(self.ctx, "provider_availability", None)
        queue = getattr(self.ctx, "task_queue", None)
        if availability is not None and queue is not None:
            chain = self._provider_chain(action, task)
            ran = availability.first_available(chain, task.userId)
            if ran is not None:
                availability.mark_inactive(ran.provider_name, task.userId,
                                           decision.resume_at, decision.reason)
            if availability.has_available_alternative(chain, task.userId):
                queue.enqueue_task(task.id)
                log.info("falling back to next agent in chain", extra={
                    "event": "agent_fallback", "action": action,
                    "exhausted_provider": ran.provider_name if ran else None,
                    "scope": decision.reason})
                return True

        self._pause_for_usage_limit(task, project, action=action, decision=decision)
        return True

    def _provider_chain(self, action: str, task: Task):
        """The ordered provider chain for this action (agent-fallback spec §4).
        Execution's chain is per escalation tier; every other action's is flat."""
        if action == "execution":
            return self.ctx.agents.resolve_execution_tier_chain(
                task.escalationTier or EscalationTier.E0, task.userId)
        return self.ctx.agents.resolve_chain(action, task.userId)

    def _pause_for_usage_limit(self, task: Task, project: Project, *, action: str,
                               decision) -> None:
        """A harness reported it has no runway until a known/estimated time
        (usage-limit-aware execution spec §5). Deliberately no TaskStatus/
        column change and — unlike _ask_human — no reassignment to a human:
        this pause resolves itself once UsageLimitWakeJob re-enqueues the
        task. The next run of process() lands on the exact same dispatch arm
        and either resumes the same harness session (spec §8) or restarts the
        turn with prior context, the same graceful degradation every other
        resume-or-restart path already uses."""
        task.usageLimitPaused = True
        task.usageLimitPausedUntil = decision.resume_at
        task.usageLimitScope = decision.reason
        conversations = getattr(self.ctx, "conversations", None)
        if conversations is not None:
            # Keep the role's open episode alive across the pause so the
            # wake-job re-entry can resume the same harness session (spec §8).
            conversations.mark_usage_limit_pause(task, action, decision.resume_at)
        log.info("paused for usage limit", extra={
            "event": "usage_limit_paused", "action": action,
            "resume_at": decision.resume_at, "scope": decision.reason,
        })

    def _ask_human(self, task: Task, project: Project, *, action: str,
                   question: str,
                   options: ClarificationOptions | None = None) -> None:
        """A role paused in place on a clarification question (conversation-
        lifecycle spec §6): post it as a task comment and reassign to the
        human. Deliberately no TaskStatus/column change — the task drops out
        of the poll set until reassigned back, then the next poll cycle lands
        on the same dispatch arm and the ConversationRunner resumes/restarts
        the role's turn."""
        comment = self.ctx.translator.render_clarification_question(
            action, question, options)
        self.ctx.task_adapter_for(project).add_comment(task.externalId, comment)
        self._route_to_human(task, project)
        log.info("awaiting clarification", extra={"event": "clarification_paused",
                                                  "action": action})

    @_with_action("blocked")
    def _park_blocked(self, task: Task, project: Project, summary: str, *,
                      approval_required: bool, report_url: str | None = None) -> None:
        adapter = self.ctx.task_adapter_for(project)
        from_status = task.status
        comment = self.ctx.translator.render_situation_report(
            summary, report_url, approval_required=approval_required
        )
        adapter.add_comment(task.externalId, comment)
        blocked_column = project.columns.blocked
        if blocked_column:
            self._move_card(task, project, blocked_column, force=True)
        else:
            # No dedicated Blocked column: reuse Task Finalization + a label
            self._move_card(task, project, project.columns.task_finalization,
                            force=True)
            adapter.add_label(task.externalId, BLOCKED_LABEL)
        self._route_to_human(task, project)
        self._set_status(task, TaskStatus.Blocked)
        log.info("task blocked", extra={"event": "blocked", "summary": summary})
        self.ctx.analytics.record(task, action="blocked", from_status=from_status)
        self._stamp_provenance_outcome(task)

    # ------------------------------------------------------------- PR / review

    def _open_pull_request(self, task: Task, project: Project,
                           repos: list[Repository], repo: Repository, work: RepoWork,
                           response: TaskExecutionResponse,
                           review: TaskReviewResponse | None = None) -> bool:
        """Open one repo's PR (multi-repo-project spec §8.1). Returns True when
        the serial loop should continue (PR opened / no-op), False on a
        block/pause."""
        git = self.ctx.git_for(repo)
        workspace = git.workspace_for(task.id, repo.id)
        pushed = git.commit_and_push(
            workspace, work.branchName, f"{task.title}\n\nSprintBaton task {task.id}"
        )
        if not pushed:
            # No changes for THIS repo — a no-op repo, not a whole-task block
            # (multi-repo-project spec §8.1). The loop continues.
            work.status = RepoWorkStatus.NoOp
            return True

        # Test-merge against devBranch before any PR exists — a conflict is
        # resolved machine-only, never surfaced as a surprise on GitHub
        # (conflict-resolution spec §5.1).
        conflicted_files = git.merge_conflict_files(workspace, repo.devBranch)
        if conflicted_files:
            return self._resolve_conflict(task, project, repos, repo, work,
                                          response, review, conflicted_files)

        return self._finish_pull_request(task, project, repo, work, response, review)

    @_with_action("pr_opened")
    def _finish_pull_request(self, task: Task, project: Project, repo: Repository,
                             work: RepoWork,
                             response: TaskExecutionResponse,
                             review: TaskReviewResponse | None,
                             conflict_resolved: bool = False) -> bool:
        git = self.ctx.git_for(repo)
        from_status = task.status
        body = response.summary
        if review is not None:
            body += "\n\n**Reviewed by the SprintBaton Review Agent — approved.**"
            if review.findings:
                body += ("\n\nNon-blocking notes from the Review Agent:\n"
                         + "\n".join(f"- {f}" for f in review.findings))
        if conflict_resolved:
            body += (f"\n\n**A merge conflict against `{repo.devBranch}` was automatically "
                     f"resolved by the SprintBaton Conflict Resolution Agent** — please review "
                     f"the merge commit with extra care.")
        if task.mandatoryHumanReview:
            body += "\n\n**Mandatory human review** — this change touches an importance-gated surface."
        pr_url = git.open_pull_request(
            repo.githubRepo,
            head=work.branchName, base=repo.devBranch,
            title=task.title, body=body,
        )
        work.prUrl = pr_url
        work.status = RepoWorkStatus.PrOpen
        log.info("PR opened", extra={"event": "pr_opened", "pr_url": pr_url,
                                     "repo_id": repo.id})
        self.ctx.observer.task_processed(str(task.type), "pr_opened")
        self.ctx.analytics.record(task, action="pr_opened", from_status=from_status)
        return True

    @_with_action("conflict_resolution")
    def _resolve_conflict(self, task: Task, project: Project, repos: list[Repository],
                          repo: Repository, work: RepoWork,
                          response: TaskExecutionResponse,
                          review: TaskReviewResponse | None,
                          conflicted_files: list[str],
                          situation_report: str | None = None) -> bool:
        """A merge conflict against repo.devBranch was detected at PR-open time
        (conflict-resolution spec §5.3). Machine-only, per-repo. Returns True to
        continue the serial loop (resolved -> PR opened), False on pause/block."""
        git = self.ctx.git_for(repo)
        workspace = git.workspace_for(task.id, repo.id)
        work.conflictResolutionRounds += 1
        cap = self.ctx.settings.escalation.conflict_resolution_max_rounds

        if work.conflictResolutionRounds > cap:
            git.abort_merge(workspace)
            work.status = RepoWorkStatus.Blocked
            self._park_blocked(
                task, project,
                f"Merge conflict against {repo.devBranch} could not be reconciled after "
                f"{cap} attempt(s). Conflicting files: {', '.join(conflicted_files)}.",
                approval_required=True,
            )
            return False

        result = self.factory.conflict_resolution.run(
            task, project, repo, conflicted_files=conflicted_files,
            execution_summary=response.summary, situation_report=situation_report,
        )
        task.tokensSpent += result.tokensUsed
        self.ctx.analytics.record(task, action="conflict_resolution",
                                  from_status=TaskStatus.InProgress, response=result)

        if self._usage_limit_pause(task, project, action="conflict_resolution",
                                   response=result):
            # Deliberately not counted as a bounce round: nothing was attempted.
            work.conflictResolutionRounds -= 1
            return False

        if result.clarificationQuestion:
            self._ask_human(task, project, action="conflict_resolution",
                            question=result.clarificationQuestion,
                            options=result.clarificationOptions)
            return False

        if not result.resolved:
            git.abort_merge(workspace)
            cap_remaining = cap - work.conflictResolutionRounds
            if cap_remaining <= 0:
                work.status = RepoWorkStatus.Blocked
                self._park_blocked(
                    task, project,
                    result.summary or "Conflict resolution agent could not reconcile the branches.",
                    approval_required=True,
                )
                return False
            # Fresh dry-run merge on retry — never resume an agent-abandoned
            # partial resolution.
            conflicted_files = git.merge_conflict_files(workspace, repo.devBranch)
            return self._resolve_conflict(task, project, repos, repo, work,
                                          response, review, conflicted_files,
                                          situation_report=result.summary or None)

        # `git add -A` + commit finalizes the already-in-progress merge.
        git.commit_and_push(
            workspace, work.branchName,
            f"Merge {repo.devBranch} into {work.branchName} — SprintBaton conflict resolution",
        )
        return self._finish_pull_request(task, project, repo, work, response, review,
                                         conflict_resolved=True)

    def _review_reentry(self, task: Task, project: Project,
                        repos: list[Repository]) -> None:
        """Human PR comments bounced the task back (main spec step 6;
        multi-repo-project spec §8.2). Inspect each open PR's review comments and
        bounce only the commented repos back to execution; a repo whose PR is
        clean stays PrOpen. Each repo carries its own human-review bounce budget
        (escalation spec §5.6): within budget it re-executes at the same tier;
        once exhausted, a further bounce escalates that repo up the ladder."""
        open_works = [w for w in task.repoWork
                      if w.status == RepoWorkStatus.PrOpen and w.prUrl]
        bounced = []
        for work in open_works:
            repo = self._repo_by_id(repos, work.repoId)
            if repo is None:
                continue
            try:
                pr_number = self.ctx.git_for(repo).pr_number_from_url(work.prUrl)
                comments = (self.ctx.git_for(repo).list_pr_review_comments(
                    repo.githubRepo, pr_number) if pr_number else [])
            except Exception:
                comments = []
            if comments:
                bounced.append(work)
        # Reassignment with no per-PR comment we can see: conservatively bounce
        # every open PR (mirror the single-repo behavior — a reassignment means
        # the human wants changes).
        if not bounced:
            bounced = open_works
        for work in bounced:
            work.status = RepoWorkStatus.InProgress
            work.reviewFailures += 1

        cap = self.ctx.settings.escalation.human_review_max_rounds
        for work in bounced:
            repo = self._repo_by_id(repos, work.repoId)
            if repo is None:
                continue
            if work.reviewFailures > cap:
                signals = ExecutionSignals(review_failed=True)
                cont = self._apply_escalation(
                    task, project, repos, repo, work,
                    TaskExecutionResponse(taskId=task.id, summary="Review feedback received."),
                    signals, TriggerType.ReviewFailure)
                if not cont:
                    return
        # Re-run the serial loop over the bounced (now InProgress) repos.
        self._run_execution_loop(task, project, repos)

    # ---------------------------------------------------------------- shipping

    def _ship(self, task: Task, project: Project,
              repos: list[Repository]) -> None:
        """QA okayed (a task reassigned to the agent in the QA column): the
        sign-off covers the whole release-window batch — promote staging ->
        production per member repo once and fan Shipped out to every task in
        the batch (three-branch promotion spec §5; multi-repo-project spec §9).
        Repeat sign-offs are idempotent."""
        release = (self.ctx.release_repo.get(task.releaseId)
                   if task.releaseId else None)
        if release is None:
            # Pre-release-window task (or lost release): ship just this card.
            log.warning("QA sign-off on a task with no release; shipping solo",
                        extra={"task_id": task.id})
            for repo in repos:
                self.ctx.git_for(repo).promote(
                    repo.githubRepo,
                    from_branch=repo.stagingBranch, to_branch=repo.productionBranch,
                    title=f"Ship: {task.title}",
                )
            self._mark_shipped(task, project)
            return

        if release.status != ReleaseStatus.Shipped:
            for repo in repos:
                pr = self.ctx.git_for(repo).promote(
                    repo.githubRepo,
                    from_branch=repo.stagingBranch, to_branch=repo.productionBranch,
                    title=f"Ship {release.title}",
                )
                if pr:
                    release.shipPrUrls[repo.id] = pr
            release.status = ReleaseStatus.Shipped
            release.touch()
            self.ctx.release_repo.save(release)

        for task_id in release.taskIds:
            sibling = task if task_id == task.id else self.ctx.task_repo.get(task_id)
            if sibling is None or sibling.status == TaskStatus.Shipped:
                continue
            self._mark_shipped(sibling, project)
            if sibling is not task:  # the triggering task is saved by process()
                sibling.touch()
                self.ctx.task_repo.save(sibling)

    @_with_action("shipped")
    def _mark_shipped(self, task: Task, project: Project) -> None:
        from_status = task.status
        self._move_card(task, project, project.columns.shipped)
        self._set_status(task, TaskStatus.Shipped)
        # Nothing reads a Shipped task's clones again (workspace-mirrors-and-
        # cleanup spec §5.2); best-effort, the sweeper catches what this misses.
        self._remove_task_workspace(task)
        log.info("shipped", extra={"event": "shipped", "task_id": task.id,
                                   "release_id": task.releaseId or ""})
        self.ctx.observer.task_processed(str(task.type), "shipped")
        self.ctx.analytics.record(task, action="shipped", from_status=from_status)
        self._stamp_provenance_outcome(task)

    def _move_card(self, task: Task, project: Project, column: str, *,
                   force: bool = False) -> bool:
        """The one way the orchestrator moves a card (task-revisions spec
        §8.1, invariant 6)."""
        return move_card(self.ctx, task, project, column, force=force)

    def _remove_task_workspace(self, task: Task) -> None:
        remove = getattr(self.ctx.task_workspace, "remove_task", None)
        if callable(remove):
            remove(task)

    # ----------------------------------------------------------------- helpers

    def _repo_title(self, repos: list[Repository], repo_id: str) -> str:
        repo = self._repo_by_id(repos, repo_id)
        return repo.title if repo else repo_id

    def _record_note(self, task: Task, project: Project, role: str, text: str) -> None:
        """Durable, cross-role-visible reasoning trace (task-workspace spec
        §5.2) — without it, `text` only ever reaches an analytics event, which
        no other role's prompt or workspace ever reads back."""
        self.ctx.task_workspace.append_note(project.id, task, role=role, text=text)

    def _record_classification_provenance(self, task: Task, project: Project, *,
                                          action: str, response) -> None:
        """Snapshot the exact context a classification saw into the SprintBaton-
        owned provenance store and persist a ClassificationRecord referencing it
        (classification-provenance spec §5). Best-effort and fully guarded: a
        missing provenance service (the SimpleNamespace unit tests) or any
        failure never touches the task's control flow."""
        prov = getattr(self.ctx, "provenance", None)
        if prov is None:
            return
        try:
            metadata = self.factory.classification.metadata_summary(task, project)
            repos = self.ctx.repositories_for(project)
            upstream_sha = (self.ctx.git_for(repos[0]).remote_head_sha(
                repos[0].remoteUrl, repos[0].devBranch) if repos else "")
            prov.record_classification(
                project.id, task, project, action=action, response=response,
                metadata_summary=metadata, upstream_sha=upstream_sha)
        except Exception:
            log.warning("provenance snapshot failed",
                        extra={"task_id": task.id, "action": action})

    def _stamp_provenance_outcome(self, task: Task) -> None:
        """Join this task's ClassificationRecords to its terminal outcome
        (classification-provenance spec §4.2). Guarded like the write path."""
        prov = getattr(self.ctx, "provenance", None)
        if prov is None:
            return
        try:
            prov.stamp_outcome(task)
        except Exception:
            log.warning("provenance outcome stamp failed", extra={"task_id": task.id})

    def _current_plan(self, task: Task) -> str | None:
        if task.taskPlanningAction:
            return self.ctx.object_storage.get_text_by_url(task.taskPlanningAction)
        if task.taskFinalizationAction:
            return self.ctx.object_storage.get_text_by_url(task.taskFinalizationAction)
        return None

    def _route_to_human(self, task: Task, project: Project) -> None:
        """Hand the task back to the human (todoist-label-routing spec §2.2).
        How the provider represents "waiting on a human" is the adapter's own
        business — the orchestrator only expresses the behavior. The task drops
        out of the agent poll set until the human queues it back (Todoist: by
        re-applying the sprintbaton-agent label), at which point the next poll
        cycle re-enters the same dispatch arm."""
        try:
            self.ctx.task_adapter_for(project).route_to_human(task.externalId)
        except Exception:
            log.warning("could not route task to human", extra={"task_id": task.id})

    # ------------------------------------------------ project initialization
    # The metadata init pass as an invisible, queued Task (project-
    # initialization-task spec §5, §8). Nothing below ever touches a todolist
    # adapter: no task_adapter_for, _route_to_human, _ask_human, _park_blocked,
    # _sync_task_status, or move_task — an init task has no card.

    def _process_initialization(self, task: Task, project: Project,
                                repos: list[Repository]) -> None:
        if task.status != TaskStatus.ProjectInitialization:
            log.info("initialization run already finished",
                     extra={"status": str(task.status)})
            return
        try:
            self._initialize_project(task, project, repos)
        except Exception as e:
            # Kind-aware catch (spec §5.5): every exception of an init pass is
            # classified once — retried with backoff, or failed loudly.
            self._initialization_attempt_failed(task, project, e)

    @_with_action("project_initialization")
    def _initialize_project(self, task: Task, project: Project,
                            repos: list[Repository]) -> None:
        s = settings_for(self.ctx, task.userId)
        task.retryAfter = None
        if not task.metadataRepoProgress:
            # Filled once, on the first pass, from the members at that moment;
            # later passes (retries, resumes, crash recovery) reuse it and
            # skip Completed repos (spec §4.1).
            task.metadataRepoProgress = self._initial_progress(task, repos)
            task.touch()
            self.ctx.task_repo.save(task)

        members = {r.id: r for r in repos}
        for entry in task.metadataRepoProgress:
            if entry.status != "Pending":
                continue
            repo = members.get(entry.repoId)
            if repo is None or not repo.remoteUrl:
                entry.status = "Skipped"
                continue
            try:
                if not self._metadata_repo_pass(task, project, repo, entry, s):
                    return  # paused on a usage limit / walking a fallback chain
            except Exception as e:
                if classify_initialization_error(e) != TRANSIENT:
                    entry.status = "Failed"
                    entry.error = str(e)[:2000]
                raise

        # Fresh rows: the repo passes just moved their pointers.
        published = [r for r in self.ctx.repositories_for(project) if r.metadataRevision]
        if not published:
            raise NoMetadataProduced(
                "no member repository has published metadata — every repository "
                "was skipped for lack of a remoteUrl")
        self._metadata_project_pass(task, project, published, s)

    @staticmethod
    def _initial_progress(task: Task,
                          repos: list[Repository]) -> list[MetadataRepoProgress]:
        entries = []
        for repo in repos:
            skipped = (not repo.remoteUrl
                       or (task.metadataScope == MetadataScope.Missing
                           and repo.metadataRevision is not None))
            entries.append(MetadataRepoProgress(
                repoId=repo.id, status="Skipped" if skipped else "Pending"))
        return entries

    def _metadata_repo_pass(self, task: Task, project: Project, repo: Repository,
                            entry: MetadataRepoProgress, s) -> bool:
        """One repo's pass (spec §8.1): clone, seed from the current revision,
        run the agent (continuing / bouncing as needed), publish, swap.
        Returns False when the run paused."""
        if repo.metadataRevision == task.id:
            # This run already swapped the pointer and crashed before recording
            # it. Re-running would re-upload over the *current* revision, which
            # readers may be mid-way through — just record the completion.
            self._complete_repo_entry(task, entry, repo.metadataRevision)
            return True
        git = self.ctx.git_for(repo, project)
        # productionBranch: init describes shipped code (storage-layout §4.3).
        clone = git.prepare_init_workspace(repo.id, repo.remoteUrl, repo.productionBranch)
        metadata_dir = Path(clone) / ".sprintbaton"
        # Seeding discards any unvalidated edits of an interrupted attempt; the
        # per-attempt bounce budgets start over with it.
        self._reseed(metadata_dir,
                     lambda: self.ctx.task_workspace.materialize_repo_metadata(clone, repo))
        entry.validationRounds = entry.continuationRounds = 0

        response = self._run_metadata_agent(
            task, project, action="metadata_generation", rounds=entry,
            metadata_dir=metadata_dir,
            run=lambda report, continuation: self.factory.metadata_generation.run(
                task, project, repo, workspace=clone,
                situation_report=report, continuation=continuation),
            validate=lambda: validate_repo_metadata(metadata_dir), s=s)
        if response is None:
            return False

        result = self._metadata_publisher().publish_repo(repo, metadata_dir, task.id)
        self._complete_repo_entry(task, entry, result.revision, save=False)
        self.ctx.analytics.record(task, action="metadata_generation",
                                  from_status=TaskStatus.ProjectInitialization,
                                  response=response)
        self._record_note(task, project, "metadata_generation",
                          f"[{repo.title}] {response.summary}" if response.summary else "")
        log.info("metadata revision published", extra={
            "event": "metadata_revision_published", "scope": "repo",
            "repo_id": repo.id, "revision": result.revision, "files": result.files,
            "collected_revisions": result.collected})
        task.touch()
        self.ctx.task_repo.save(task)
        return True

    def _complete_repo_entry(self, task: Task, entry: MetadataRepoProgress,
                             revision: str, save: bool = True) -> None:
        entry.status = "Completed"
        entry.revision = revision
        entry.error = None
        entry.completedAt = now_millis()
        task.transientFailures = 0  # progress proves the dependencies are back
        if save:
            task.touch()
            self.ctx.task_repo.save(task)

    def _metadata_project_pass(self, task: Task, project: Project,
                               published: list[Repository], s) -> None:
        """The project-index pass (spec §8.2) — on every run, whatever the
        scope, since membership may have changed. Ends the run Shipped."""
        if project.metadataRevision == task.id:
            # Swapped, then crashed before the terminal status was saved.
            task.projectPassRevision = task.id
            task.transientFailures = 0
            self._set_status(task, TaskStatus.Shipped)
            return
        metadata_dir = project_init_metadata_dir(self.ctx.settings.workspace_root,
                                                 task.userId, project.id)
        # A first run starts empty: no fallback index is written here.
        self._reseed(metadata_dir, lambda: self.ctx.task_workspace.materialize_project_metadata(
            metadata_dir, project, published, write_fallback=False))
        rounds = MetadataRepoProgress(repoId=project.id)
        expected = [r.id for r in published]

        response = self._run_metadata_agent(
            task, project, action="project_metadata_generation", rounds=rounds,
            metadata_dir=metadata_dir,
            run=lambda report, continuation: self.factory.project_metadata_generation.run(
                task, project, published, metadata_dir=metadata_dir,
                situation_report=report, continuation=continuation),
            validate=lambda: validate_project_metadata(metadata_dir, expected), s=s)
        if response is None:
            return

        result = self._metadata_publisher().publish_project(project, metadata_dir, task.id)
        task.projectPassRevision = result.revision
        task.transientFailures = 0
        task.initializationError = None
        self._set_status(task, TaskStatus.Shipped)
        # A successful run's init/ is dead weight — the next run's clones come
        # from the local mirror (spec §5.3). A failed run keeps it to debug.
        remove_init = getattr(self.ctx.task_workspace, "remove_init", None)
        if callable(remove_init):
            remove_init(task.userId, project.id)
        # Recorded after the transition, so the final pass's event carries
        # toStatus=Shipped (spec §11).
        self.ctx.analytics.record(task, action="project_metadata_generation",
                                  from_status=TaskStatus.ProjectInitialization,
                                  response=response)
        self._record_note(task, project, "project_metadata_generation", response.summary)
        log.info("metadata revision published", extra={
            "event": "metadata_revision_published", "scope": "project",
            "project_id": project.id, "revision": result.revision,
            "files": result.files, "collected_revisions": result.collected,
            "first_initialization": result.first_initialization})

    def _run_metadata_agent(self, task: Task, project: Project, *, action: str,
                            rounds: MetadataRepoProgress, metadata_dir: Path,
                            run, validate, s):
        """Run one pass's agent to a validated directory (spec §8.1 steps 3-7).
        Returns the final response, or None when the run paused. Raises
        MetadataPassFailed once a continuation or validation budget is spent.

        Every intermediate run records its own TaskActionEvent, so token
        spend per chunk stays visible; the final one is recorded by the
        caller after publication."""
        report: str | None = None
        continuation = False
        from_status = TaskStatus.ProjectInitialization
        while True:
            response = run(report, continuation)
            task.tokensSpent += response.tokensUsed
            if response.usageLimitSignals and self._usage_limit_pause(
                    task, project, action=action, response=response):
                self.ctx.analytics.record(task, action=action,
                                          from_status=from_status, response=response)
                return None
            if response.clarificationQuestion:
                # No surface exists for a human reply (spec §5.8).
                log.warning("init pass asked a clarification question; ignored",
                            extra={"question": response.clarificationQuestion})

            if response.stopReason in ("turn_limit", "time_limit"):
                self.ctx.analytics.record(task, action=action,
                                          from_status=from_status, response=response)
                cap = s.sprintbaton_metadata_generation_max_continuations
                if rounds.continuationRounds >= cap:
                    raise MetadataPassFailed(
                        f"{action} was cut off by a {response.stopReason.replace('_', ' ')} "
                        f"after {cap} continuation round(s)")
                rounds.continuationRounds += 1
                # Not re-seeded: the directory holds the cut-off run's work.
                report, continuation = None, True
                log.info("init pass continued", extra={
                    "event": "initialization_continued", "action": action,
                    "stop_reason": response.stopReason,
                    "round": rounds.continuationRounds})
                task.touch()
                self.ctx.task_repo.save(task)
                continue
            if response.stopReason == "error":
                # The files decide: a run that errored but left a valid tree is
                # publishable, one that did not bounces like any violation.
                log.warning("init pass harness run ended in an error",
                            extra={"action": action})

            self._apply_metadata_removals(metadata_dir, response.removedFiles)
            validation = validate()
            if validation.warnings:
                log.info("metadata contract warnings",
                         extra={"action": action, "warnings": validation.warnings[:20]})
            if validation.ok:
                return response

            self.ctx.analytics.record(task, action=action,
                                      from_status=from_status, response=response)
            cap = s.sprintbaton_metadata_validation_max_rounds
            if rounds.validationRounds >= cap:
                raise MetadataPassFailed(
                    f"{action} still violates the metadata contract after {cap} "
                    f"bounce(s): " + "; ".join(validation.violations[:10]))
            rounds.validationRounds += 1
            # Not re-seeded: the agent fixes its own tree.
            report, continuation = validation.render(), False
            log.info("init pass bounced on contract violations", extra={
                "action": action, "round": rounds.validationRounds,
                "violations": validation.violations[:20]})
            task.touch()
            self.ctx.task_repo.save(task)

    @staticmethod
    def _reseed(directory: Path, materialize) -> None:
        shutil.rmtree(directory, ignore_errors=True)
        directory.mkdir(parents=True, exist_ok=True)
        materialize()

    @staticmethod
    def _apply_metadata_removals(root: Path, removed: list[str]) -> None:
        """Unlink each model-supplied removal inside the metadata directory
        (spec §8.1 step 6), rejecting any path that escapes it, then prune the
        directories that removal emptied."""
        base = Path(root).resolve()
        for raw in removed:
            target = (base / raw).resolve()
            if target == base or not target.is_relative_to(base):
                log.warning("rejected a metadata removal outside the directory",
                            extra={"path": raw})
                continue
            if not target.is_file():
                continue
            target.unlink()
            parent = target.parent
            while parent != base and parent.is_relative_to(base):
                try:
                    parent.rmdir()
                except OSError:
                    break
                parent = parent.parent

    def _metadata_publisher(self) -> MetadataRevisionPublisher:
        if getattr(self.ctx, "lock", None) is None:
            raise RuntimeError("metadata publication needs ServiceContext.lock")
        return MetadataRevisionPublisher(self.ctx.object_storage, self.ctx.repo_repo,
                                         self.ctx.project_repo, self.ctx.lock)

    def _initialization_attempt_failed(self, task: Task, project: Project,
                                       exc: Exception) -> None:
        """Classify an escaped exception (spec §5.6): schedule a backoff retry
        for a transient one, fail the run for anything else."""
        s = settings_for(self.ctx, task.userId)
        if classify_initialization_error(exc) == TRANSIENT:
            task.transientFailures += 1
            task.lastTransientError = f"{type(exc).__name__}: {exc}"[:2000]
            attempts = task.transientFailures
            if attempts > s.sprintbaton_init_retry_max_attempts:
                self._fail_initialization(
                    task, project,
                    f"gave up after {attempts} consecutive transient failures: "
                    f"{task.lastTransientError}")
                return
            delay = backoff_millis(attempts,
                                   base_seconds=s.sprintbaton_init_retry_base_seconds,
                                   max_seconds=s.sprintbaton_init_retry_max_backoff_seconds)
            # Status stays ProjectInitialization and the claim is released by
            # process()'s finally; the polling lane rediscovers the row once
            # retryAfter passes (spec §5.2).
            task.retryAfter = now_millis() + delay
            log.warning("initialization retry scheduled", extra={
                "event": "initialization_retry_scheduled", "attempt": attempts,
                "retry_after": task.retryAfter, "delay_seconds": round(delay / 1000),
                "error": task.lastTransientError})
            return
        if not isinstance(exc, (MetadataPassFailed, NoMetadataProduced)):
            log.error("initialization pass raised", exc_info=exc,
                      extra={"task_id": task.id})
        self._fail_initialization(task, project, f"{type(exc).__name__}: {exc}")

    @_with_action("blocked")
    def _fail_initialization(self, task: Task, project: Project | None,
                             reason: str) -> None:
        """The terminal failure path of an init run (spec §5.7) — Blocked with
        the reason on the row, surfaced through every channel that needs no
        board. The TaskPending gate stays closed."""
        from_status = task.status
        task.initializationError = reason[:4000]
        task.retryAfter = None
        self._set_status(task, TaskStatus.Blocked)
        log.error("project initialization failed", extra={
            "event": "initialization_failed", "task_id": task.id,
            "project_id": task.projectId,
            "trigger": str(task.initializationTrigger or ""),
            "error": task.initializationError,
            "attempts": task.transientFailures,
            "remedies": remedies_for(project) if project is not None else []})
        analytics = getattr(self.ctx, "analytics", None)
        if analytics is not None:
            analytics.record(task, action="blocked", from_status=from_status)

    def _report_awaiting_initialization(self, task: Task, project: Project) -> None:
        """A board task is gated (spec §5.7 channel 2): `warning` naming the
        error and remedies when the latest run failed (or none exists), `info`
        while one is pending — at most once per project per poll cycle."""
        interval = getattr(self.ctx.settings, "poll_interval_seconds", 60)
        now = time.monotonic()
        last = self._awaiting_logged.get(project.id)
        if last is not None and now - last < interval * 0.9:
            return
        self._awaiting_logged[project.id] = now
        latest = latest_initialization_task(self.ctx.task_repo, project)
        extra = {
            "event": "awaiting_project_initialization", "project_id": project.id,
            "initialization_task_id": latest.id if latest else None,
            "initialization_status": str(latest.status) if latest else "none",
        }
        if latest is None or latest.status == TaskStatus.Blocked:
            extra["error"] = (latest.initializationError if latest
                              else "no initialization run exists for this project")
            extra["remedies"] = remedies_for(project)
            log.warning("board tasks are waiting on project initialization",
                        extra=extra)
        else:
            log.info("board tasks are waiting for project initialization",
                     extra=extra)

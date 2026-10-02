"""TaskExecutionService — the Coding Model, the only role with write access.

One agent per escalation tier, resolved at construction (execution-tier-agents
spec §5): SPRINTBATON_EXECUTION_TIER_AGENTS overrides per tier, falling through
to SPRINTBATON_EXECUTION_AGENT (uniform), then the built-in Sonnet(E0-E2)/
Opus(E3)/Fable(E4) split. An IrreversibleOperationError from the workspace guard is
converted into the irreversible_attempt signal so the orchestrator can
hard-stop to EH.
"""

import logging
import time

from sprintbaton.entities.actions import TaskExecutionResponse
from sprintbaton.entities.enums import EscalationTier
from sprintbaton.entities.project import Project
from sprintbaton.entities.repository import Repository
from sprintbaton.entities.task import Task
from sprintbaton.escalation.triggers import ExecutionSignals
from sprintbaton.models.agents import TaskExecutionAgent
from sprintbaton.models.guard import IrreversibleOperationError
from sprintbaton.services.adapters import TaskExecutionRequestAdapter
from sprintbaton.services.base import ServiceContext, TaskActionService

log = logging.getLogger(__name__)


class TaskExecutionService(TaskActionService):
    action_name = "execution"

    def __init__(self, ctx: ServiceContext):
        super().__init__(ctx)
        self._adapter = TaskExecutionRequestAdapter()

    def _agent_for(self, task: Task, tier: EscalationTier) -> TaskExecutionAgent:
        # Per-run, keyed by task.userId and availability-filtered so a tier's
        # fallback chain is honored (agent-fallback spec §4) and a hosted
        # user's own per-tier wiring applies.
        cfg = self.ctx.agents.resolve_execution_tier(tier, task.userId)
        return TaskExecutionAgent(
            harness=cfg.harness, model=cfg.model,
            prompt=self.ctx.prompts.action_prompt(
                cfg.prompt_name, system=cfg.system_prompt_name),
            subscription_auth=cfg.subscription_auth,
        )

    def run(self, task: Task, project: Project, repo: Repository, *,
            tier: EscalationTier = EscalationTier.E0,
            situation_report: str | None = None,
            work=None,
            **kwargs) -> tuple[TaskExecutionResponse, ExecutionSignals]:
        agent = self._agent_for(task, tier)
        work = work or task.work_for(repo.id)
        branch = (work.branchName if work and work.branchName
                  else f"sprintbaton/{task.id}/{repo.id}")
        if work is not None:
            work.branchName = branch
        workspace = self.ctx.git_for(repo).prepare_workspace(
            task.id, repo.remoteUrl, repo.devBranch, branch, repo_id=repo.id
        )
        self.ctx.task_workspace.materialize(workspace, project.id, task)
        self.ctx.task_workspace.materialize_repo_metadata(workspace, repo)

        plan = spec = criteria = None
        if task.taskPlanningAction:
            plan = self.ctx.object_storage.get_text_by_url(task.taskPlanningAction)
        if task.taskFinalizationAction:
            spec = self.ctx.object_storage.get_text_by_url(task.taskFinalizationAction)
        if task.taskPassingCriteriaAction:
            criteria = self.ctx.object_storage.get_text_by_url(task.taskPassingCriteriaAction)

        # Clarification continuity (conversation-lifecycle spec §9.1): the
        # workspace is keyed by task.id and prepare_workspace reuses an
        # in-progress checkout, so both the resume and the restart branch pick
        # the paused work back up on disk.
        conversation, reply_text, clarification_context = self._begin_turn(task, project)
        request = self._adapter.adapt(
            task, repo,
            self.metadata_summary(task, project, repo=repo,
                                  workspace_path=str(workspace)),
            workspace_path=str(workspace),
            model_id=agent.model.model_id,
            plan=plan, spec=spec, passing_criteria=criteria,
            situation_report=situation_report,
            **self._turn_kwargs(conversation, reply_text, clarification_context),
            **self._amendment_kwargs(task),
        )

        started = time.monotonic()
        signals = ExecutionSignals()
        try:
            response = agent.execute(request)
        except IrreversibleOperationError as e:
            log.warning("irreversible operation blocked", extra={
                "task_id": task.id, "command": e.command, "reason": e.reason,
            })
            response = TaskExecutionResponse(
                taskId=task.id, completed=False,
                summary=f"Blocked: attempted irreversible operation ({e.reason}): `{e.command}`",
            )
            signals.irreversible_attempt = True

        self._end_turn(conversation, response)
        signals.file_edit_counts = response.filesEdited
        signals.consecutive_check_failures = response.consecutiveCheckFailures
        signals.asked_question = response.askedQuestion is not None
        signals.plan_broken = response.planBroken
        signals.importance_flags = response.importanceFlags
        signals.tier_tokens_used = response.tokensUsed
        signals.tier_wall_clock_seconds = time.monotonic() - started
        signals.total_tokens_used = task.tokensSpent + response.tokensUsed

        self.ctx.observer.tokens_used(
            "coding", request.modelId, response.inputTokens, response.outputTokens
        )
        return response, signals

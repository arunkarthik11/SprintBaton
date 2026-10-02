"""TaskConflictResolutionService — resolves a PR-open-time merge conflict
against repo.devBranch on the task's own branch (conflict-resolution spec §7.3).

Never dispatched from process()'s top-level match — the role has no TaskStatus
of its own (spec §3); it is only ever called directly from the orchestrator's
_resolve_conflict, which also records the analytics event (matching
_review_code's pattern of the orchestrator recording, not the service).
"""

import logging

from sprintbaton.entities.actions import TaskConflictResolutionResponse
from sprintbaton.entities.project import Project
from sprintbaton.entities.repository import Repository
from sprintbaton.entities.task import Task
from sprintbaton.models.agents import TaskConflictResolutionAgent
from sprintbaton.services.adapters import TaskConflictResolutionRequestAdapter
from sprintbaton.services.base import ServiceContext, TaskActionService

log = logging.getLogger(__name__)


class TaskConflictResolutionService(TaskActionService):
    action_name = "conflict_resolution"

    def __init__(self, ctx: ServiceContext):
        super().__init__(ctx)
        self._adapter = TaskConflictResolutionRequestAdapter()

    def _agent_for(self, task: Task) -> TaskConflictResolutionAgent:
        # Per call, keyed by the task's own owner (auth-mode-resolution spec
        # §4.1). The former __init__-time process identity meant a write-capable
        # role resolved its agent — and now its auth mode — against the wrong
        # user; being write-capable is no longer a reason to skip per-user
        # routing, only a reason for the conformance gate.
        resolved = self.ctx.agents.resolve("conflict_resolution", task.userId)
        return TaskConflictResolutionAgent(
            harness=resolved.harness,
            model=resolved.model,
            prompt=self.ctx.prompts.action_prompt(
                resolved.prompt_name, system=resolved.system_prompt_name),
            subscription_auth=resolved.subscription_auth,
        )

    def run(self, task: Task, project: Project, repo: Repository, *,
            conflicted_files: list[str] | None = None,
            execution_summary: str = "",
            situation_report: str | None = None,
            **kwargs) -> TaskConflictResolutionResponse:
        # Runs inside execution's own workspace, already mid-merge from the
        # dry run (spec §5.2) — same reuse the review role already does.
        agent = self._agent_for(task)
        workspace = self.ctx.git_for(repo).workspace_for(task.id, repo.id)
        self.ctx.task_workspace.materialize(workspace, project.id, task)
        criteria = None
        if task.taskPassingCriteriaAction:
            criteria = self.ctx.object_storage.get_text_by_url(task.taskPassingCriteriaAction)
        conversation, reply_text, clarification_context = self._begin_turn(task, project)
        request = self._adapter.adapt(
            task, repo,
            self.metadata_summary(task, project, repo=repo,
                                  workspace_path=str(workspace)),
            workspace_path=str(workspace),
            conflicted_files=conflicted_files or [],
            execution_summary=execution_summary,
            passing_criteria=criteria,
            situation_report=situation_report,
            **self._turn_kwargs(conversation, reply_text, clarification_context),
        )
        response = agent.execute(request)
        self.ctx.observer.tokens_used(
            "conflict_resolution", agent.model.model_id,
            response.inputTokens, response.outputTokens,
        )
        self._end_turn(conversation, response)
        log.info("conflict resolution complete",
                 extra={"task_id": task.id, "resolved": response.resolved})
        return response

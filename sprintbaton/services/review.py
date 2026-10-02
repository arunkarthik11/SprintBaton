"""TaskReviewService — reviews the diff before the PR advances."""

import logging

from sprintbaton.entities.actions import TaskReviewResponse
from sprintbaton.entities.project import Project
from sprintbaton.entities.repository import Repository
from sprintbaton.entities.task import Task
from sprintbaton.models.agents import TaskReviewAgent
from sprintbaton.services.adapters import TaskReviewRequestAdapter
from sprintbaton.services.base import ServiceContext, TaskActionService

log = logging.getLogger(__name__)


class TaskReviewService(TaskActionService):
    action_name = "review"

    def __init__(self, ctx: ServiceContext):
        super().__init__(ctx)
        self._adapter = TaskReviewRequestAdapter()

    def _agent_for(self, task: Task) -> TaskReviewAgent:
        # Per-run, keyed by task.userId (claude-code-cli harness spec §4.5).
        resolved = self.ctx.agents.resolve("review", task.userId)
        return TaskReviewAgent(
            harness=resolved.harness,
            model=resolved.model,
            prompt=self.ctx.prompts.action_prompt(
                resolved.prompt_name, system=resolved.system_prompt_name),
            subscription_auth=resolved.subscription_auth,
        )

    def run(self, task: Task, project: Project, repo: Repository, *,
            diff: str = "", **kwargs) -> TaskReviewResponse:
        agent = self._agent_for(task)
        workspace = self.ctx.git_for(repo).workspace_for(task.id, repo.id)
        # Review reuses execution's workspace — re-materialize to pick up
        # notes.md/review-comments.md written since execution's own call
        # (task-workspace spec §6.3).
        self.ctx.task_workspace.materialize(workspace, project.id, task)
        criteria = None
        if task.taskPassingCriteriaAction:
            criteria = self.ctx.object_storage.get_text_by_url(task.taskPassingCriteriaAction)
        conversation, reply_text, clarification_context = self._begin_turn(task, project)
        request = self._adapter.adapt(
            task, repo,
            self.metadata_summary(task, project, repo=repo,
                                  workspace_path=str(workspace)),
            str(workspace), diff,
            passing_criteria=criteria,
            **self._turn_kwargs(conversation, reply_text, clarification_context),
        )
        response = agent.execute(request)
        self.ctx.observer.tokens_used(
            "review", agent.model.model_id, response.inputTokens, response.outputTokens
        )
        self._end_turn(conversation, response)
        log.info("review complete", extra={"task_id": task.id, "approved": response.approved})
        return response

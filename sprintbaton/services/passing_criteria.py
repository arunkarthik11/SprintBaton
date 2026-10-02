"""TaskPassingCriteriaService — the Passing Criteria checkpoint
(finalization-passing-criteria spec §6): from the task's specification alone
(the finalized spec when one exists, otherwise the raw title/description),
enumerate exhaustive acceptance criteria before any planning or execution
begins. Read-only, single-shot."""

import logging

from sprintbaton.entities.actions import TaskPassingCriteriaResponse
from sprintbaton.entities.project import Project
from sprintbaton.entities.repository import Repository
from sprintbaton.entities.task import Task
from sprintbaton.models.agents import TaskPassingCriteriaAgent
from sprintbaton.services.adapters import TaskPassingCriteriaRequestAdapter
from sprintbaton.services.base import ServiceContext, TaskActionService

log = logging.getLogger(__name__)


class TaskPassingCriteriaService(TaskActionService):
    action_name = "passing_criteria"

    def __init__(self, ctx: ServiceContext):
        super().__init__(ctx)
        self._adapter = TaskPassingCriteriaRequestAdapter()

    def _agent_for(self, task: Task) -> TaskPassingCriteriaAgent:
        # Per-run, keyed by task.userId (claude-code-cli harness spec §4.5).
        resolved = self.ctx.agents.resolve("passing_criteria", task.userId)
        return TaskPassingCriteriaAgent(
            harness=resolved.harness,
            model=resolved.model,
            prompt=self.ctx.prompts.action_prompt(
                resolved.prompt_name, system=resolved.system_prompt_name),
            subscription_auth=resolved.subscription_auth,
        )

    def run(self, task: Task, project: Project,
            repos: list[Repository] | None = None, **kwargs) -> TaskPassingCriteriaResponse:
        agent = self._agent_for(task)
        spec_text = ""
        if task.taskFinalizationAction:
            spec_text = self.ctx.object_storage.get_text_by_url(
                task.taskFinalizationAction) or ""
        if not spec_text:
            spec_text = f"{task.title}\n\n{task.description or ''}".strip()
        conversation, reply_text, clarification_context = self._begin_turn(task, project)
        request = self._adapter.adapt(
            task, project, self.metadata_summary(task, project), spec_text,
            **self._turn_kwargs(conversation, reply_text, clarification_context),
            **self._amendment_kwargs(task),
        )
        response = agent.execute(request)
        self.ctx.observer.tokens_used(
            "passing_criteria", agent.model.model_id,
            response.inputTokens, response.outputTokens,
        )
        self._end_turn(conversation, response)
        log.info("passing criteria enumerated", extra={
            "task_id": task.id, "criteria_count": len(response.criteria),
        })
        return response

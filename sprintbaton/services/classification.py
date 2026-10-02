"""TaskClassificationService — the Router."""

import logging

from sprintbaton.entities.actions import TaskClassificationResponse
from sprintbaton.entities.project import Project
from sprintbaton.entities.repository import Repository
from sprintbaton.entities.task import Task
from sprintbaton.models.agents import TaskClassificationAgent
from sprintbaton.services.adapters import TaskClassificationRequestAdapter
from sprintbaton.services.base import ServiceContext, TaskActionService

log = logging.getLogger(__name__)


class TaskClassificationService(TaskActionService):
    # Deliberately not integrated with the clarification/Conversation
    # lifecycle: the Router stays a cheap, fully autonomous first triage step
    # (conversation-lifecycle spec §13 resolution).
    action_name = "classification"

    def __init__(self, ctx: ServiceContext):
        super().__init__(ctx)
        self._adapter = TaskClassificationRequestAdapter()

    def _agent_for(self, task: Task) -> TaskClassificationAgent:
        # Per call, keyed by the task's own owner (auth-mode-resolution spec
        # §4.1). The former __init__-time resolution used the process identity,
        # which in hosted mode is LOCAL_USER_ID — so a hosted user's own
        # AgentDefinition was unfindable, their UserConfiguration overrides and
        # credentials never applied, and auth mode could not be derived from
        # the real owner.
        resolved = self.ctx.agents.resolve("classification", task.userId)
        return TaskClassificationAgent(
            harness=resolved.harness,
            model=resolved.model,
            prompt=self.ctx.prompts.action_prompt(
                resolved.prompt_name, system=resolved.system_prompt_name),
            subscription_auth=resolved.subscription_auth,
        )

    def run(self, task: Task, project: Project,
            repos: list[Repository] | None = None, **kwargs) -> TaskClassificationResponse:
        agent = self._agent_for(task)
        request = self._adapter.adapt(task, project, self.metadata_summary(task, project))
        response = agent.execute(request)
        self.ctx.observer.tokens_used(
            "router", agent.model.model_id, response.inputTokens, response.outputTokens
        )
        log.info("task classified", extra={
            "task_id": task.id, "category": response.category,
            "importance": response.importanceFlags,
        })
        return response

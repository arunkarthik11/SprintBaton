"""TaskPlanClassificationService — the Plan Classification checkpoint
(classification-taxonomy spec §5): once a plan is finalized, a 2-way verdict —
Simple (Sonnet) or Compound (Opus) execution."""

import logging

from sprintbaton.entities.actions import TaskPlanClassificationResponse
from sprintbaton.entities.enums import PlanClassificationVerdict
from sprintbaton.entities.project import Project
from sprintbaton.entities.repository import Repository
from sprintbaton.entities.task import Task
from sprintbaton.models.agents import TaskPlanClassificationAgent
from sprintbaton.services.adapters import TaskPlanClassificationRequestAdapter
from sprintbaton.services.base import ServiceContext, TaskActionService

log = logging.getLogger(__name__)


class TaskPlanClassificationService(TaskActionService):
    action_name = "plan_classification"

    def __init__(self, ctx: ServiceContext):
        super().__init__(ctx)
        self._adapter = TaskPlanClassificationRequestAdapter()

    def _agent_for(self, task: Task) -> TaskPlanClassificationAgent:
        # Per call, keyed by the task's own owner (auth-mode-resolution spec
        # §4.1) — the __init__-time process identity could not see a hosted
        # user's own AgentDefinition, nor derive auth mode from the real owner.
        resolved = self.ctx.agents.resolve("plan_classification", task.userId)
        return TaskPlanClassificationAgent(
            harness=resolved.harness,
            model=resolved.model,
            prompt=self.ctx.prompts.action_prompt(
                resolved.prompt_name, system=resolved.system_prompt_name),
            subscription_auth=resolved.subscription_auth,
        )

    def run(self, task: Task, project: Project,
            repos: list[Repository] | None = None, **kwargs) -> TaskPlanClassificationResponse:
        # A task that skipped clarification (Complex, or Simple floored to E2
        # by importance) has no finalized spec — the raw task description on
        # the request stands in (implementability spec §4.2).
        finalized_spec = ""
        if task.taskFinalizationAction:
            finalized_spec = self.ctx.object_storage.get_text_by_url(
                task.taskFinalizationAction) or ""
        plan = ""
        if task.taskPlanningAction:
            plan = self.ctx.object_storage.get_text_by_url(task.taskPlanningAction) or ""
        agent = self._agent_for(task)
        conversation, reply_text, clarification_context = self._begin_turn(task, project)
        request = self._adapter.adapt(
            task, project, self.metadata_summary(task, project), finalized_spec, plan,
            **self._turn_kwargs(conversation, reply_text, clarification_context),
        )
        try:
            response = agent.execute(request)
        except ValueError as e:
            # Malformed classifier output: bias toward the safer branch
            # (implementability spec §12) rather than under-scoping.
            log.warning("plan classification output malformed; defaulting to Compound",
                        extra={"task_id": task.id, "error": str(e)})
            response = TaskPlanClassificationResponse(
                taskId=task.id, verdict=PlanClassificationVerdict.Compound,
                rationale=f"Classifier output unusable ({e}); defaulted to the safer branch.",
                modelId=agent.model.model_id,
                promptId=agent.prompt.prompt_id,
            )
            self._end_turn(conversation, response)
            return response
        self.ctx.observer.tokens_used(
            "plan_classifier", agent.model.model_id,
            response.inputTokens, response.outputTokens,
        )
        self._end_turn(conversation, response)
        log.info("finalized plan classified", extra={
            "task_id": task.id, "verdict": str(response.verdict),
        })
        return response

"""TaskSpecClassificationService — the Spec Classification checkpoint
(classification-taxonomy spec §4): once a spec is finalized, a 3-way verdict —
Simple (Sonnet direct), Compound (Opus direct), or Complex (needs a plan)."""

import logging

from sprintbaton.entities.actions import TaskSpecClassificationResponse
from sprintbaton.entities.enums import SpecClassificationVerdict
from sprintbaton.entities.project import Project
from sprintbaton.entities.repository import Repository
from sprintbaton.entities.task import Task
from sprintbaton.models.agents import TaskSpecClassificationAgent
from sprintbaton.services.adapters import TaskSpecClassificationRequestAdapter
from sprintbaton.services.base import ServiceContext, TaskActionService

log = logging.getLogger(__name__)


class TaskSpecClassificationService(TaskActionService):
    action_name = "spec_classification"

    def __init__(self, ctx: ServiceContext):
        super().__init__(ctx)
        self._adapter = TaskSpecClassificationRequestAdapter()

    def _agent_for(self, task: Task) -> TaskSpecClassificationAgent:
        # Per call, keyed by the task's own owner (auth-mode-resolution spec
        # §4.1) — the __init__-time process identity could not see a hosted
        # user's own AgentDefinition, nor derive auth mode from the real owner.
        resolved = self.ctx.agents.resolve("spec_classification", task.userId)
        return TaskSpecClassificationAgent(
            harness=resolved.harness,
            model=resolved.model,
            prompt=self.ctx.prompts.action_prompt(
                resolved.prompt_name, system=resolved.system_prompt_name),
            subscription_auth=resolved.subscription_auth,
        )

    def run(self, task: Task, project: Project,
            repos: list[Repository] | None = None, **kwargs) -> TaskSpecClassificationResponse:
        finalized_spec = ""
        if task.taskFinalizationAction:
            finalized_spec = self.ctx.object_storage.get_text_by_url(
                task.taskFinalizationAction) or ""
        agent = self._agent_for(task)
        conversation, reply_text, clarification_context = self._begin_turn(task, project)
        request = self._adapter.adapt(
            task, project, self.metadata_summary(task, project), finalized_spec,
            **self._turn_kwargs(conversation, reply_text, clarification_context),
        )
        try:
            response = agent.execute(request)
        except ValueError as e:
            # Malformed classifier output: bias toward the safer branch
            # (implementability spec §12) rather than under-scoping.
            log.warning("spec classification output malformed; defaulting to Complex",
                        extra={"task_id": task.id, "error": str(e)})
            response = TaskSpecClassificationResponse(
                taskId=task.id, verdict=SpecClassificationVerdict.Complex,
                rationale=f"Classifier output unusable ({e}); defaulted to the safer branch.",
                modelId=agent.model.model_id,
                promptId=agent.prompt.prompt_id,
            )
            self._end_turn(conversation, response)
            return response
        self.ctx.observer.tokens_used(
            "spec_classifier", agent.model.model_id,
            response.inputTokens, response.outputTokens,
        )
        self._end_turn(conversation, response)
        log.info("finalized spec classified", extra={
            "task_id": task.id, "verdict": str(response.verdict),
        })
        return response

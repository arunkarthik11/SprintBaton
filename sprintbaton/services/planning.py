"""TaskPlanningService — the Planning Model (Opus tier).

Also the E2 escalation target: with a situation report, it produces a corrected
plan and hands control back to the Coding Model for the mechanical work."""

import logging

from sprintbaton.entities.actions import TaskPlanningResponse
from sprintbaton.entities.project import Project
from sprintbaton.entities.repository import Repository
from sprintbaton.entities.task import Task
from sprintbaton.models.agents import TaskPlanningAgent
from sprintbaton.services.adapters import TaskPlanningRequestAdapter
from sprintbaton.storage.keys import revision_filename
from sprintbaton.services.base import (
    ServiceContext,
    TaskActionService,
    read_only_workspaces,
)

log = logging.getLogger(__name__)


class TaskPlanningService(TaskActionService):
    action_name = "planning"

    def __init__(self, ctx: ServiceContext):
        super().__init__(ctx)
        self._adapter = TaskPlanningRequestAdapter()

    def _agent_for(self, task: Task) -> TaskPlanningAgent:
        # Resolved fresh per run, keyed by the task's own userId (claude-code-
        # cli harness spec §4.5) — per-user overrides and the admin-tier
        # default can never be decided from a process-wide cache.
        resolved = self.ctx.agents.resolve("planning", task.userId)
        return TaskPlanningAgent(
            harness=resolved.harness,
            model=resolved.model,
            prompt=self.ctx.prompts.action_prompt(
                resolved.prompt_name, system=resolved.system_prompt_name),
            subscription_auth=resolved.subscription_auth,
        )

    def run(self, task: Task, project: Project,
            repos: list[Repository] | None = None,
            situation_report: str | None = None, **kwargs) -> TaskPlanningResponse:
        agent = self._agent_for(task)
        repos = repos or []
        conversation, reply_text, clarification_context = self._begin_turn(task, project)
        # Clone every member repo read-only and materialize the project index,
        # then carry the resulting paths into the prompt as a location guide —
        # the index prose deliberately states none (storage-layout spec §10 q4).
        workspace_path, guide = read_only_workspaces(
            self.ctx, agent, task, project, repos)
        spec = criteria = ""
        if task.taskFinalizationAction:
            spec = self.ctx.object_storage.get_text_by_url(
                task.taskFinalizationAction) or ""
        if task.taskPassingCriteriaAction:
            criteria = self.ctx.object_storage.get_text_by_url(
                task.taskPassingCriteriaAction) or ""
        request = self._adapter.adapt(
            task, project,
            self.metadata_summary(task, project, location_guide=guide),
            situation_report,
            workspace_path=workspace_path,
            finalized_spec=spec, passing_criteria=criteria,
            **self._turn_kwargs(conversation, reply_text, clarification_context),
            **self._amendment_kwargs(task),
        )
        response = agent.execute(request)
        self.ctx.observer.tokens_used(
            "planning", agent.model.model_id, response.inputTokens, response.outputTokens
        )
        self._end_turn(conversation, response)

        if response.clarificationQuestion:
            # Paused on a tradeoff question — no plan to persist this turn.
            return response
        key = self.ctx.object_storage.task_key(
            task.userId, project.id, task.id, "artifacts",
            revision_filename("plan.md", task.currentRevision))
        task.taskPlanningAction = self.ctx.object_storage.put_text(key, response.plan)
        task.artifactRevisions["planning"] = task.currentRevision
        log.info("plan produced", extra={"task_id": task.id, "replan": bool(situation_report)})
        return response

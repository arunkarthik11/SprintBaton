"""TaskRepoScopingService — the machine-only, column-less determination of
which member repos a task touches (multi-repo-project spec §7.3), run once
before the serial per-repo execution loop.

Single-repo projects short-circuit to the one repo with no model call. For
multi-repo projects a single_shot agent reads the spec/plan (or raw
description) + the candidate repos and returns the affected subset; malformed
or empty output falls back to *all* member repos (the safe over-set — an
unaffected repo just produces a no-op no-PR result)."""

import logging

from sprintbaton.entities.actions import TaskRepoScopingResponse
from sprintbaton.entities.project import Project
from sprintbaton.entities.repository import Repository
from sprintbaton.entities.task import Task
from sprintbaton.models.agents import TaskRepoScopingAgent
from sprintbaton.services.adapters import TaskRepoScopingRequestAdapter
from sprintbaton.services.base import ServiceContext, TaskActionService

log = logging.getLogger(__name__)


class TaskRepoScopingService(TaskActionService):
    action_name = "repo_scoping"

    def __init__(self, ctx: ServiceContext):
        super().__init__(ctx)
        self._adapter = TaskRepoScopingRequestAdapter()

    def _agent_for(self, task: Task) -> TaskRepoScopingAgent:
        resolved = self.ctx.agents.resolve("repo_scoping", task.userId)
        return TaskRepoScopingAgent(
            harness=resolved.harness,
            model=resolved.model,
            prompt=self.ctx.prompts.action_prompt(
                resolved.prompt_name, system=resolved.system_prompt_name),
            subscription_auth=resolved.subscription_auth,
        )

    def run(self, task: Task, project: Project,
            repos: list[Repository] | None = None, **kwargs) -> TaskRepoScopingResponse:
        repos = repos or []
        all_ids = [r.id for r in repos]
        # Single-repo (the common case): no model call needed.
        if len(repos) <= 1:
            return TaskRepoScopingResponse(
                taskId=task.id, affectedRepoIds=all_ids,
                rationale="single-repo project" if all_ids else "no member repos")

        spec_text = ""
        if task.taskFinalizationAction:
            spec_text = self.ctx.object_storage.get_text_by_url(
                task.taskFinalizationAction) or ""
        if not spec_text:
            spec_text = f"{task.title}\n\n{task.description or ''}".strip()
        plan = ""
        if task.taskPlanningAction:
            plan = self.ctx.object_storage.get_text_by_url(task.taskPlanningAction) or ""

        agent = self._agent_for(task)
        request = self._adapter.adapt(
            task, project, self.metadata_summary(task, project),
            spec_text=spec_text, plan=plan,
            candidate_repos=[{"repoId": r.id, "title": r.title, "role": r.role}
                             for r in repos],
        )
        try:
            response = agent.execute(request)
        except (ValueError, Exception) as e:  # noqa: BLE001 — fall back safely
            log.warning("repo scoping failed; defaulting to all repos",
                        extra={"task_id": task.id, "error": str(e)})
            return TaskRepoScopingResponse(
                taskId=task.id, affectedRepoIds=all_ids,
                rationale=f"scoping failed ({e}); defaulted to all repos")

        if not response.affectedRepoIds:
            log.info("repo scoping returned empty set; defaulting to all repos",
                     extra={"task_id": task.id})
            response.affectedRepoIds = all_ids
        self.ctx.observer.tokens_used(
            "repo_scoping", agent.model.model_id,
            response.inputTokens, response.outputTokens)
        log.info("repos scoped", extra={
            "task_id": task.id, "affected": response.affectedRepoIds})
        return response

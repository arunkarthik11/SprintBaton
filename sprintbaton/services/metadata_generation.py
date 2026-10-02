"""The metadata init pass as two resolved task actions
(docs/project-initialization-task-spec.md §6, §8).

`TaskMetadataGenerationService` runs one per-repo pass over an init clone whose
`.sprintbaton/` the orchestrator has already seeded from the current revision;
`TaskProjectMetadataGenerationService` runs the project-index pass over the
seeded project metadata directory. Both resolve their agent per call, keyed by
the task owner, like every other role — so per-user AgentDefinitions,
credentials, fallback chains, and subscription auth all apply.

Neither is ever dispatched from a TaskStatus, and neither touches a todolist
adapter: an init task has no card, so there is no clarification conversation
(spec §5.8) and no board move. The orchestrator owns seeding, continuation,
validation, publication, and analytics; these services own one agent run.
"""

import logging
from pathlib import Path

from sprintbaton.entities.actions import (
    MetadataGenerationRequest,
    MetadataGenerationResponse,
    ProjectMetadataGenerationRequest,
    ProjectMetadataGenerationResponse,
)
from sprintbaton.entities.project import Project
from sprintbaton.entities.repository import Repository
from sprintbaton.entities.task import Task
from sprintbaton.models.agents import (
    TaskMetadataGenerationAgent,
    TaskProjectMetadataGenerationAgent,
)
from sprintbaton.services.base import TaskActionService, settings_for
from sprintbaton.workspace.locations import (
    init_project_location_guide,
    init_repo_location_guide,
)

log = logging.getLogger(__name__)

# Each member repo's current `info` index is carried into the project pass's
# prompt, capped per repo (spec §8.2).
MAX_REPO_INFO_CHARS = 4000


class TaskMetadataGenerationService(TaskActionService):
    action_name = "metadata_generation"

    def _agent_for(self, task: Task) -> TaskMetadataGenerationAgent:
        resolved = self.ctx.agents.resolve(self.action_name, task.userId)
        # No metadata layer — the chained metadata.md teaches a role how to
        # *read* metadata for a task, which is not this role's job. That
        # rationale governs the metadata layer only, so the agent's own system
        # preamble still applies (agent-system-prompt spec §4.7).
        return TaskMetadataGenerationAgent(
            harness=resolved.harness,
            model=resolved.model,
            prompt=self.ctx.prompts.compose(
                resolved.prompt_name, system=resolved.system_prompt_name,
                with_metadata=False),
            subscription_auth=resolved.subscription_auth,
        )

    def run(self, task: Task, project: Project, repo: Repository, *,
            workspace: Path | str, situation_report: str | None = None,
            continuation: bool = False, **kwargs) -> MetadataGenerationResponse:
        agent = self._agent_for(task)
        clone = Path(workspace)
        metadata_dir = clone / ".sprintbaton"
        s = settings_for(self.ctx, task.userId)
        request = MetadataGenerationRequest(
            taskId=task.id,
            userId=task.userId,
            repoId=repo.id,
            workspacePath=str(clone),
            writableRoot=str(metadata_dir),
            repoTitle=repo.title,
            repoRole=repo.role,
            locationGuide=init_repo_location_guide(repo, clone, metadata_dir),
            metadataGuidance=task.metadataGuidance,
            situationReport=situation_report,
            continuation=continuation,
            maxTurns=s.sprintbaton_metadata_generation_max_turns,
        )
        response = agent.execute(request)
        self.ctx.observer.tokens_used(
            self.action_name, agent.model.model_id,
            response.inputTokens, response.outputTokens)
        log.info("metadata generation run finished", extra={
            "task_id": task.id, "repo_id": repo.id,
            "stop_reason": response.stopReason,
        })
        return response


class TaskProjectMetadataGenerationService(TaskActionService):
    action_name = "project_metadata_generation"

    def _agent_for(self, task: Task) -> TaskProjectMetadataGenerationAgent:
        resolved = self.ctx.agents.resolve(self.action_name, task.userId)
        return TaskProjectMetadataGenerationAgent(
            harness=resolved.harness,
            model=resolved.model,
            # metadata layer omitted for the same reason as the per-repo pass
            prompt=self.ctx.prompts.compose(
                resolved.prompt_name, system=resolved.system_prompt_name,
                with_metadata=False),
            subscription_auth=resolved.subscription_auth,
        )

    def run(self, task: Task, project: Project, repos: list[Repository], *,
            metadata_dir: Path | str, situation_report: str | None = None,
            continuation: bool = False, **kwargs) -> ProjectMetadataGenerationResponse:
        agent = self._agent_for(task)
        s = settings_for(self.ctx, task.userId)
        request = ProjectMetadataGenerationRequest(
            taskId=task.id,
            userId=task.userId,
            repoId=project.id,
            workspacePath=str(metadata_dir),
            projectTitle=project.title,
            repoSummaries=self.repo_summaries(repos),
            locationGuide=init_project_location_guide(metadata_dir),
            metadataGuidance=task.metadataGuidance,
            situationReport=situation_report,
            continuation=continuation,
            maxTurns=s.sprintbaton_metadata_generation_max_turns,
        )
        response = agent.execute(request)
        self.ctx.observer.tokens_used(
            self.action_name, agent.model.model_id,
            response.inputTokens, response.outputTokens)
        log.info("project metadata generation run finished", extra={
            "task_id": task.id, "stop_reason": response.stopReason,
        })
        return response

    def repo_summaries(self, repos: list[Repository]) -> str:
        """Every member repo's id/title/role/remote plus its current published
        `info` (spec §8.2) — the only view of the repos this pass gets."""
        parts = []
        for repo in repos:
            info = ""
            if repo.metadataUrl:
                info = (self.ctx.object_storage.get_text_by_url(repo.metadataUrl)
                        or "")[:MAX_REPO_INFO_CHARS]
            indented = "\n".join(f"    {line}" for line in info.splitlines())
            parts.append(
                f"- id: {repo.id}\n"
                f"  title: {repo.title}\n"
                f"  role: {repo.role or 'unspecified'}\n"
                f"  remote: {repo.githubRepo or repo.remoteUrl or '(none)'}\n"
                f"  published metadata: "
                f"{'revision ' + repo.metadataRevision if repo.metadataRevision else 'none'}\n"
                f"  metadata index:\n{indented or '    (no per-repo metadata published)'}")
        return "\n\n".join(parts) or "(no member repositories)"

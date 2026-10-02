"""TaskRevisionClassificationService — the Revision Classification role
(docs/task-revisions-and-board-driven-workflow-spec.md §7).

A card's title/description changed after SprintBaton had derived work from the
old text. A cheap router-tier call names the **rewind point**: the earliest
stage whose output the edit invalidates. The orchestrator's reconcile pass
decides what to do with it (auto-rewind before execution, ask the human after
— §8.6); this service only asks the question. On the factory, never in
`_by_status`: it has no TaskStatus of its own.
"""

import difflib
import logging

from sprintbaton.entities.actions import TaskRevisionClassificationResponse
from sprintbaton.entities.enums import RewindPoint
from sprintbaton.entities.project import Project
from sprintbaton.entities.repository import Repository
from sprintbaton.entities.task import Task
from sprintbaton.entities.task_revision import TaskRevision
from sprintbaton.models.agents import TaskRevisionClassificationAgent
from sprintbaton.services.adapters import TaskRevisionClassificationRequestAdapter
from sprintbaton.services.base import ServiceContext, TaskActionService

log = logging.getLogger(__name__)

ARTIFACT_CHARS = 6000


def content_diff(previous: TaskRevision, new: TaskRevision) -> str:
    """A unified diff of two revisions' title + description (§7.3)."""
    def lines(rev: TaskRevision) -> list[str]:
        return [f"Title: {rev.title}", "", *(rev.description or "").splitlines()]

    return "\n".join(difflib.unified_diff(
        lines(previous), lines(new),
        fromfile=f"revision {previous.revision}", tofile=f"revision {new.revision}",
        lineterm=""))


class TaskRevisionClassificationService(TaskActionService):
    action_name = "revision_classification"

    def __init__(self, ctx: ServiceContext):
        super().__init__(ctx)
        self._adapter = TaskRevisionClassificationRequestAdapter()

    def _agent_for(self, task: Task) -> TaskRevisionClassificationAgent:
        # Per call, keyed by the task's own owner, like every role.
        resolved = self.ctx.agents.resolve(self.action_name, task.userId)
        return TaskRevisionClassificationAgent(
            harness=resolved.harness,
            model=resolved.model,
            prompt=self.ctx.prompts.action_prompt(
                resolved.prompt_name, system=resolved.system_prompt_name),
            subscription_auth=resolved.subscription_auth,
        )

    def _artifact(self, task: Task, url: str | None, action: str) -> str:
        if not url:
            return ""
        text = self.ctx.object_storage.get_text_by_url(url) or ""
        stamp = task.artifactRevisions.get(action)
        header = f"(built from revision {stamp})\n" if stamp else ""
        return header + text[:ARTIFACT_CHARS]

    @staticmethod
    def _repo_summary(task: Task) -> str:
        return "\n".join(
            f"- {w.repoId}: status {w.status}, branch "
            f"{'exists' if w.branchName else 'none'}, PR {w.prUrl or 'not open'}"
            for w in task.repoWork)

    def run(self, task: Task, project: Project,
            repos: list[Repository] | None = None, *,
            previous: TaskRevision, new: TaskRevision,
            guidance: str | None = None, open_question: str | None = None,
            **kwargs) -> TaskRevisionClassificationResponse:
        agent = self._agent_for(task)
        conversation, reply_text, clarification_context = self._begin_turn(task, project)
        spec_action = ("abstract_finalization" if "abstract_finalization"
                       in task.artifactRevisions else "finalization")
        request = self._adapter.adapt(
            task, project, self.metadata_summary(task, project),
            previous=previous, new=new, diff=content_diff(previous, new),
            finalized_spec=self._artifact(task, task.taskFinalizationAction, spec_action),
            passing_criteria=self._artifact(
                task, task.taskPassingCriteriaAction, "passing_criteria"),
            plan=self._artifact(task, task.taskPlanningAction, "planning"),
            repo_summary=self._repo_summary(task),
            open_question=open_question, guidance=guidance,
            **self._turn_kwargs(conversation, reply_text, clarification_context),
        )
        try:
            response = agent.execute(request)
        except ValueError as e:
            # Malformed output: the safe, expensive branch (spec §7.4).
            log.warning("revision classification output malformed; defaulting to "
                        "Classification", extra={"task_id": task.id, "error": str(e)})
            response = TaskRevisionClassificationResponse(
                taskId=task.id, rewindTo=RewindPoint.Classification,
                changeSummary="the card's description changed",
                rationale=f"Classifier output unusable ({e}); redoing from the start.",
                modelId=agent.model.model_id, promptId=agent.prompt.prompt_id)
        self.ctx.observer.tokens_used(
            "revision_classifier", agent.model.model_id,
            response.inputTokens, response.outputTokens)
        self._end_turn(conversation, response)
        log.info("revision classified", extra={
            "event": "revision_classified", "task_id": task.id,
            "revision": new.revision, "rewind_to": str(response.rewindTo)})
        return response

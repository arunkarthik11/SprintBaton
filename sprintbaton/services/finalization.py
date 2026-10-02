"""TaskFinalizationService — the Spec Model loop (the shared Ambiguous/Abstract
flow, and the E1 lateral escalation target). The clarification mechanism is one
loop, but the agent answering it differs by category: Abstract resolves the
`abstract_finalization` action (Opus by default) instead of `finalization`
(finalization-passing-criteria spec §5.2)."""

import logging

from sprintbaton.entities.actions import TaskFinalizationResponse
from sprintbaton.entities.enums import TaskType
from sprintbaton.entities.project import Project
from sprintbaton.entities.repository import Repository
from sprintbaton.entities.task import Task
from sprintbaton.models.agents import TaskFinalizationAgent
from sprintbaton.services.adapters import TaskFinalizationRequestAdapter
from sprintbaton.storage.keys import revision_filename
from sprintbaton.services.base import (
    ServiceContext,
    TaskActionService,
    read_only_workspaces,
)

log = logging.getLogger(__name__)


class TaskFinalizationService(TaskActionService):
    action_name = "finalization"  # Abstract tasks resolve via action_for()

    def __init__(self, ctx: ServiceContext):
        super().__init__(ctx)
        self._adapter = TaskFinalizationRequestAdapter()

    def action_for(self, task: Task) -> str:
        # Non-Ambiguous/Abstract tasks only reach _clarify via the
        # DiscoveredAmbiguity lateral escalation — they get the generic agent.
        return ("abstract_finalization" if task.type == TaskType.Abstract
                else "finalization")

    def _agent_for(self, task: Task) -> TaskFinalizationAgent:
        # Per-run, keyed by task.userId (claude-code-cli harness spec §4.5) —
        # the category still picks which action resolves (finalization-
        # passing-criteria spec §5.2), now per call instead of a dict-of-two.
        resolved = self.ctx.agents.resolve(self.action_for(task), task.userId)
        return TaskFinalizationAgent(
            harness=resolved.harness,
            model=resolved.model,
            prompt=self.ctx.prompts.action_prompt(
                resolved.prompt_name, system=resolved.system_prompt_name),
            subscription_auth=resolved.subscription_auth,
        )

    def run(self, task: Task, project: Project,
            repos: list[Repository] | None = None, **kwargs) -> TaskFinalizationResponse:
        agent = self._agent_for(task)
        repos = repos or []
        # This role's whole loop is clarification: its `questions` list is the
        # pause signal, so the shared Conversation machinery tracks those
        # turns instead of a clarificationQuestion (conversation-lifecycle
        # spec §13 resolution). The comment thread stays this role's own prior
        # context — clarificationContext would duplicate it, so it is not
        # threaded here (spec §4.3 note).
        conversation, reply_text, _ = self._begin_turn(
            task, project, action=self.action_for(task))
        prior_qa = self._render_comment_thread(task, project)
        # Clone every member repo read-only and materialize the project index,
        # then carry the resulting paths into the prompt as a location guide —
        # the index prose deliberately states none (storage-layout spec §10 q4).
        workspace_path, guide = read_only_workspaces(
            self.ctx, agent, task, project, repos)
        request = self._adapter.adapt(
            task, project,
            self.metadata_summary(task, project, location_guide=guide), prior_qa,
            workspace_path=workspace_path,
            conversation_id=(conversation.harnessSessionId
                             if reply_text is not None else None),
            reply_text=reply_text,
            **self._amendment_kwargs(task, self.action_for(task)),
        )
        response = agent.execute(request)
        self.ctx.observer.tokens_used(
            "spec", agent.model.model_id, response.inputTokens, response.outputTokens
        )
        open_questions = None if response.finalizedSpec else (
            "\n".join(response.questions) or None)
        self._end_turn(conversation, response, question=open_questions)

        if response.finalizedSpec:
            # Revision-suffixed, so a rerun after a card edit keeps the
            # previous version (task-revisions spec §5.4)
            key = self.ctx.object_storage.task_key(
                task.userId, project.id, task.id, "artifacts",
                revision_filename("finalized-spec.md", task.currentRevision))
            task.taskFinalizationAction = self.ctx.object_storage.put_text(
                key, response.finalizedSpec
            )
            task.artifactRevisions[self.action_for(task)] = task.currentRevision
            log.info("task finalized", extra={"task_id": task.id})
        return response

    def _render_comment_thread(self, task: Task, project: Project) -> str:
        comments = self.ctx.task_adapter_for(project).list_comments(task.externalId)
        return "\n\n".join(f"- {c.body}" for c in comments)

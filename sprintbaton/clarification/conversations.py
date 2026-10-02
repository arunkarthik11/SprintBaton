"""ConversationRunner — the shared human-pause lifecycle every TaskActionService
uses (conversation-lifecycle spec §7): when a role ends a turn with a
clarification question, the episode is tracked as a Conversation entity, and
the next turn for that (task, action) either *resumes* the harness session
(fast human reply, session id available) or *restarts* with the prior Q&A
concatenated into the prompt (slow reply, or a harness that can't resume).

Extends this package's charter from "render provider-facing text"
(ClarificationTranslator) to owning the whole pause lifecycle. The
resume/restart decision lives here exactly once — services only thread the
resolved (conversation_id, reply_text, clarification_context) triple into
their requests.
"""

import logging

from sprintbaton.adaptors.base import ExternalComment, TaskAdapter
from sprintbaton.clarification.translator import AGENT_COMMENT_HEADERS
from sprintbaton.entities.base import now_millis
from sprintbaton.entities.clarification import ClarificationOptions, match_reply
from sprintbaton.entities.message import Conversation
from sprintbaton.entities.task import Task
from sprintbaton.storage.base import EntityDAO
from sprintbaton.storage.base import BlobStore

log = logging.getLogger(__name__)

# The fixed, harness-agnostic continuation nudge for a session resumed after a
# usage-limit pause (usage-limit-aware execution spec §8.2) — threaded through
# as reply_text purely so _agentic's existing `conversation_id and reply_text`
# resume gate is satisfied without changing its shape.
RESUME_AFTER_USAGE_LIMIT_MESSAGE = (
    "Your previous turn was interrupted by a usage limit; the limit has now "
    "reset. Continue exactly where you left off.")


class ConversationRunner:
    def __init__(self, conversation_repo: EntityDAO[Conversation],
                 object_storage: BlobStore,
                 resume_window_seconds: int, agent_user_id: str):
        self._repo = conversation_repo
        self._object_storage = object_storage
        self._window_millis = resume_window_seconds * 1000
        self._agent_user_id = agent_user_id

    # ------------------------------------------------------------------ public

    def resolve(self, task: Task, action: str, project: str,
                task_adapter: TaskAdapter
                ) -> tuple[Conversation, str | None, str | None]:
        """Returns (conversation, reply_text, clarification_context) — exactly
        one of the last two is non-None, or both are None on a task's very
        first turn for this action (spec §7.1). The adapter is passed per call
        because it is now resolved per repo, with per-repo credentials
        (repository-onboarding spec §4.3)."""
        conversation = self._latest_open(task, action)
        if conversation is not None and conversation.pausedForUsageLimitUntil is not None:
            # A wake-job re-entry, not a detected reply (usage-limit-aware
            # execution spec §8.2): the trigger condition is structurally
            # different from the reply-driven flow below, so it lives in its
            # own method — delegated to here so every service's _begin_turn
            # keeps working unchanged.
            return self.resolve_after_usage_limit_pause(task, action, project)
        if conversation is not None:
            # Evaluated before the transcript append below touches
            # modifiedTime, or the resume window would never expire.
            resumable = self._resumable(conversation)
            reply = self._new_reply_since(task, conversation, task_adapter)
            if reply is not None:
                # The one place reply-to-option matching happens
                # (clarification-options spec §6): best-effort, stamped on the
                # episode; a non-match leaves both fields None and replyText
                # stays the authority.
                if conversation.pendingOptions is not None:
                    conversation.selectedAnswer, conversation.selectedAnswers = (
                        match_reply(conversation.pendingOptions, reply))
                # Recorded before any close so a restart's rendered prior
                # context always contains the human's answer, even when the
                # reply arrived after the resume window expired.
                self._append_transcript(conversation, "Human reply", reply)
                log.info("human replied", extra={
                    "event": "clarification_resumed", "action": action,
                    "branch": "resume" if resumable else "restart",
                })
            if resumable:
                return conversation, reply, None
            reason = ("resume_window_expired" if conversation.harnessSessionId
                      else "superseded_by_new_episode")
            self.close(conversation, reason)
        prior = self._render_prior(task, action)
        conversation = self._start(task, action, project)
        return conversation, None, prior

    def resolve_after_usage_limit_pause(self, task: Task, action: str, project: str
                                        ) -> tuple[Conversation, str | None, str | None]:
        """Called (via resolve()'s delegation) when the dispatch arm re-enters
        after a usage-limit pause rather than a fresh call or a human reply
        (usage-limit-aware execution spec §8.2). Resumes the session
        unconditionally when one exists — the wake job only re-enqueues once
        the recorded reset time has passed, so there is nothing further to
        gate on; otherwise degrades to the ordinary restart branch, the exact
        graceful-degradation shape _resumable already uses for harnesses that
        never emit a session id."""
        conversation = self._latest_open(task, action)
        if conversation is not None and conversation.harnessSessionId is not None:
            conversation.pausedForUsageLimitUntil = None
            conversation.touch()
            self._repo.save(conversation)
            log.info("resuming after usage-limit pause", extra={
                "event": "usage_limit_resumed", "task_id": task.id,
                "action": action, "branch": "resume"})
            return conversation, RESUME_AFTER_USAGE_LIMIT_MESSAGE, None
        if conversation is not None:
            self.close(conversation, "usage_limit_restart")
        prior = self._render_prior(task, action)
        conversation = self._start(task, action, project)
        log.info("restarting after usage-limit pause", extra={
            "event": "usage_limit_resumed", "task_id": task.id,
            "action": action, "branch": "restart"})
        return conversation, None, prior

    def mark_usage_limit_pause(self, task: Task, action: str, until: int | None) -> None:
        """Stamp the latest open episode for (task, action) as paused on a
        usage limit (usage-limit-aware execution spec §8) so the wake-job
        re-entry takes resolve_after_usage_limit_pause instead of the
        reply-driven flow. No-op when no episode is open (e.g. a role whose
        turn never recorded one) — the re-entry then simply starts fresh."""
        conversation = self._latest_open(task, action)
        if conversation is None:
            return
        conversation.pausedForUsageLimitUntil = until
        conversation.touch()
        self._repo.save(conversation)

    def record_turn(self, conversation: Conversation, *,
                    harness_session_id: str | None, turn_text: str,
                    pending_options: ClarificationOptions | None = None) -> None:
        conversation.turnCount += 1
        if harness_session_id:
            conversation.harnessSessionId = harness_session_id
        # Always assigned, so a turn that pauses without options clears any
        # stale ones from an earlier round (clarification-options spec §6).
        conversation.pendingOptions = pending_options
        self._append_transcript(
            conversation, f"Agent turn {conversation.turnCount}", turn_text)

    def open_question(self, task: Task, action: str, project: str,
                      question: str,
                      options: ClarificationOptions | None = None) -> Conversation:
        """Open an episode for a question the orchestrator itself asks — not a
        role's harness turn — so the reply can be matched against `options`
        later (task-revisions spec §8.6, the late-edit decision). Any earlier
        open episode for (task, action) is closed first."""
        self.close_open(task, action, "superseded_by_new_episode")
        conversation = self._start(task, action, project)
        self.record_turn(conversation, harness_session_id=None,
                         turn_text=question, pending_options=options)
        return conversation

    def take_reply(self, task: Task, action: str, task_adapter: TaskAdapter
                   ) -> tuple[str | None, str | None]:
        """The human's reply to an `open_question` episode, if one arrived:
        returns (reply_text, matched_option) and closes the episode as
        answered. (None, None) while no reply has been posted — the episode
        stays open. The option match is best-effort, exactly as in resolve()."""
        conversation = self._latest_open(task, action)
        if conversation is None:
            return None, None
        reply = self._new_reply_since(task, conversation, task_adapter)
        if reply is None:
            return None, None
        selected = None
        if conversation.pendingOptions is not None:
            conversation.selectedAnswer, conversation.selectedAnswers = (
                match_reply(conversation.pendingOptions, reply))
            selected = conversation.selectedAnswer
        self._append_transcript(conversation, "Human reply", reply)
        self.close(conversation, reason="answered")
        return reply, selected

    def close_open(self, task: Task, action: str, reason: str) -> None:
        """Close the latest open episode for (task, action), if any — e.g.
        "clarify_cap_reached" when the finalization loop parks the task."""
        conversation = self._latest_open(task, action)
        if conversation is not None:
            self.close(conversation, reason)

    def close(self, conversation: Conversation, reason: str) -> None:
        conversation.closed = True
        conversation.closeReason = reason
        conversation.touch()
        self._repo.save(conversation)
        log.info("conversation closed", extra={
            "conversation_id": conversation.id, "task_id": conversation.taskId,
            "action": conversation.action, "reason": reason,
        })

    # ----------------------------------------------------------------- internal

    def _start(self, task: Task, action: str, project: str) -> Conversation:
        conversation = Conversation(
            userId=task.userId,
            taskId=task.id, action=action,
            participants=[p for p in (self._agent_user_id, task.createdBy) if p],
        )
        key = self._object_storage.task_key(
            task.userId, project, task.id, "agent", f"{conversation.id}.md")
        conversation.transcriptUrl = self._object_storage.url_for(key)
        self._repo.save(conversation)
        return conversation

    def _latest_open(self, task: Task, action: str) -> Conversation | None:
        open_conversations = self._repo.find(
            {"taskId": task.id, "action": action, "closed": False,
             "userId": task.userId})
        if not open_conversations:
            return None
        return max(open_conversations, key=lambda c: c.modifiedTime)

    def _resumable(self, conversation: Conversation) -> bool:
        """Self-healing across harness configurations: single_shot /
        raw_tool_loop never populate a session id, so roles on those harnesses
        always take the restart branch with no per-role config (spec §7.1)."""
        return (not conversation.closed
                and conversation.harnessSessionId is not None
                and now_millis() - conversation.modifiedTime <= self._window_millis)

    def _new_reply_since(self, task: Task, conversation: Conversation,
                         task_adapter: TaskAdapter) -> str | None:
        """Human comments posted after the conversation's last recorded turn.
        Relies on the TaskAdapter contract: list_comments returns every comment
        with postedAtMillis populated, ordered oldest-first (spec §13)."""
        comments = task_adapter.list_comments(task.externalId)
        replies = [c.body for c in comments
                   if c.postedAtMillis > conversation.modifiedTime
                   and not self._from_agent(c)]
        return "\n\n".join(replies) or None

    def _from_agent(self, comment: ExternalComment) -> bool:
        if comment.authorId:
            return comment.authorId == self._agent_user_id
        # No author id from the provider: recognize the agent's own comments
        # by their fixed headers instead.
        return comment.body.startswith(AGENT_COMMENT_HEADERS)

    def _render_prior(self, task: Task, action: str) -> str | None:
        """The restart branch's concatenated prior context: the most recent
        prior episode's transcript for this (task, action), if any."""
        conversations = self._repo.find(
            {"taskId": task.id, "action": action, "userId": task.userId})
        for conversation in sorted(conversations, key=lambda c: c.modifiedTime,
                                   reverse=True):
            if conversation.turnCount == 0 or not conversation.transcriptUrl:
                continue
            transcript = self._object_storage.get_text_by_url(
                conversation.transcriptUrl)
            if transcript:
                return transcript
        return None

    def _append_transcript(self, conversation: Conversation, heading: str,
                           text: str) -> None:
        url = conversation.transcriptUrl
        key = self._object_storage.key_for(url) if url else None
        if key is not None:
            existing = self._object_storage.get_text_by_url(url) or ""
            self._object_storage.put_text(
                key, f"{existing}## {heading}\n\n{text}\n\n")
        conversation.touch()
        self._repo.save(conversation)

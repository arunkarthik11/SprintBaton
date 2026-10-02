"""TaskActionEventRecorder — the long-horizon analytics stack.

Complements the Observer (current health, OTel metrics): the recorder persists a
TaskActionEvent document for every task state change so capability reporting and
experiments can be built later. Each event is stamped with the versions of
everything that shaped the outcome (prompt id/version, model id, metadata format,
app version) and the token usage consumed in that state.
"""

import logging

import sprintbaton
from sprintbaton.entities.actions import TaskActionResponse
from sprintbaton.entities.enums import TaskStatus, TriggerType
from sprintbaton.entities.events import TaskActionEvent
from sprintbaton.entities.task import Task
from sprintbaton.entities.usage import TokenUsage
from sprintbaton.metadata.format import METADATA_FORMAT_VERSION
from sprintbaton.prompts.registry import PROMPT_VERSION
from sprintbaton.storage.base import EntityDAO

log = logging.getLogger(__name__)

# userId joined taskId/modelId with the reporting layer — every report query
# filters on it first (token-usage-reporting spec §5).
INDEXED_FIELDS = ("taskId", "modelId", "userId")


class TaskActionEventRecorder:
    def __init__(self, event_repo: EntityDAO[TaskActionEvent]):
        self._repo = event_repo
        self._repo.ensure_indexes(*INDEXED_FIELDS)

    def record(
        self,
        task: Task,
        *,
        action: str,
        from_status: TaskStatus,
        response: TaskActionResponse | None = None,
        trigger: TriggerType | None = None,
        duration_millis: int = 0,
    ) -> TaskActionEvent:
        """Persist one event for a state change; `task.status` is the to-status.

        `response` carries the token usage and model/prompt identity of the
        action that caused the transition; pure board moves (pr_opened, shipped,
        blocked) pass no response and record zero usage.
        """
        event = TaskActionEvent(
            userId=task.userId,
            taskId=task.id,
            repoId=task.repoId,
            action=action,
            fromStatus=from_status,
            toStatus=task.status,
            taskType=task.type,
            escalationTier=task.escalationTier,
            trigger=trigger,
            modelId=response.modelId if response else "",
            promptId=response.promptId if response else "",
            promptVersion=PROMPT_VERSION,
            metadataVersion=METADATA_FORMAT_VERSION,
            sprintbatonVersion=sprintbaton.__version__,
            usage=response.usage if response else TokenUsage(),
            durationMillis=duration_millis,
            # getattr, not attribute access: a pure board move passes no
            # response at all, and callers may pass a duck-typed stand-in.
            # "Guarded" is the safe default — only a harness that actually
            # measured otherwise sets it False.
            guardrailEnforced=getattr(response, "guardrailEnforced", True),
            revision=task.currentRevision,
            round=task.round,
            amended=action in task.amendments,
        )
        self._repo.save(event)
        log.info("task action event recorded", extra={
            "task_id": task.id, "action": action, "event_id": event.id,
            "from_status": str(from_status), "to_status": str(task.status),
            "model_id": event.modelId, "tokens": event.usage.totalTokens,
        })
        return event

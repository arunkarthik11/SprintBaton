"""TaskActionEvent — the long-horizon analytics stream.

One document is persisted per task state change (and per model action taken while
in a state). Unlike the Observer's OTel metrics, which monitor current health,
these events are the durable base for capability reporting and experimentation:
each carries the versions of everything that produced the outcome (prompt, model,
metadata format, app version) plus the token usage consumed in that state.

Indexed on taskId and modelId (see TaskActionEventRecorder).
"""

from pydantic import Field

from sprintbaton.entities.base import LOCAL_USER_ID, BaseEntity
from sprintbaton.entities.enums import EscalationTier, TaskStatus, TaskType, TriggerType
from sprintbaton.entities.usage import TokenUsage


class TaskActionEvent(BaseEntity):
    # Tenant owner (user-multitenancy spec §6), inherited from the task
    userId: str = LOCAL_USER_ID
    taskId: str = ""
    repoId: str = ""
    # Which action produced this transition: classification | finalization |
    # planning | execution | review | pr_opened | shipped | blocked
    action: str = ""
    fromStatus: TaskStatus | None = None
    toStatus: TaskStatus | None = None
    taskType: TaskType | None = None
    escalationTier: EscalationTier | None = None
    trigger: TriggerType | None = None  # escalation trigger fired by this action, if any

    # Experimentation dimensions — versions of everything that shaped the outcome
    modelId: str = ""            # "" for transitions without a model call
    promptId: str = ""           # versioned prompt id, e.g. "execution@v1"
    promptVersion: str = ""      # prompt registry version, e.g. "v1"
    metadataVersion: str = ""    # .sprintbaton metadata format version
    sprintbatonVersion: str = ""

    usage: TokenUsage = Field(default_factory=TokenUsage)
    durationMillis: int = 0
    # Whether guard.py's checks actually executed for the harness run behind
    # this event (subprocess-cli-write-parity spec §7.3). False only on a
    # probe-failed CLI harness, which runs unguarded by explicit product
    # decision — recorded here so the question is answerable after the fact.
    guardrailEnforced: bool = True
    # The card revision the task was working from, the card's round, and
    # whether this action reran a stage over its own previous output
    # (task-revisions spec §12) — what separates first-pass work from rework,
    # so the cost of card edits and board moves is measurable.
    revision: int = 1
    round: int = 1
    amended: bool = False

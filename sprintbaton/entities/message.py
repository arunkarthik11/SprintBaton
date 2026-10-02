from pydantic import Field

from sprintbaton.entities.base import LOCAL_USER_ID, BaseEntity
from sprintbaton.entities.clarification import ClarificationOptions


class Message(BaseEntity):
    fromUserId: str = ""
    toUserId: str = ""
    parentMessageId: str | None = None
    childrenMessageIds: list[str] = Field(default_factory=list)
    message: str | None = None      # inline body; empty if too big
    messageUrl: str | None = None   # object-storage URL for a large message


class Conversation(BaseEntity):
    """One clarification episode of a task-action role: the same-role thread a
    paused agent resumes (or restarts from) once a human answers
    (docs/conversation-lifecycle-spec.md §4.1). Identity is (taskId, action);
    never resumed across tiers/roles — see escalation spec §7."""

    # Tenant owner (user-multitenancy spec §6), inherited from the task
    userId: str = LOCAL_USER_ID
    taskId: str = ""
    action: str = ""                     # AgentDefinitionResolver action name
    harnessSessionId: str | None = None  # set only by resume-capable harnesses
    transcriptUrl: str | None = None     # object-storage URL (§5 key scheme)
    turnCount: int = 0
    closed: bool = False
    closeReason: str | None = None       # "answered" | "resume_window_expired" |
                                         # "clarify_cap_reached" | "superseded_by_new_episode"
    participants: list[str] = Field(default_factory=list)
    # Structured options offered by the turn that paused this episode
    # (clarification-options spec §6) — persisted so the async reply can be
    # matched against them even across a process restart. selectedAnswer(s)
    # record how the latest human reply mapped onto them, or stay None when it
    # didn't (replyText remains authoritative).
    pendingOptions: ClarificationOptions | None = None
    selectedAnswer: str | None = None
    selectedAnswers: list[str] | None = None
    # Set when this episode's harness session is being kept alive across a
    # usage-limit pause rather than a human-clarification pause (usage-limit-
    # aware execution spec §8). Modeled as its own field rather than
    # overloading closeReason: a usage-limit pause never "closes" the episode
    # the way a clarification episode closes on "answered".
    pausedForUsageLimitUntil: int | None = None

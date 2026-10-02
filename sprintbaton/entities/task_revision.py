"""Card observations (docs/task-revisions-and-board-driven-workflow-spec.md §4-§5).

The poller only *observes*: it writes these two collections and never the
`Task` row a running `process()` holds in memory. The orchestrator reconciles
the observation against the task at the start of every `process()`. Because
the poller and the worker write disjoint documents, the lost-update race
between them is gone structurally rather than by locking (spec §4).
"""

import hashlib

from pydantic import BaseModel, Field

from sprintbaton.entities.base import LOCAL_USER_ID, BaseEntity, now_millis


def normalize_content(text: str | None) -> str:
    """Line endings to `\\n`, trailing whitespace stripped per line, leading
    and trailing blank lines stripped (spec §5.2) — so a whitespace-only edit
    never creates a revision."""
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    lines = [line.rstrip() for line in lines]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def content_fingerprint(title: str | None, description: str | None) -> str:
    """sha256 of the normalized card content (spec §5.2). Labels and priority
    are deliberately not content."""
    payload = normalize_content(title) + "\n\x00\n" + normalize_content(description)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class TaskRevision(BaseEntity):
    """One version of a round's card content — append-only, never updated,
    unique on (taskId, revision) (spec §5.1). A revert (A -> B -> A) is three
    revisions: the history records what the human did."""

    userId: str = LOCAL_USER_ID
    taskId: str = ""
    revision: int = 1
    title: str = ""
    description: str | None = None
    fingerprint: str = ""
    observedAt: int = Field(default_factory=now_millis)


class CardSnapshot(BaseEntity):
    """What the card looks like right now — one row per round, upserted by the
    poller (spec §5.1). Its `id` is the round's Task.id, so the orchestrator
    reads it with a plain `get`."""

    userId: str = LOCAL_USER_ID
    taskId: str = ""
    columnId: str = ""
    fingerprint: str = ""
    # The observed content, so a new round (spec §9.2) starts from what the
    # card says now without another provider call.
    title: str = ""
    description: str | None = None
    labels: list[str] | None = None
    priority: int | None = None
    observedAt: int = Field(default_factory=now_millis)


def latest_revision(revision_repo, task) -> TaskRevision | None:
    """The newest recorded revision of a round (equality-only find, max in
    Python — the persistence contract has no ordering)."""
    revisions = revision_repo.find({"taskId": task.id, "userId": task.userId})
    return max(revisions, key=lambda r: r.revision) if revisions else None


def revision_of(revision_repo, task, number: int) -> TaskRevision | None:
    return revision_repo.find_one(
        {"taskId": task.id, "userId": task.userId, "revision": number})


class AmendmentContext(BaseModel):
    """Why a stage is rerunning over its own previous output (spec §7.6).
    Threaded into finalization / abstract_finalization / passing_criteria /
    planning / execution requests; each template renders an "Amending a
    previous version" block only when this is set. Classification never
    amends — it reruns from scratch."""

    previousArtifact: str = ""
    previousContent: str = ""
    newContent: str = ""
    diff: str = ""
    # "edit" | "backward_move" | "late_edit_decision"
    reason: str = "edit"
    guidance: str = ""


class PriorRoundContext(BaseModel):
    """What the previous round of this card did (spec §9.3) — context only,
    never state. Rendered into the classification, finalization, planning and
    execution prompts."""

    previousTaskId: str = ""
    round: int = 1
    title: str = ""
    description: str | None = None
    finalizedSpecUrl: str | None = None
    planUrl: str | None = None
    prUrls: list[str] = Field(default_factory=list)
    shippedAt: int | None = None


def render_amendment(amendment: "AmendmentContext | None") -> str:
    """The "Amending a previous version" prompt block, or "" (spec §7.6)."""
    if amendment is None:
        return ""
    why = {"edit": "the human edited the card",
           "backward_move": "the human moved the card back to redo this stage",
           "late_edit_decision": "the human chose to redo this after an edit",
           }.get(amendment.reason, amendment.reason)
    parts = [
        "## Amending a previous version",
        f"This stage already ran once for this task; it is rerunning because {why}. "
        "Revise your previous output rather than starting from nothing: keep what "
        "still holds, change what the new content or guidance invalidates.",
    ]
    if amendment.guidance:
        parts += ["", "Guidance:", amendment.guidance]
    if amendment.diff:
        parts += ["", "What changed in the card:", "```diff", amendment.diff, "```"]
    if amendment.previousArtifact:
        parts += ["", "Your previous output:", amendment.previousArtifact[:20_000]]
    return "\n".join(parts)


def render_prior_round(prior: "PriorRoundContext | None") -> str:
    """The "this card was shipped before" prompt block, or "" (spec §9.3)."""
    if prior is None:
        return ""
    lines = [
        "## This card was shipped before",
        f"Round {prior.round} of this card (task {prior.previousTaskId}) already "
        "shipped. The human moved it back to start a new round; the request below "
        "is what they want now. Build on what shipped rather than redoing it.",
        f"Previous title: {prior.title}",
    ]
    if prior.description:
        lines.append(f"Previous description:\n{prior.description[:4000]}")
    if prior.prUrls:
        lines.append("Previous pull requests: " + ", ".join(prior.prUrls))
    return "\n".join(lines)

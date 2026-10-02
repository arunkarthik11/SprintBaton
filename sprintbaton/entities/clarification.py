"""Structured clarification options (docs/clarification-options-spec.md §4) —
the optional multiple-choice payload a role can attach alongside its
clarificationQuestion pause signal. Plain value objects (the TokenUsage
precedent, entities/usage.py), never top-level persisted entities.

Field names mirror Claude Code's AskUserQuestion tool (spec §3.1); the
snake_case validation aliases accept the model-output schema shape (spec §7)
so `ClarificationOptions.model_validate(data)` works on either casing.
"""

from pydantic import AliasChoices, BaseModel, Field, model_validator

# AskUserQuestion's own bounds (spec §3.1): 2-4 options per question.
MIN_ANSWERS = 2
MAX_ANSWERS = 4


class Answer(BaseModel):
    """One offered option — AskUserQuestion's {label, description} plus two
    SprintBaton additions (spec §4.1)."""

    option: str
    description: str = ""
    isRecommended: bool = Field(
        default=False,
        validation_alias=AliasChoices("isRecommended", "is_recommended"))
    additionalNotes: str = Field(
        default="",
        validation_alias=AliasChoices("additionalNotes", "additional_notes"))


class ClarificationOptions(BaseModel):
    """The per-question options wrapper (spec §4.2). Violations of the 2-4
    answer count and single-recommendation invariants degrade in place
    (truncate / keep the first flag) rather than rejecting the turn — the
    same posture as every other optional field in this pipeline (§12). The
    too-few-answers case cannot be repaired here; parse_clarification_options
    (models/agents.py) drops the whole payload instead."""

    header: str = ""  # <=12 chars by AskUserQuestion convention; not enforced
    answers: list[Answer] = Field(default_factory=list)
    multiSelect: bool = Field(
        default=False,
        validation_alias=AliasChoices("multiSelect", "multi_select"))

    @model_validator(mode="after")
    def _degrade_violations(self) -> "ClarificationOptions":
        if len(self.answers) > MAX_ANSWERS:
            self.answers = self.answers[:MAX_ANSWERS]
        recommended = [a for a in self.answers if a.isRecommended]
        for extra in recommended[1:]:
            extra.isRecommended = False
        return self


def match_reply(options: ClarificationOptions,
                reply: str) -> tuple[str | None, list[str] | None]:
    """Best-effort mapping of a human's free-text comment onto the offered
    answers (spec §6): a number (or comma-separated numbers, multiSelect), or
    an exact case-insensitive option-text match. Anything else returns
    (None, None) — never a failure; replyText stays authoritative. Exact-only
    for v1, no fuzzy matching (§12 resolution)."""
    labels = [a.option for a in options.answers]
    if not labels or not reply.strip():
        return None, None

    def resolve_one(text: str) -> str | None:
        text = text.strip()
        if text.isdigit() and 1 <= int(text) <= len(labels):
            return labels[int(text) - 1]
        for label in labels:
            if text.casefold() == label.casefold():
                return label
        return None

    for candidate in (reply.strip(), reply.strip().splitlines()[0]):
        if options.multiSelect:
            picks = [resolve_one(part) for part in candidate.split(",")]
            if picks and all(p is not None for p in picks):
                # De-duplicated, offer order preserved
                chosen = [label for label in labels if label in picks]
                return None, chosen
        pick = resolve_one(candidate)
        if pick is not None:
            return (None, [pick]) if options.multiSelect else (pick, None)
    return None, None

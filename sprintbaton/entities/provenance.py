"""ClassificationRecord — the replay/provenance record for a classification
decision (docs/classification-provenance-spec.md §4.3).

One document per classification checkpoint (classification / spec_classification
/ plan_classification). It references a commit in the SprintBaton-owned provenance
git store (a bundle in MinIO) so the exact context the classifier saw can be
replayed offline and fed to an AI-as-judge for prompt/model calibration. Unlike
TaskActionEvent (which stamps versions + usage but never the verbatim inputs),
this record's snapshot IS the verbatim input, and stamp_outcome joins it to the
downstream result so the judge has a label, not just an input.

Indexed on taskId, action, category (see TaskActionEventRecorder-style wiring in
container.py).
"""

from pydantic import Field

from sprintbaton.entities.base import LOCAL_USER_ID, BaseEntity
from sprintbaton.entities.usage import TokenUsage


class ClassificationRecord(BaseEntity):
    # Tenant owner (user-multitenancy spec §6), inherited from the task
    userId: str = LOCAL_USER_ID
    taskId: str = ""
    repoId: str = ""
    project: str = ""
    # classification | spec_classification | plan_classification
    action: str = ""

    # The decision
    category: str = ""    # TaskType, for the Router; "" for the two checkpoints
    verdict: str = ""     # Spec/Plan classification verdict; "" for the Router
    important: bool = False
    rationale: str = ""

    # Provenance handles — snapshotCommitSha on snapshotBranch of the bundle at
    # snapshotBundleUrl reproduces the context; upstreamSha is the user-repo
    # commit the task was based on (the codebase handle).
    upstreamSha: str = ""
    snapshotBranch: str = ""
    snapshotCommitSha: str = ""
    snapshotBundleUrl: str = ""

    # Experimentation dimensions (mirror TaskActionEvent)
    modelId: str = ""
    promptId: str = ""
    promptVersion: str = ""
    provenanceVersion: str = ""

    usage: TokenUsage = Field(default_factory=TokenUsage)

    # Downstream outcome — stamped by ContextSnapshotService.stamp_outcome when
    # the task reaches Shipped / Blocked (classification provenance spec §4.2).
    outcomeStatus: str = ""
    outcomeTier: str = ""
    outcomeReviewFailures: int = 0
    outcomeAiReviewRounds: int = 0
    outcomeUpdatedTime: int | None = None

"""Classification provenance / context snapshots (docs/classification-provenance-spec.md).

A SprintBaton-owned, VCS-choice-independent snapshot store for the exact context
an agent saw at a decision point (git bundle in MinIO), plus the
ClassificationRecord that references it for offline AI-as-judge calibration.
"""

from sprintbaton.provenance.format import PROVENANCE_FORMAT_VERSION
from sprintbaton.provenance.git_store import ProvenanceGitStore, SnapshotRef
from sprintbaton.provenance.snapshot import ContextSnapshotService

__all__ = [
    "PROVENANCE_FORMAT_VERSION",
    "ProvenanceGitStore",
    "SnapshotRef",
    "ContextSnapshotService",
]

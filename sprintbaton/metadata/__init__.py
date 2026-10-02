"""Repository/project metadata: the `.sprintbaton/` contract, immutable
revisions, and initialization runs (docs/project-initialization-task-spec.md).
The init pass itself runs as a queued Task through the orchestrator."""

from sprintbaton.metadata.format import METADATA_FORMAT_VERSION

__all__ = ["METADATA_FORMAT_VERSION"]

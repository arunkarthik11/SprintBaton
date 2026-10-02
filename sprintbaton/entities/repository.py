"""Repository — a git repo that is a member of exactly one Project
(multi-repo-project spec §4.2). Slimmed to git-level concerns only; the
board-level fields (columns/board id/cadence/provider) moved up to Project now
that one board can drive N repos.

ColumnConfig / TEMPLATE_SECTIONS are re-exported here from entities.project so
existing imports (`from sprintbaton.entities.repository import ColumnConfig`)
keep resolving."""

from sprintbaton.entities.base import LOCAL_USER_ID, DescriptionEntity
from sprintbaton.entities.project import (  # re-export for import compatibility
    TEMPLATE_SECTIONS,
    ColumnConfig,
)

__all__ = ["Repository", "ColumnConfig", "TEMPLATE_SECTIONS"]


class Repository(DescriptionEntity):
    """A git repository, member of one Project (one Project == N repos)."""

    # Tenant owner (user-multitenancy spec §6); LOCAL_USER_ID in CLI mode
    userId: str = LOCAL_USER_ID
    # The owning Project (multi-repo-project spec §4.2)
    projectId: str = ""
    # What this repo does in the project ("frontend UI", "backend API", …),
    # fed into the combined project metadata (spec §5.2).
    role: str = ""
    remoteUrl: str = ""
    githubRepo: str = ""  # "owner/name"
    devBranch: str = "dev"          # integration trunk feature PRs merge into
    stagingBranch: str = "staging"  # current release-window batch under QA
    productionBranch: str = "main"  # live
    # Per-repo credential override (spec §10): explicit id -> user default ->
    # deployment Settings fallback.
    githubCredentialId: str | None = None
    # Per-repo git identity override (tier 1 of storage-layout-and-git-identity
    # spec §5.2); None falls through to the project's, then the deployment's.
    gitAuthorName: str | None = None
    gitAuthorEmail: str | None = None
    # This repo's own .sprintbaton info-file URL (per-repo metadata pass §5.1),
    # always written in the same save as metadataRevision.
    metadataUrl: str | None = None
    # The pointer to this repo's current immutable metadata revision (the init
    # task id that published it — project-initialization-task spec §4.4).
    # None is the auto-trigger's "missing" signal.
    metadataRevision: str | None = None

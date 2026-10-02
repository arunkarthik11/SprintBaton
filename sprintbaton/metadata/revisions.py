"""Metadata revisions: publish, pointer swap, garbage collection
(docs/project-initialization-task-spec.md §9).

Every successful init pass uploads its validated directory as a new
**immutable** revision (keyed by the init task id that produced it), then makes
it current by repointing one entity field — `Repository.metadataRevision` or
`Project.metadataRevision` — in a single whole-document save. Readers resolve
the pointer once and read only under that revision, so they never observe a
mixed or partially uploaded tree; a failed, crashed, retried, or cut-off
attempt never changes a pointer.

Garbage collection keeps exactly the new current and the previous current
revision, which also makes a reader that resolved the pointer just before a
swap safe, and leaves one revision for a manual rollback.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path

from sprintbaton.entities.base import now_millis
from sprintbaton.entities.project import Project
from sprintbaton.entities.repository import Repository
from sprintbaton.storage.base import BlobStore, DistributedLock, EntityDAO, safe_segment

log = logging.getLogger(__name__)

INIT_LOCK_PREFIX = "project-init:"
# Long enough to outwait an apply_project upsert, short enough that a wedged
# holder surfaces as a (transient, retried) failure rather than a hang.
LOCK_TTL_SECONDS = 120
LOCK_WAIT_SECONDS = 60


def init_lock_key(project_id: str) -> str:
    """The one DistributedLock key serializing every write to a project's
    Project/Repository rows that init touches: apply_project's upsert, run
    creation, the gate override, and every pointer swap (spec §5.1)."""
    return f"{INIT_LOCK_PREFIX}{project_id}"


class MetadataLockTimeout(TimeoutError):
    """The project-init lock stayed held past LOCK_WAIT_SECONDS. A TimeoutError,
    so the init pass classifies it transient and retries (spec §5.6)."""


@dataclass
class PublishResult:
    revision: str
    info_url: str
    files: int
    previous: str | None
    collected: list[str] = field(default_factory=list)
    first_initialization: bool = False


def _directory_files(source: Path) -> list[tuple[str, Path]]:
    return [(path.relative_to(source).as_posix(), path)
            for path in sorted(source.rglob("*")) if path.is_file()]


def _revision_of(key: str, root: str) -> str | None:
    rest = key[len(root):] if key.startswith(root) else ""
    head, sep, _ = rest.partition("/")
    return head if sep and head else None


class MetadataRevisionPublisher:
    def __init__(self, blob: BlobStore, repo_repo: EntityDAO[Repository],
                 project_repo: EntityDAO[Project], lock: DistributedLock):
        self._blob = blob
        self._repos = repo_repo
        self._projects = project_repo
        self._lock = lock

    # ------------------------------------------------------------- publish

    def publish_repo(self, repo: Repository, source: Path | str,
                     revision: str) -> PublishResult:
        """Upload a validated `.sprintbaton/` as `revision` and make it the
        repo's current one."""
        prefix = self._blob.repo_metadata_prefix(
            repo.userId, repo.projectId, repo.id, revision)
        files = self._upload(prefix, Path(source), lambda rel: self._blob.repo_metadata_key(
            repo.userId, repo.projectId, repo.id, revision, rel))
        info_url = self._blob.url_for(self._blob.repo_metadata_key(
            repo.userId, repo.projectId, repo.id, revision, "info"))

        with self._locked(repo.projectId):
            current = self._repos.get(repo.id)
            if current is None:
                raise RuntimeError(f"repository {repo.id} vanished before its "
                                   f"metadata revision could be published")
            previous = current.metadataRevision
            current.metadataRevision = revision
            current.metadataUrl = info_url
            current.touch()
            self._repos.save(current)
        # Mirror onto the caller's copy so later reads in the same pass see it.
        repo.metadataRevision, repo.metadataUrl = revision, info_url

        collected = self._collect(
            self._blob.repo_metadata_root(repo.userId, repo.projectId, repo.id),
            keep={revision, previous})
        return PublishResult(revision=revision, info_url=info_url, files=files,
                             previous=previous, collected=collected)

    def publish_project(self, project: Project, source: Path | str,
                        revision: str) -> PublishResult:
        """Upload a validated project index as `revision`, make it current, and
        — on the first successful run — open the TaskPending gate (§5.4)."""
        prefix = self._blob.project_metadata_prefix(project.userId, project.id, revision)
        files = self._upload(prefix, Path(source), lambda rel: self._blob.project_metadata_key(
            project.userId, project.id, revision, rel))
        info_url = self._blob.url_for(self._blob.project_metadata_key(
            project.userId, project.id, revision, "info"))

        with self._locked(project.id):
            current = self._projects.get(project.id, include_deleted=True)
            if current is None:
                raise RuntimeError(f"project {project.id} vanished before its "
                                   f"metadata revision could be published")
            previous = current.metadataRevision
            current.metadataRevision = revision
            current.metadataUrl = info_url
            first = current.metadataInitializedAt is None
            if first:
                current.metadataInitializedAt = now_millis()
            # The gate is open on its own now; an override has nothing left to
            # do (spec §4.3, invariant 12).
            current.metadataGateOverride = False
            current.touch()
            self._projects.save(current)
        project.metadataRevision, project.metadataUrl = revision, info_url
        project.metadataInitializedAt = current.metadataInitializedAt
        project.metadataGateOverride = False

        collected = self._collect(
            self._blob.project_metadata_root(project.userId, project.id),
            keep={revision, previous})
        return PublishResult(revision=revision, info_url=info_url, files=files,
                             previous=previous, collected=collected,
                             first_initialization=first)

    # ------------------------------------------------------------- helpers

    def _upload(self, prefix: str, source: Path, key_for) -> int:
        # Step 1: a revision is never current before its own swap, so any keys
        # already under this prefix came from a crashed or retried attempt of
        # the same task — clear them so the revision is exactly this upload.
        for stale in self._blob.list_keys(prefix):
            self._blob.delete(stale)
        # Step 2: upload every file of the validated directory.
        files = _directory_files(source)
        for rel, path in files:
            self._blob.put_text(key_for(rel), path.read_text(errors="replace"))
        return len(files)

    def _locked(self, project_id: str) -> "_HeldLock":
        return held_init_lock(self._lock, project_id)

    def _collect(self, root: str, keep: set[str | None]) -> list[str]:
        """Delete every revision under `root` except the ones in `keep` — also
        sweeping staged revisions orphaned by failed runs. Best-effort: a GC
        failure is logged and the next swap retries it (spec §9.3)."""
        # Keys carry the sanitized revision segment; compare like with like.
        keep_segments = {safe_segment(r) for r in keep if r}
        try:
            by_revision: dict[str, list[str]] = {}
            for key in self._blob.list_keys(root):
                revision = _revision_of(key, root)
                if revision is not None:
                    by_revision.setdefault(revision, []).append(key)
            collected = []
            for revision, keys in sorted(by_revision.items()):
                if revision in keep_segments:
                    continue
                for key in keys:
                    self._blob.delete(key)
                collected.append(revision)
            return collected
        except Exception:
            log.warning("metadata revision garbage collection failed",
                        extra={"root": root}, exc_info=True)
            return []


class _HeldLock:
    """Enter the project-init lock or raise MetadataLockTimeout."""

    def __init__(self, lock: DistributedLock, key: str):
        self._cm = lock.lock(key, ttl_seconds=LOCK_TTL_SECONDS,
                             wait_seconds=LOCK_WAIT_SECONDS)
        self._key = key

    def __enter__(self):
        if not self._cm.__enter__():
            self._cm.__exit__(None, None, None)
            raise MetadataLockTimeout(f"could not acquire lock {self._key!r}")
        return self

    def __exit__(self, *exc):
        return self._cm.__exit__(*exc)


def held_init_lock(lock: DistributedLock, project_id: str) -> _HeldLock:
    """The project-init lock as a context manager that raises instead of
    yielding False — for every writer that must not proceed unlocked."""
    return _HeldLock(lock, init_lock_key(project_id))

"""EntityDAO / PersistenceBackend — the storage interface every backend
implements (docs/persistence-abstraction-spec.md §3).

Structural Protocols (like Harness), not ABCs, so the existing Mongo
EntityRepository satisfies them without inheriting from anything. The surface
is deliberately identical to today's EntityRepository: `find`/`find_one` take
a plain equality-only filter dict — every call site in the codebase uses that
shape and nothing more (spec §2), and keeping the contract narrow is what lets
a second backend match Mongo's semantics exactly rather than approximately.
"""

import re
from contextlib import AbstractContextManager
from typing import Any, Protocol, TypeVar

from sprintbaton.entities.base import BaseEntity

T = TypeVar("T", bound=BaseEntity)


def safe_segment(value: str, max_length: int = 64) -> str:
    """Sanitize one path segment of a blob key or lock filename. Backend-
    agnostic key-shaping logic shared by every BlobStore implementation
    (zero-infra-storage spec §3): task ids come from providers/Mongo, the
    project/repo segments are entity ids, and a metadata `rel` component comes
    straight from a generation model — none is trusted to be key-safe by
    convention alone (conversation-lifecycle spec §5).

    max_length exists because filenames legitimately run longer than the
    entity-id segments this default was sized for (storage-layout spec §3.2);
    truncating one would silently collide two distinct artifacts."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", value)[:max_length]


class EntityDAO(Protocol[T]):
    """One entity collection's persistence operations."""

    def ensure_indexes(self, *fields: str) -> None: ...

    def save(self, entity: T) -> T: ...

    def get(self, entity_id: str, include_deleted: bool = False) -> T | None: ...

    def find(self, query: dict[str, Any], limit: int = 0) -> list[T]: ...

    def find_one(self, query: dict[str, Any]) -> T | None: ...

    def soft_delete(self, entity_id: str) -> None: ...


class PersistenceBackend(Protocol):
    """A storage engine that can hand out an EntityDAO per collection."""

    def repository(self, entity_cls: type[T], collection: str) -> EntityDAO[T]: ...

    def close(self) -> None: ...


class BlobStore(Protocol):
    """Large-artifact storage behind a URL-pointer convention (zero-infra-
    storage spec §3): entities carry only the URL a put_* returned; every read
    goes back through get_text_by_url/get_text/get_bytes. Identical surface to
    the original ObjectStorageService — same discipline as EntityDAO: never
    grow the interface and swap backends in the same change."""

    def put_text(self, key: str, text: str) -> str: ...

    def get_text(self, key: str) -> str | None: ...

    def put_bytes(self, key: str, data: bytes,
                  content_type: str = "application/octet-stream") -> str: ...

    def get_bytes(self, key: str) -> bytes | None: ...

    def get_text_by_url(self, url: str) -> str | None: ...

    def url_for(self, key: str) -> str: ...

    def key_for(self, url: str) -> str | None: ...

    # The one tenant-rooted key namespace (storage-layout spec §3.2). No call
    # site outside sprintbaton/storage/ constructs a key by concatenation —
    # see sprintbaton/storage/keys.py for the tree and the reasoning.

    def task_key(self, user_id: str, project_id: str, task_id: str,
                 category: str, filename: str) -> str: ...

    # Metadata keys carry a revision segment (project-initialization-task
    # spec §9.1); the *_root builders are the parent of every revision.

    def repo_metadata_key(self, user_id: str, project_id: str, repo_id: str,
                          revision: str, rel: str) -> str: ...

    def repo_metadata_prefix(self, user_id: str, project_id: str,
                             repo_id: str, revision: str) -> str: ...

    def repo_metadata_root(self, user_id: str, project_id: str,
                           repo_id: str) -> str: ...

    def project_metadata_key(self, user_id: str, project_id: str,
                             revision: str, rel: str) -> str: ...

    def project_metadata_prefix(self, user_id: str, project_id: str,
                                revision: str) -> str: ...

    def project_metadata_root(self, user_id: str, project_id: str) -> str: ...

    def provenance_key(self, user_id: str, project_id: str,
                       filename: str) -> str: ...

    def list_keys(self, prefix: str = "") -> list[str]: ...

    def delete(self, key: str) -> None:
        """Remove one key; a missing key is a no-op. Added for metadata-
        revision garbage collection (project-initialization-task spec §9.3)."""


class TaskQueue(Protocol):
    """The producer/consumer handoff between the polling job and the worker
    loop (zero-infra-storage spec §4). Deliberately not durable: crash
    recovery is the startup reconciliation pass over the entity store
    (orchestrator/recovery.py), not queue persistence. One queue per
    deployment: every worker consumes every owner's tasks, because tenant
    isolation is the sandbox's job (hosted-sandbox-isolation spec §12)."""

    def enqueue_task(self, task_id: str) -> None: ...

    def dequeue_task(self, timeout_seconds: int = 5) -> str | None: ...


class DistributedLock(Protocol):
    """Best-effort cross-process mutual exclusion (zero-infra-storage spec §5).
    lock() never raises; it yields True if acquired, False on timeout —
    callers needing exclusivity treat False as "skip this write"."""

    def lock(self, key: str, ttl_seconds: int = 60, wait_seconds: int = 30,
             poll_seconds: float = 0.1) -> AbstractContextManager[bool]: ...

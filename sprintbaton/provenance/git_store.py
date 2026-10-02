"""ProvenanceGitStore — the git-in-MinIO snapshot mechanics
(docs/classification-provenance-spec.md §4.1).

SprintBaton owns one provenance repo per project, distinct from the user's own
git (Tree 2 in the spec). Its canonical durable form is a git bundle stored in
MinIO; a fresh ephemeral working copy is hydrated from that bundle per write, so
nothing depends on pod-local disk surviving. Each task gets its own orphan
branch (task/<task_id>), so concurrent tasks never contend on a shared HEAD, and
git's object model dedups identical blobs/trees across every branch — the
codebase is never copied (the upstream SHA is its handle), so the bundle stays
small.
"""

import logging
import subprocess
import tempfile
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from sprintbaton.storage.base import BlobStore

log = logging.getLogger(__name__)

BUNDLE_FILENAME = "context.bundle"
_AUTHOR_NAME = "SprintBaton"
_AUTHOR_EMAIL = "provenance@sprintbaton"


class _Locker(Protocol):
    def lock(self, key: str, ttl_seconds: int = ..., wait_seconds: int = ...): ...


@dataclass(frozen=True)
class SnapshotRef:
    branch: str
    commit_sha: str
    bundle_url: str


class ProvenanceGitStore:
    def __init__(self, object_storage: BlobStore, workspace_root: str,
                 locker: _Locker | None = None):
        self._storage = object_storage
        self._root = Path(workspace_root) / ".provenance"
        self._root.mkdir(parents=True, exist_ok=True)
        self._locker = locker

    def commit(self, user_id: str, project: str, branch: str,
               files: dict[str, str], message: str) -> SnapshotRef:
        """Add one snapshot commit on `branch` of `project`'s provenance repo and
        push the updated bundle back to MinIO. Serialised per project by the
        locker's SET NX lock so concurrent writers can't clobber the bundle."""
        bundle_key = self._storage.provenance_key(user_id, project, BUNDLE_FILENAME)
        lock_cm = (self._locker.lock(f"sprintbaton:provenance:{project}")
                   if self._locker else nullcontext(True))
        with lock_cm as acquired:
            if acquired is False:
                # Could not get the lock in time — skip rather than risk a
                # racy bundle overwrite. The decision is still in Mongo minus
                # its snapshot; provenance is best-effort by design.
                raise RuntimeError("provenance lock not acquired")
            return self._commit_locked(project, bundle_key, branch, files, message)

    def _commit_locked(self, project: str, bundle_key: str, branch: str,
                       files: dict[str, str], message: str) -> SnapshotRef:
        with tempfile.TemporaryDirectory(dir=self._root) as td:
            work = Path(td)
            self._git(["init", "-q"], work)
            bundle = self._storage.get_bytes(bundle_key)
            if bundle:
                incoming = work / ".incoming.bundle"
                incoming.write_bytes(bundle)
                self._git(["fetch", "-q", str(incoming),
                           "refs/heads/*:refs/heads/*"], work)
                incoming.unlink()

            if self._branch_exists(work, branch):
                self._git(["checkout", "-q", branch], work)
            else:
                self._git(["checkout", "-q", "--orphan", branch], work)

            for rel, content in files.items():
                path = work / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)

            self._git(["add", "-A"], work)
            if not self._git(["status", "--porcelain"], work).strip() \
                    and self._branch_exists(work, branch):
                # Identical to the prior snapshot — reuse its commit.
                sha = self._git(["rev-parse", "HEAD"], work).strip()
                return SnapshotRef(branch, sha, self._storage.url_for(bundle_key))

            self._git(["-c", f"user.name={_AUTHOR_NAME}",
                       "-c", f"user.email={_AUTHOR_EMAIL}",
                       "commit", "-q", "-m", message], work)
            sha = self._git(["rev-parse", "HEAD"], work).strip()

            out_bundle = work / ".out.bundle"
            self._git(["bundle", "create", str(out_bundle), "--all"], work)
            url = self._storage.put_bytes(bundle_key, out_bundle.read_bytes())
            log.info("provenance snapshot committed",
                     extra={"project": project, "branch": branch, "commit": sha})
            return SnapshotRef(branch, sha, url)

    # ------------------------------------------------------------- git helpers

    @staticmethod
    def _branch_exists(work: Path, branch: str) -> bool:
        result = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            cwd=work, capture_output=True, text=True)
        return result.returncode == 0

    @staticmethod
    def _git(args: list[str], cwd: Path) -> str:
        result = subprocess.run(["git", *args], cwd=cwd,
                                capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            raise RuntimeError(
                f"git {' '.join(args)} failed: {result.stderr.strip()}")
        return result.stdout

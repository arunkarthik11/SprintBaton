"""Per-task on-disk workspace (docs/task-workspace-spec.md).

Materializes .sprintbaton/tasks/<task_id>/ inside whichever git clone the
calling role already has: a metadata index, the original title/description,
the finalized spec/plan/passing criteria mirrored from their Task.*Action
MinIO artifacts, plus two genuinely new append-only artifacts accumulated in
MinIO under the "agent" category — notes.md (cross-role reasoning traces) and
review-comments.md (Review Agent findings per bounce round). The MinIO
artifacts stay authoritative; the directory is a disposable, always-freshly-
regenerated mirror.
"""

import logging
from datetime import datetime, timezone
from pathlib import Path

from sprintbaton.entities.task import Task
from sprintbaton.storage.base import BlobStore
from sprintbaton.workspace.format import TASK_WORKSPACE_FORMAT_VERSION

log = logging.getLogger(__name__)

_DESCRIPTIONS = {
    "task.md": "Original title + description as posted to the todolist",
    "spec.md": "Finalized specification (Task Finalization)",
    "plan.md": "Implementation plan (Plan Finalization)",
    "passing-criteria.md": "Exhaustive acceptance criteria",
    "notes.md": "Cross-role reasoning traces — appended by every role that produces one",
    "review-comments.md": "Review Agent findings, one section per bounce round",
}

# Placeholder text for absent artifacts, shared by materialize() and
# snapshot_files() so the two can never drift.
_PLACEHOLDERS = {
    "spec.md": ("(task did not go through Task Finalization — Simple/Complex tasks "
                "execute directly from the title and description)"),
    "plan.md": "(task did not go through Plan Finalization)",
    "passing-criteria.md": "(passing criteria not yet generated)",
    "notes.md": "(no notes yet)",
    "review-comments.md": "(no review rounds yet)",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class TaskWorkspaceService:
    def __init__(self, object_storage: BlobStore, cleaner=None):
        self._storage = object_storage
        # WorkspaceCleaner (workspace/cleanup.py); None = removal is a no-op
        # (unit tests that never touch disk).
        self._cleaner = cleaner

    # ---------------------------------------------------------------- cleanup

    def remove_task(self, task: Task) -> bool:
        """Remove a Shipped task's directory and harness scratch cwds
        (workspace-mirrors-and-cleanup spec §5.2). Best-effort: a failure logs
        `workspace_cleanup_failed` and never fails the ship — the sweeper
        retries it. Returns whether the removal succeeded."""
        if self._cleaner is None or not task.projectId:
            return False
        try:
            self._cleaner.remove_task(task.userId, task.projectId, task.id)
            return True
        except Exception:
            log.warning("could not remove the task's workspace", exc_info=True,
                        extra={"event": "workspace_cleanup_failed", "task_id": task.id})
            return False

    def remove_init(self, user_id: str, project_id: str) -> bool:
        """Remove a project's init/ directory after a successful init run
        (spec §5.3). Best-effort, like remove_task."""
        if self._cleaner is None:
            return False
        try:
            self._cleaner.remove_init(user_id, project_id)
            return True
        except Exception:
            log.warning("could not remove the project's init workspace", exc_info=True,
                        extra={"event": "workspace_cleanup_failed",
                               "project_id": project_id})
            return False

    # ------------------------------------------------------------- read path

    def materialize(self, workspace: Path | str, project: str, task: Task) -> None:
        """Regenerate .sprintbaton/tasks/<task_id>/ in full (spec §5.1).
        Idempotent — always safe to call more than once per turn."""
        task_dir = Path(workspace) / ".sprintbaton" / "tasks" / task.id
        task_dir.mkdir(parents=True, exist_ok=True)

        task_dir.joinpath("task.md").write_text(
            f"# {task.title}\n\n{task.description or ''}")
        statuses = {"task.md": "present"}
        statuses["spec.md"] = self._write_artifact(
            task_dir / "spec.md",
            self._action_text(task.taskFinalizationAction),
            _PLACEHOLDERS["spec.md"])
        statuses["plan.md"] = self._write_artifact(
            task_dir / "plan.md",
            self._action_text(task.taskPlanningAction),
            _PLACEHOLDERS["plan.md"])
        statuses["passing-criteria.md"] = self._write_artifact(
            task_dir / "passing-criteria.md",
            self._action_text(task.taskPassingCriteriaAction),
            _PLACEHOLDERS["passing-criteria.md"])
        statuses["notes.md"] = self._write_artifact(
            task_dir / "notes.md",
            self._agent_text(task.userId, project, task.id, "notes.md"),
            _PLACEHOLDERS["notes.md"])
        review_text = self._agent_text(
            task.userId, project, task.id, "review-comments.md")
        statuses["review-comments.md"] = self._write_artifact(
            task_dir / "review-comments.md", review_text,
            _PLACEHOLDERS["review-comments.md"])
        if review_text:
            rounds = review_text.count("## Round ")
            statuses["review-comments.md"] = f"present ({rounds} round(s))"

        task_dir.joinpath("metadata.md").write_text(self._render_index(task, statuses))
        log.info("task workspace materialized",
                 extra={"task_id": task.id, "path": str(task_dir)})

    def snapshot_files(self, project: str, task: Task) -> dict[str, str]:
        """The six workspace mirror files as {filename: content}, without
        touching disk — used by the provenance store to capture a faithful copy
        of the context a role saw (classification provenance spec §3). Uses the
        same placeholders as materialize() for absent artifacts."""
        return {
            "task.md": f"# {task.title}\n\n{task.description or ''}",
            "spec.md": (self._action_text(task.taskFinalizationAction)
                        or _PLACEHOLDERS["spec.md"]),
            "plan.md": (self._action_text(task.taskPlanningAction)
                        or _PLACEHOLDERS["plan.md"]),
            "passing-criteria.md": (self._action_text(task.taskPassingCriteriaAction)
                                    or _PLACEHOLDERS["passing-criteria.md"]),
            "notes.md": (self._agent_text(task.userId, project, task.id, "notes.md")
                         or _PLACEHOLDERS["notes.md"]),
            "review-comments.md": (
                self._agent_text(task.userId, project, task.id, "review-comments.md")
                or _PLACEHOLDERS["review-comments.md"]),
        }

    def materialize_repo_metadata(self, workspace: Path | str, repo) -> None:
        """Mirror a repo's current metadata revision from the blob store onto
        disk at <clone>/.sprintbaton/ so a browsing agent can read it directly
        (multi-repo-project spec §5.1). The pointer is resolved once and only
        that revision is read, so a concurrent swap can never produce a mixed
        tree (project-initialization-task spec §9.4). Does nothing when no
        revision is published — best-effort, a missing pass just leaves no
        metadata dir."""
        revision = getattr(repo, "metadataRevision", None)
        if not revision:
            return
        prefix = self._storage.repo_metadata_prefix(
            repo.userId, repo.projectId, repo.id, revision)
        written = self._mirror(prefix, Path(workspace) / ".sprintbaton")
        if written:
            log.info("repo metadata materialized", extra={
                "repo_id": repo.id, "revision": revision, "files": written})

    def materialize_project_metadata(self, index_dir: Path | str, project,
                                     repos: list, *,
                                     write_fallback: bool = True) -> None:
        """Materialize the combined project-metadata index (spec §5.2) outside
        every repo clone, from the project's current revision. If none is
        published, writes a minimal fallback index that still names each repo
        and points at its .sprintbaton/ — so a model always has a starting map
        (spec §5.3) — unless `write_fallback` is off: the project init pass
        seeds its first run empty (project-initialization-task spec §8.2)."""
        index = Path(index_dir)
        index.mkdir(parents=True, exist_ok=True)
        wrote = 0
        revision = getattr(project, "metadataRevision", None)
        if revision:
            wrote = self._mirror(
                self._storage.project_metadata_prefix(project.userId, project.id,
                                                      revision), index)
        if not wrote and write_fallback:
            index.joinpath("info").write_text(
                self._fallback_project_index(project, repos))

    def _mirror(self, prefix: str, dest_root: Path) -> int:
        """Copy every key under `prefix` to the same relative path under
        `dest_root`. Returns how many files were written."""
        try:
            keys = self._storage.list_keys(prefix)
        except Exception:
            keys = []
        written = 0
        for key in keys:
            rel = key[len(prefix):]
            if not rel:
                continue
            text = self._storage.get_text(key)
            if text is None:
                continue
            dest = dest_root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(text)
            written += 1
        return written

    @staticmethod
    def _fallback_project_index(project, repos: list) -> str:
        """States no clone paths, for the same reason the generated index
        doesn't (storage-layout spec §10 q4): the layout differs per role, and
        the runtime prepends a location guide that knows the real one."""
        lines = [
            f"# Project: {project.title}",
            "",
            "This project spans the repositories below. For each repo's internal "
            "structure (services, entities, screens, …), read that repo's own "
            "`.sprintbaton/info`; the \"Where things are on disk\" section of "
            "your prompt gives its current location.",
            "",
        ]
        for repo in repos:
            lines += [
                f"## {repo.title} ({repo.role or 'unspecified role'})",
                f"- repository id: {repo.id}",
                f"- github: {repo.githubRepo or repo.remoteUrl or '(none)'}",
                "- detailed internals: this repository's own `.sprintbaton/info`",
                "",
            ]
        return "\n".join(lines)

    # ------------------------------------------------------------ write path

    def append_note(self, project: str, task: Task, *, role: str, text: str) -> None:
        """Durable, cross-role-visible reasoning trace (spec §5.2). Append-only
        for the lifetime of the task; a no-op on empty text."""
        if not text:
            return
        key = self._storage.task_key(
            task.userId, project, task.id, "agent", "notes.md")
        existing = self._storage.get_text(key) or f"# Notes — {task.title}\n"
        entry = f"\n## {role} — {_now_iso()}\n\n{text}\n"
        self._storage.put_text(key, existing + entry)

    def append_review_comments(self, project: str, task: Task, *,
                               round_number: int, findings: list[str]) -> None:
        """Review Agent findings for one bounce round (spec §5.3) — kept as a
        separate file from notes.md by design."""
        if not findings:
            return
        key = self._storage.task_key(
            task.userId, project, task.id, "agent", "review-comments.md")
        existing = (self._storage.get_text(key)
                    or f"# Review comments — {task.title}\n")
        bullets = "\n".join(f"- {finding}" for finding in findings)
        entry = f"\n## Round {round_number} — {_now_iso()}\n\n{bullets}\n"
        self._storage.put_text(key, existing + entry)

    # --------------------------------------------------------------- helpers

    def _action_text(self, url: str | None) -> str | None:
        return self._storage.get_text_by_url(url) if url else None

    def _agent_text(self, user_id: str, project: str, task_id: str,
                    filename: str) -> str | None:
        key = self._storage.task_key(user_id, project, task_id, "agent", filename)
        return self._storage.get_text(key)

    @staticmethod
    def _write_artifact(path: Path, text: str | None, placeholder: str) -> str:
        path.write_text(text if text else placeholder)
        return "present" if text else "not yet produced"

    @staticmethod
    def _render_index(task: Task, statuses: dict[str, str]) -> str:
        rows = "\n".join(f"| {name} | {status} | {_DESCRIPTIONS[name]} |"
                         for name, status in statuses.items())
        return (f"# Task workspace: {task.id}\n\n"
                f"version: {TASK_WORKSPACE_FORMAT_VERSION}\n"
                f"task: {task.title}\n\n"
                f"| File | Status | Description |\n|---|---|---|\n{rows}\n")

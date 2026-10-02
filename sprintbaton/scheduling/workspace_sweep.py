"""WorkspaceSweepJob — the backstop for on-disk cleanup
(docs/workspace-mirrors-and-cleanup-spec.md §5.4).

Shipped tasks remove their own directories inline (`_mark_shipped`); this
catches everything that misses: a crash mid-removal, a cleanup failure, a ship
that happened while the process was down, and whatever deleted projects and
repositories leave behind. Same shape as UsageLimitWakeJob (daemon thread,
start/stop, a `run_once` a test can call directly).

It walks the filesystem and asks the entity store about what it finds. It
never enqueues and never writes an entity; it only deletes local directories,
and only through `remove_tree`. A directory whose path names a different
owner than its row is skipped with a warning, never deleted. `Blocked` and
non-terminal tasks are never touched.
"""

import logging
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

from sprintbaton.entities.enums import TaskStatus
from sprintbaton.workspace.cleanup import WorkspaceCleaner, remove_tree

log = logging.getLogger(__name__)

_SCRATCH_RE = re.compile(r"^sprintbaton-.+-cwd-(?P<task>.+)$")


@dataclass
class SweepReport:
    removed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


class WorkspaceSweepJob:
    def __init__(self, cleaner: WorkspaceCleaner, task_repo, project_repo, repo_repo,
                 interval_seconds: int = 3600):
        self._cleaner = cleaner
        self._tasks = task_repo
        self._projects = project_repo
        self._repos = repo_repo
        self._interval = interval_seconds
        self._stop = threading.Event()

    # -------------------------------------------------------------- lifecycle

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self._loop, name="workspace-sweep-job",
                                  daemon=True)
        thread.start()
        return thread

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        log.info("workspace sweep job started", extra={"interval": self._interval})
        while True:
            try:
                self.run_once()
            except Exception:
                log.exception("workspace sweep failed")
            # 0 disables the periodic pass; the startup pass always runs.
            if self._interval <= 0 or self._stop.wait(self._interval):
                return

    # ------------------------------------------------------------------ work

    def run_once(self) -> SweepReport:
        report = SweepReport()
        self._sweep_workspaces(report)
        self._sweep_mirrors(report)
        self._sweep_scratch(report)
        if report.removed:
            log.info("workspace sweep reclaimed directories", extra={
                "event": "workspace_swept", "removed": len(report.removed)})
        return report

    def _owner_matches(self, row, user_id: str, path: Path, report: SweepReport) -> bool:
        if row is None or row.userId == user_id:
            return True
        log.warning("workspace sweep skipped a directory whose owner does not match "
                    "its row", extra={"event": "workspace_sweep_owner_mismatch",
                                      "path": str(path), "row_user_id": row.userId})
        report.skipped.append(str(path))
        return False

    def _remove(self, path: Path, root: Path, report: SweepReport) -> None:
        if remove_tree(path, root):
            report.removed.append(str(path))

    @staticmethod
    def _dirs(parent: Path) -> list[Path]:
        if not parent.is_dir():
            return []
        return sorted(p for p in parent.iterdir() if p.is_dir() and not p.is_symlink())

    def _task_removable(self, task) -> bool:
        return task is None or task.deleted or task.status == TaskStatus.Shipped

    def _sweep_workspaces(self, report: SweepReport) -> None:
        root = self._cleaner.workspace_root
        for user_dir in self._dirs(root / "users"):
            user_id = user_dir.name
            for project_dir in self._dirs(user_dir / "projects"):
                project = self._projects.get(project_dir.name, include_deleted=True)
                if not self._owner_matches(project, user_id, project_dir, report):
                    continue
                if project is None or project.deleted:
                    self._remove(project_dir, root, report)
                    continue
                for task_dir in self._dirs(project_dir / "tasks"):
                    # `<t>-ro` is the repo-less read-only clone's name.
                    task_id = task_dir.name.removesuffix("-ro")
                    task = self._tasks.get(task_id, include_deleted=True)
                    if not self._owner_matches(task, user_id, task_dir, report):
                        continue
                    if self._task_removable(task):
                        self._remove(task_dir, root, report)
                        for scratch in self._cleaner.scratch_dirs(task_id):
                            self._remove(scratch, self._cleaner.scratch_root, report)

    def _sweep_mirrors(self, report: SweepReport) -> None:
        root = self._cleaner.mirror_root
        for user_dir in self._dirs(root / "users"):
            for project_dir in self._dirs(user_dir / "projects"):
                for mirror in self._dirs(project_dir / "repos"):
                    if not mirror.name.endswith(".git"):
                        continue
                    repo = self._repos.get(mirror.name.removesuffix(".git"),
                                           include_deleted=True)
                    if not self._owner_matches(repo, user_dir.name, mirror, report):
                        continue
                    if repo is None or repo.deleted or repo.projectId != project_dir.name:
                        self._remove(mirror, root, report)

    def _sweep_scratch(self, report: SweepReport) -> None:
        """Harness scratch cwds live in a shared temp directory, which another
        install on the same host may use too — so only a task this store
        knows to be finished is reclaimed here; an unknown id is left alone."""
        scratch_root = self._cleaner.scratch_root
        if not scratch_root.is_dir():
            return
        for entry in self._dirs(scratch_root):
            match = _SCRATCH_RE.match(entry.name)
            if not match:
                continue
            task = self._tasks.get(match.group("task"), include_deleted=True)
            if task is not None and (task.deleted or task.status == TaskStatus.Shipped):
                self._remove(entry, scratch_root, report)

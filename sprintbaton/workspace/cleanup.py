"""Removing the on-disk directories of finished work
(docs/workspace-mirrors-and-cleanup-spec.md Part C).

Every removal goes through `remove_tree`, and every path it is handed is
computed from entity ids through the vcs/git_service.py path functions —
never from anything read off disk (§5.5). Removing a task directory loses
nothing that is not already in the blob store, the entity store or the git
remote (§5.6): the `.sprintbaton/tasks/<t>/` inside a clone is a regenerated
mirror of blob-store artifacts, never authoritative.
"""

import logging
import shutil
import tempfile
from pathlib import Path

from sprintbaton.harness.base import scratch_cwd_for
from sprintbaton.vcs.git_service import project_workspace_dir, task_workspace_dir

log = logging.getLogger(__name__)


def remove_tree(path: str | Path, root: str | Path) -> bool:
    """Remove `path` iff it is strictly inside `root` (spec §5.5).

    Refuses — and logs — `root` itself, any ancestor of it, and anything that
    resolves outside it. shutil.rmtree on Linux is the dir_fd-based
    implementation (`rmtree.avoids_symlink_attacks`), so an agent-planted
    symlink inside a clone pointing at `/` or at the mirror is unlinked, never
    followed. A top-level symlink is unlinked rather than traversed too.
    Returns whether anything was removed."""
    path, root = Path(path), Path(root)
    try:
        resolved_root = root.resolve()
        # Resolve the parent, not the path: a symlink *at* `path` must be
        # judged by where it sits, and then unlinked, not by where it points.
        resolved = path.parent.resolve() / path.name
    except OSError:
        log.warning("workspace cleanup refused an unresolvable path",
                    extra={"event": "workspace_cleanup_refused", "path": str(path)})
        return False
    if resolved == resolved_root or not resolved.is_relative_to(resolved_root) \
            or path.name in ("", ".", ".."):
        log.warning("workspace cleanup refused a path outside its root",
                    extra={"event": "workspace_cleanup_refused", "path": str(path),
                           "root": str(root)})
        return False
    if path.is_symlink():
        path.unlink()
        return True
    if not path.exists():
        return False
    shutil.rmtree(path)
    return True


class WorkspaceCleaner:
    """Knows where a task's, an init run's and a project's directories live,
    and removes them. Needs no credential: the path functions are pure."""

    def __init__(self, workspace_root: str, mirror_root: str,
                 harness_names: list[str] | tuple[str, ...] = (),
                 scratch_root: str | None = None):
        self.workspace_root = Path(workspace_root)
        self.mirror_root = Path(mirror_root)
        self.harness_names = tuple(harness_names)
        # Where the subprocess-CLI harnesses keep their stable per-task cwd
        # (their `scratch_root` default).
        self.scratch_root = Path(scratch_root or tempfile.gettempdir())

    def task_dir(self, user_id: str, project_id: str, task_id: str) -> Path:
        return task_workspace_dir(self.workspace_root, user_id, project_id, task_id)

    def scratch_dirs(self, task_id: str) -> list[Path]:
        return [scratch_cwd_for(self.scratch_root, name, task_id)
                for name in self.harness_names]

    def remove_task(self, user_id: str, project_id: str, task_id: str) -> None:
        """The task directory (every clone + the project index) and each
        registered harness's scratch cwd for it (spec §5.2). Raises on
        failure — the caller decides whether that is fatal."""
        remove_tree(self.task_dir(user_id, project_id, task_id), self.workspace_root)
        for scratch in self.scratch_dirs(task_id):
            remove_tree(scratch, self.scratch_root)

    def remove_init(self, user_id: str, project_id: str) -> None:
        """`users/<u>/projects/<p>/init/` after a successful init run (§5.3)."""
        remove_tree(project_workspace_dir(self.workspace_root, user_id, project_id)
                    / "init", self.workspace_root)

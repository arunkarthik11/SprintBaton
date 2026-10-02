"""On-disk location guides prepended to the project index in a prompt
(docs/storage-layout-and-git-identity-spec.md §10 q4 — the "project-context
threading gap").

The combined project index is generated once, at init time, by a model that
cannot know where anything will sit when a role actually runs: the browsing
roles get read-only clones (`<repoId>-ro`) under the task root, while the three
per-repo roles run *inside* one repo's own clone, one level deeper, with no
sibling repo checked out. A single set of paths baked into the generated prose
is therefore wrong for somebody no matter what it says — which is what it was:
every navigational pointer in the index dangled for exactly the roles that
write code.

So the index states *what* each repository is and that each carries its own
`.sprintbaton/`, and the concrete paths are generated here, per role, per turn,
from the paths that role actually holds. Nothing in this module derives a
layout — every path is passed in by the caller that already computed it
(`GitService` stays the single source of layout truth, per Part B).
"""

import os
from pathlib import Path

_READ_ONLY_NOTE = (
    "These clones are for reading only — do not edit files in them; a later "
    "role does the writing.")


def project_location_guide(cwd: Path | str,
                           repo_clones: list[tuple[object, Path]],
                           index_dir: Path | str | None = None) -> str:
    """For the project-scoped browsing roles, whose working directory is the
    task root with a read-only clone of every member repository under it.

    `repo_clones` pairs each member Repository with the clone path
    `prepare_read_only_workspace` actually returned for it.
    """
    if not repo_clones:
        return ""
    lines = [
        "## Where things are on disk (this turn)",
        "",
        "Your working directory holds a read-only clone of every repository in "
        "this project:",
        "",
    ]
    for repo, clone in repo_clones:
        rel = _rel(clone, cwd)
        role = getattr(repo, "role", "") or "unspecified role"
        lines.append(
            f"- **{getattr(repo, 'title', '')}** ({role}) — `{rel}/`; its own "
            f"detailed metadata is at `{rel}/.sprintbaton/info`")
    if index_dir is not None:
        lines.append(
            f"- the project index below is also on disk at "
            f"`{_rel(index_dir, cwd)}/info`")
    lines += ["", _READ_ONLY_NOTE, ""]
    return "\n".join(lines)


def repo_location_guide(task_id: str, repo, cwd: Path | str) -> str:
    """For the three per-repo roles (execution, review, conflict resolution),
    whose working directory *is* one repository's clone.

    Sibling repositories are deliberately not offered a path: whether any is
    checked out in this tree depends on which browsing roles ran on which
    harness earlier in the task, so promising one would reintroduce the
    dangling pointer this module exists to remove. What the role needs instead
    is to know that cross-repo work is somebody else's run.
    """
    if not cwd:
        return ""
    role = getattr(repo, "role", "") or "unspecified role"
    return "\n".join([
        "## Where things are on disk (this turn)",
        "",
        f"Your working directory is the clone of **{getattr(repo, 'title', '')}** "
        f"({role}) — the only repository this turn may change.",
        "",
        "- this repository's own detailed metadata: `.sprintbaton/info`",
        f"- this task's spec, plan, passing criteria, notes and review "
        f"comments: `.sprintbaton/tasks/{task_id}/`",
        "",
        "The project's other repositories are **not** checked out here. "
        "SprintBaton works one repository at a time and opens a separate pull "
        "request for each, so if this task also needs a change elsewhere, say "
        "so in your summary rather than trying to make it from here.",
        "",
    ])


def _rel(path: Path | str, cwd: Path | str) -> str:
    """Path relative to the role's working directory, falling back to the
    absolute path when the two share no root (which a prompt can still use)."""
    try:
        return os.path.relpath(str(path), str(cwd))
    except ValueError:  # different drives on Windows
        return str(path)


def init_repo_location_guide(repo, clone: Path | str, metadata_dir: Path | str) -> str:
    """For the per-repo metadata init pass (project-initialization-task spec
    §8.4): the working directory is one repository's clone, read-only, and its
    `.sprintbaton/` is the one directory the run may write — pre-populated with
    the current revision (empty on a first run)."""
    role = getattr(repo, "role", "") or "unspecified role"
    return "\n".join([
        "## Where things are on disk (this turn)",
        "",
        f"Your working directory is a fresh clone of **{getattr(repo, 'title', '')}** "
        f"({role}). Treat the repository itself as read-only.",
        "",
        f"- the metadata directory you create and edit: `{_rel(metadata_dir, clone)}/` "
        f"(absolute: `{metadata_dir}`) — the only place you may write",
        "- it is pre-populated with this repository's current published "
        "metadata, if any exists",
        "",
    ])


def init_project_location_guide(metadata_dir: Path | str) -> str:
    """For the project-index init pass (spec §8.4): the working directory is
    the writable project metadata directory itself. No repository is checked
    out — each member's current `info` index is in the prompt."""
    return "\n".join([
        "## Where things are on disk (this turn)",
        "",
        f"Your working directory is the project metadata directory (`{metadata_dir}`) "
        "— the one place you may write, pre-populated with the project's current "
        "published index, if any exists.",
        "",
        "No repository is checked out for this pass: each member repository's "
        "current `.sprintbaton/info` index is included below.",
        "",
    ])

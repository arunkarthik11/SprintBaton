"""The one place a blob-store key is built (docs/storage-layout-and-git-identity-spec.md
Part A).

Every key SprintBaton writes lives in one tenant-rooted tree::

    users/<userId>/projects/<projectId>/
    ├── metadata/<revision>/<rel>                 # project index (project init pass)
    ├── repos/<repoId>/metadata/<revision>/<rel>  # per-repo init pass output
    ├── tasks/<taskId>/artifacts/<file>     # durable handoff outputs (Task.*Action)
    ├── tasks/<taskId>/agent/<file>         # agent process records
    └── provenance/<file>                   # the provenance git bundle

The literal `users/` / `projects/` / `repos/` / `tasks/` segments are
deliberate rather than a bare `<userId>/<projectId>/…`: they keep entity-id
segments from ever being adjacent, so a key is unambiguous read without
context, and the tree is self-describing when browsed.

Two invariants this module exists to hold:

1. **No call site outside `sprintbaton/storage/` concatenates a key.** Its
   absence is what let two of the four namespaces drift into raw f-strings in
   `metadata/generator.py`, unkeyed by project and unkeyed by owner; a test
   (`tests/storage/test_key_layout.py`) now scans for regressions.
2. **Every segment passes through `safe_segment`** — including each
   `/`-separated component of a model-supplied `rel`. `rel` comes straight out
   of the generation model's structured output, and `FilesystemBlobStore._put`
   does `self._root / key`, so an unsanitized `../../escape.md` would be a
   write-outside-root primitive.

Both backends mix this in, so keys stay byte-identical across them
(docs/zero-infra-storage-spec.md §13).

Metadata is revisioned (docs/project-initialization-task-spec.md §9.1): each
successful init pass publishes an immutable tree under `<revision>` — the init
task id that produced it — and the Repository/Project row's metadataRevision
pointer names the current one. The `*_metadata_root` builders are the parent
of every revision, for garbage collection.
"""

from sprintbaton.storage.base import safe_segment

# Filenames carry more than an entity id — "situation-report-<repoId>-<ts>.md"
# is already 70 chars — so they get a longer cap than the id segments, which
# stay at safe_segment's default. Truncating a filename would silently collide
# two distinct artifacts.
FILENAME_MAX = 128


def revision_filename(filename: str, revision: int) -> str:
    """`finalized-spec.md` -> `finalized-spec.r3.md` (task-revisions spec
    §5.4): a rerun after a card edit writes a new file instead of overwriting
    the previous one. Older files are kept; `Task.*Action` only ever points at
    the current one."""
    stem, dot, ext = filename.rpartition(".")
    if not dot:
        return f"{filename}.r{revision}"
    return f"{stem}.r{revision}.{ext}"


def _rel_segments(rel: str) -> list[str]:
    """Sanitize a model-supplied relative path into key segments. Components
    that sanitize to empty, or are `.` / `..`, are dropped — traversal can
    never escape the namespace it was built for."""
    out = []
    for raw in (rel or "").split("/"):
        if raw in ("", ".", ".."):
            continue
        segment = safe_segment(raw, FILENAME_MAX)
        if segment and segment not in (".", ".."):
            out.append(segment)
    return out


def _rel(rel: str) -> str:
    return "/".join(_rel_segments(rel))


def _project_root(user_id: str, project_id: str) -> str:
    return f"users/{safe_segment(user_id)}/projects/{safe_segment(project_id)}"


class BlobKeyMixin:
    """The six key/prefix builders every BlobStore backend shares verbatim."""

    def task_key(self, user_id: str, project_id: str, task_id: str,
                 category: str, filename: str) -> str:
        """category: "artifacts" (durable handoff outputs referenced by a
        Task.*Action URL and read back as another role's input) | "agent"
        (process/audit records — situation reports, conversation transcripts)."""
        return (f"{_project_root(user_id, project_id)}/tasks/"
                f"{safe_segment(task_id)}/{safe_segment(category)}/"
                f"{safe_segment(filename, FILENAME_MAX)}")

    def repo_metadata_key(self, user_id: str, project_id: str, repo_id: str,
                          revision: str, rel: str) -> str:
        """One file of a member repo's `.sprintbaton/` init-pass output, under
        one immutable revision. `rel` is model-supplied and sanitized
        component-wise."""
        return (f"{self.repo_metadata_prefix(user_id, project_id, repo_id, revision)}"
                f"{_rel(rel)}")

    def repo_metadata_prefix(self, user_id: str, project_id: str,
                             repo_id: str, revision: str) -> str:
        """Trailing-slash prefix for list_keys over one revision of one repo's
        metadata."""
        return (f"{self.repo_metadata_root(user_id, project_id, repo_id)}"
                f"{safe_segment(revision)}/")

    def repo_metadata_root(self, user_id: str, project_id: str,
                           repo_id: str) -> str:
        """The parent of every revision of one repo's metadata — what GC lists
        (project-initialization-task spec §9.3)."""
        return (f"{_project_root(user_id, project_id)}/repos/"
                f"{safe_segment(repo_id)}/metadata/")

    def project_metadata_key(self, user_id: str, project_id: str,
                             revision: str, rel: str) -> str:
        """One file of the combined project index (the outer init pass)."""
        return (f"{self.project_metadata_prefix(user_id, project_id, revision)}"
                f"{_rel(rel)}")

    def project_metadata_prefix(self, user_id: str, project_id: str,
                                revision: str) -> str:
        return (f"{self.project_metadata_root(user_id, project_id)}"
                f"{safe_segment(revision)}/")

    def project_metadata_root(self, user_id: str, project_id: str) -> str:
        return f"{_project_root(user_id, project_id)}/metadata/"

    def provenance_key(self, user_id: str, project_id: str, filename: str) -> str:
        """Project-level (not per-task) key for the provenance snapshot store
        (classification provenance spec §7)."""
        return (f"{_project_root(user_id, project_id)}/provenance/"
                f"{safe_segment(filename, FILENAME_MAX)}")

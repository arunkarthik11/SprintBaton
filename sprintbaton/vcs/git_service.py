"""Git / PR / branch promotion.

Cloning, branching, committing and pushing run as subprocesses in the workspace;
PR creation and merging go through the GitHub REST API. Promotion follows the
three-branch model (dev -> staging -> production): the release-window job cuts
dev -> staging, and the orchestrator's QA gate promotes staging -> production.
"""

import hashlib
import logging
import os
import re
import shutil
import stat
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import httpx

from sprintbaton.storage.base import safe_segment
from sprintbaton.storage.lock_fs import FileLock

log = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"

# Tier 4 of the git-identity resolution order (storage-layout-and-git-identity
# spec §5.2): what a deployment that configures nothing commits as. The
# users.noreply.github.com domain is accepted by GitHub without a verified
# address and never resolves to a real mailbox.
DEFAULT_AUTHOR_NAME = "SprintBaton"
DEFAULT_AUTHOR_EMAIL = "sprintbaton@users.noreply.github.com"

# stderr patterns that mean "the credential was rejected", not "the operation
# failed" (spec §5.6). Misconfigured credentials are the most likely
# first-deployment failure, and "git command failed" is not actionable.
_AUTH_FAILURE_RE = re.compile(
    r"authentication failed|could not read (username|password)|"
    r"permission denied|403 forbidden|invalid username or password|"
    r"terminal prompts disabled|support for password authentication was removed",
    re.IGNORECASE,
)

# Stderr of a git failure caused by an unreachable remote rather than by the
# command itself (project-initialization-task spec §5.6): name resolution,
# connection timeouts/resets, 5xx from the host, and a transfer that dropped
# mid-stream. Checked only after _AUTH_FAILURE_RE, so an auth failure is never
# retried.
_TRANSIENT_FAILURE_RE = re.compile(
    r"could not resolve host|temporary failure in name resolution|"
    r"name or service not known|could not resolve hostname|"
    r"connection timed out|operation timed out|timed out after|"
    r"connection reset|connection refused|failed to connect|"
    r"network is unreachable|no route to host|"
    r"the requested url returned error: 5\d\d|"
    r"early eof|rpc failed|unexpected disconnect|"
    r"the remote end hung up unexpectedly|gnutls_handshake\(\) failed|"
    r"ssl_read|ssl_error_syscall",
    re.IGNORECASE,
)

# Credential shapes that must never reach a log line, an exception, or a task
# comment. Git does not normally echo credentials, but a malformed
# embedded-credential remote URL can appear verbatim in an error message.
_TOKEN_SHAPE_RE = re.compile(
    r"gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{16,}")
_URL_CREDENTIAL_RE = re.compile(r"(?<=://)[^/\s:@]+:[^/\s@]+(?=@)")

_REDACTED = "***"


def redact_secrets(text: str, *secrets: str) -> str:
    """Scrub tokens out of text before it is surfaced (spec §5.6). Removes the
    literal secrets we know we handed to git, plus anything token-shaped and
    any embedded `user:password@` in a URL."""
    for secret in secrets:
        if secret and len(secret) >= 8:
            text = text.replace(secret, _REDACTED)
    text = _TOKEN_SHAPE_RE.sub(_REDACTED, text)
    return _URL_CREDENTIAL_RE.sub(_REDACTED, text)


class GitAuthenticationError(RuntimeError):
    """A git subprocess failed because the credential was rejected or absent.

    Subclasses RuntimeError so every existing `except RuntimeError` around
    GitService keeps catching it (spec §5.6); callers that care can single it
    out to report an operator problem rather than a task problem.
    """


class GitTransientError(RuntimeError):
    """A git command failed because the remote was unreachable, not because of
    the command (project-initialization-task spec §5.6) — the init pass
    retries it with backoff. Like GitAuthenticationError, a RuntimeError
    subclass, so every existing `except RuntimeError` keeps working."""


def resolve_git_identity(repo, project, settings) -> tuple[str, str]:
    """The four-tier commit-identity resolution order (spec §5.2), most
    specific first: repo override -> project default -> deployment Settings ->
    the built-in constants. Tier 4 is what makes a zero-config install commit
    successfully instead of failing with "Author identity unknown"."""
    name = (getattr(repo, "gitAuthorName", None)
            or getattr(project, "gitAuthorName", None)
            or getattr(settings, "sprintbaton_git_author_name", "")
            or DEFAULT_AUTHOR_NAME)
    email = (getattr(repo, "gitAuthorEmail", None)
             or getattr(project, "gitAuthorEmail", None)
             or getattr(settings, "sprintbaton_git_author_email", "")
             or DEFAULT_AUTHOR_EMAIL)
    return name, email


def build_git_service(token: str, settings, repo, project=None) -> "GitService":
    """The one construction path for a repo-scoped GitService, shared by
    ServiceContext.git_for and the API's own git_for so the identity, the
    workspace project level and the credential plumbing can never be wired at
    only one of the two sites."""
    author_name, author_email = resolve_git_identity(repo, project, settings)
    source = (f"repository credential {repo.githubCredentialId}"
              if getattr(repo, "githubCredentialId", None)
              else "the user's default GitHub credential (or the GITHUB_TOKEN "
                   "deployment fallback)")
    return GitService(
        token, settings.workspace_root, repo.userId, repo.projectId,
        exclude_sprintbaton=settings.sprintbaton_provenance_exclude_from_user_repo,
        mirror_root=settings.mirror_root,
        repo_id=repo.id,
        remote_url=getattr(repo, "remoteUrl", ""),
        author_name=author_name, author_email=author_email,
        ssh_key_path=getattr(settings, "sprintbaton_git_ssh_key_path", ""),
        credential_source=source,
    )


# How long a caller waits for a mirror's flock: the git subprocess timeout, so
# a lock can never be the thing that stalls longer than the git call it guards
# (workspace-mirrors-and-cleanup spec §4.5).
MIRROR_LOCK_WAIT_SECONDS = 600
_GIT_TIMEOUT_SECONDS = 600


def path_segment(value: str) -> str:
    """One id as a workspace path segment. safe_segment keeps `.`, so a bare
    `.`/`..` (or an empty id) would still navigate — those become `_`."""
    seg = safe_segment(value or "")
    return "_" if seg in ("", ".", "..") else seg


def project_workspace_dir(workspace_root: str | Path, user_id: str,
                          project_id: str) -> Path:
    """`<root>/users/<userId>/projects/<projectId>` — the same literal
    segments as the blob keys (storage/keys.py), so a workspace path and the
    matching blob prefix read identically (workspace-mirrors-and-cleanup spec
    §3.1). With task_workspace_dir and project_init_metadata_dir, the only
    builders of a tenant path outside GitService: cleanup can compute one
    without constructing a GitService (and so without a credential)."""
    return (Path(workspace_root) / "users" / path_segment(user_id)
            / "projects" / path_segment(project_id))


def task_workspace_dir(workspace_root: str | Path, user_id: str,
                       project_id: str, task_id: str) -> Path:
    """The per-task directory holding every member repo's clone plus the
    combined project index (multi-repo-project spec §6)."""
    return (project_workspace_dir(workspace_root, user_id, project_id)
            / "tasks" / path_segment(task_id))


def project_init_metadata_dir(workspace_root: str | Path, user_id: str,
                              project_id: str) -> Path:
    """`<root>/users/<u>/projects/<p>/init/.sprintbaton-project`: where the
    project init pass edits the combined index in place (project-
    initialization-task spec §8.2) — beside the per-repo init clones, inside
    none of them. A pure path function, so the project pass needs no member
    repo's credentials."""
    return (project_workspace_dir(workspace_root, user_id, project_id)
            / "init" / ".sprintbaton-project")


def mirror_dir(mirror_root: str | Path, user_id: str, project_id: str,
               repo_id: str) -> Path:
    """`<mirror_root>/users/<u>/projects/<p>/repos/<r>.git` (workspace-mirrors
    spec §4.2). Keyed by (user, project) deliberately: a mirror shared across
    tenants would let a user without GitHub access to a private repo read it
    through SprintBaton, and a per-tenant mirror is refreshed with that
    tenant's own credential every time it is used."""
    return (Path(mirror_root) / "users" / path_segment(user_id) / "projects"
            / path_segment(project_id) / "repos" / f"{path_segment(repo_id)}.git")


class _MirrorCorrupt(RuntimeError):
    """A mirror refresh failed for a reason that is neither authentication nor
    an unreachable remote — the mirror itself is suspect (spec §4.6)."""


def _is_ssh_remote(remote_url: str) -> bool:
    url = (remote_url or "").strip()
    return url.startswith(("ssh://", "git+ssh://")) or (
        "@" in url.split("/", 1)[0] and ":" in url and not url.startswith("http"))


class GitService:
    def __init__(self, github_token: str, workspace_root: str, user_id: str,
                 project_id: str, exclude_sprintbaton: bool = True, *,
                 mirror_root: str,
                 repo_id: str = "",
                 remote_url: str = "",
                 author_name: str = "", author_email: str = "",
                 ssh_key_path: str = "", credential_source: str = ""):
        self._token = github_token
        self._root = Path(workspace_root)
        # Every clone this service makes lives under its tenant and project
        # (workspace-mirrors-and-cleanup spec §3.1, which made the storage-
        # layout spec's project level tenant-rooted too): one tenant's files
        # sit under one path prefix, so per-tenant deletion/inspection and the
        # shipped-task sweep are path operations. user_id and project_id are
        # required positionals: an optional one would silently reintroduce
        # the ungrouped layout wherever it was forgotten.
        self._user_id = user_id
        self._project_id = project_id
        self._project_root = project_workspace_dir(self._root, user_id, project_id)
        self._project_root.mkdir(parents=True, exist_ok=True)
        # Local bare mirrors (spec §4): every clone and fetch reads from one,
        # refreshed from the remote first. A required keyword so no
        # construction site can silently opt out of the cache.
        self._mirror_root = Path(mirror_root)
        self._repo_id = repo_id
        # Whether to keep .sprintbaton/ out of commits to the user's repo
        # (classification provenance spec §8). Default preserves today's
        # behaviour; an operator who wants their own VCS to track it opts out.
        self._exclude_sprintbaton = exclude_sprintbaton
        # Scheme detection only (spec §5.5): an SSH remote gets GIT_SSH_COMMAND
        # and never GIT_ASKPASS, which would answer a passphrase prompt with a
        # GitHub token.
        self._remote_url = remote_url
        self._author_name = author_name or DEFAULT_AUTHOR_NAME
        self._author_email = author_email or DEFAULT_AUTHOR_EMAIL
        self._ssh_key_path = ssh_key_path
        # Human-readable description of which credential tier resolved the
        # token, for GitAuthenticationError messages (spec §5.6).
        self._credential_source = credential_source or "the resolved credential"
        self._askpass: tempfile.TemporaryDirectory | None = None
        self._gh = httpx.Client(
            base_url=GITHUB_API,
            headers={
                "Authorization": f"Bearer {github_token}",
                "Accept": "application/vnd.github+json",
            },
            timeout=30,
        )

    # --- credentials to the git subprocess ---------------------------------

    def _askpass_script(self) -> str:
        """A short-lived 0700 helper that echoes the token from its own
        environment. GIT_ASKPASS is git's documented, credential-helper-
        independent hook, and it is the only mechanism that keeps the token out
        of argv (world-readable via /proc/<pid>/cmdline), out of .git/config,
        and out of any file that outlives the call (spec §5.4)."""
        if self._askpass is None:
            self._askpass = tempfile.TemporaryDirectory(prefix="sprintbaton-git-")
            os.chmod(self._askpass.name, stat.S_IRWXU)
            script = Path(self._askpass.name) / "askpass.sh"
            script.write_text(
                '#!/bin/sh\nprintf \'%s\' "$SPRINTBATON_GIT_TOKEN"\n')
            script.chmod(stat.S_IRWXU)
        return str(Path(self._askpass.name) / "askpass.sh")

    def _env(self) -> dict[str, str]:
        """The per-call environment overlay for every git subprocess.

        Built as {**os.environ, …} and passed to subprocess.run(env=…) — never
        exported into the worker's own os.environ, so harness subprocesses
        (which inherit the worker's environment and have Bash in the very
        workspace this token can push to) never see it. Same
        overlay-never-mutate discipline as the model-provider keys
        (docs/per-user-provider-credentials-spec.md)."""
        env = {
            **os.environ,
            "GIT_TERMINAL_PROMPT": "0",   # fail fast instead of hanging on a TTY prompt
            "GIT_CONFIG_NOSYSTEM": "1",   # ignore /etc/gitconfig
        }
        if _is_ssh_remote(self._remote_url):
            if self._ssh_key_path:
                env["GIT_SSH_COMMAND"] = (
                    f"ssh -i {self._ssh_key_path} -o IdentitiesOnly=yes")
            # No GIT_ASKPASS: over SSH it would be asked for a key passphrase,
            # not a token.
            return env
        if self._token:
            env["GIT_ASKPASS"] = self._askpass_script()
            env["SPRINTBATON_GIT_TOKEN"] = self._token
        return env

    # --- local git ---------------------------------------------------------

    def task_root(self, task_id: str) -> Path:
        """The per-task directory that holds every member repo's clone plus the
        combined project-metadata index (multi-repo-project spec §6)."""
        return task_workspace_dir(self._root, self._user_id, self._project_id,
                                  task_id)

    def project_index_dir(self, task_id: str) -> Path:
        """Where the combined project index is materialized — outside every
        repo clone so it is never committed (multi-repo-project spec §6)."""
        return self.task_root(task_id) / ".sprintbaton-project"

    def workspace_for(self, task_id: str, repo_id: str = "") -> Path:
        # repo_id="" keeps the flat layout (init pass, single-clone callers,
        # pre-multi-repo tests). A repo_id nests the clone under the task root
        # so several repos coexist for one task (multi-repo-project spec §6).
        if repo_id:
            return self.task_root(task_id) / "repos" / path_segment(repo_id)
        return self.task_root(task_id)

    def prepare_workspace(self, task_id: str, remote_url: str, base_branch: str,
                          work_branch: str, repo_id: str = "") -> Path:
        workspace = self.workspace_for(task_id, repo_id)
        workspace.parent.mkdir(parents=True, exist_ok=True)
        if not (workspace / ".git").exists():
            self._clone_via_mirror(remote_url, base_branch, workspace, repo_id)
        else:
            current = self._run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                                cwd=workspace).strip()
            if current == work_branch:
                # Mid-task re-entry (clarification pause, AI-review bounce,
                # crash recovery): the workspace left behind by the previous
                # turn is in a continuable state — reuse it as-is, keeping
                # uncommitted work (conversation-lifecycle spec §9.1). A crash
                # mid-conflict-resolution can leave a merge in progress here —
                # abort it (a no-op otherwise) so the Coding Model never sees
                # conflict markers it isn't prompted to expect
                # (conflict-resolution spec §5.4).
                self.abort_merge(workspace)
                self._ensure_local_exclude(workspace)
                return workspace
            self._fetch_via_mirror(remote_url, workspace, repo_id)
            self._run(["git", "checkout", base_branch], cwd=workspace)
            self._run(["git", "reset", "--hard", f"origin/{base_branch}"], cwd=workspace)
        # A work branch that was already pushed holds the task's earlier work:
        # continue on top of it instead of recreating it from the base, which
        # would make the next non-force push fail (task-revisions spec §8.5;
        # workspace-mirrors spec §10 q7 — the mirror carries every head).
        start = (f"origin/{work_branch}" if self._has_ref(
            workspace, f"refs/remotes/origin/{work_branch}") else "HEAD")
        self._run(["git", "checkout", "-B", work_branch, start], cwd=workspace)
        self._ensure_local_exclude(workspace)
        return workspace

    def _has_ref(self, workspace: Path, ref: str) -> bool:
        return subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", ref], cwd=workspace,
            capture_output=True, timeout=60, env=self._env()).returncode == 0

    def read_only_workspace_for(self, task_id: str, repo_id: str = "") -> Path:
        # Suffixed so a task's read-only clone never collides with its
        # execution workspace (workspace_for). repo_id nests it under the task
        # root for multi-repo browsing (multi-repo-project spec §6).
        if repo_id:
            return self.task_root(task_id) / "repos" / f"{path_segment(repo_id)}-ro"
        return self._project_root / "tasks" / f"{path_segment(task_id)}-ro"

    def prepare_read_only_workspace(self, task_id: str, remote_url: str,
                                    base_branch: str, repo_id: str = "") -> Path:
        """Checkout-only clone for the read-only roles (execution-tier-agents
        spec §9.6): no work branch created, never pushed. Reused per task and
        refreshed to origin/<base_branch> on every call."""
        workspace = self.read_only_workspace_for(task_id, repo_id)
        workspace.parent.mkdir(parents=True, exist_ok=True)
        if not (workspace / ".git").exists():
            self._clone_via_mirror(remote_url, base_branch, workspace, repo_id)
        else:
            self._fetch_via_mirror(remote_url, workspace, repo_id)
            self._run(["git", "checkout", base_branch], cwd=workspace)
            self._run(["git", "reset", "--hard", f"origin/{base_branch}"], cwd=workspace)
        self._ensure_local_exclude(workspace)
        return workspace

    def init_workspace_for(self, repo_id: str) -> Path:
        """Where a repo's init-pass clone lives (spec §4.1)."""
        return self._project_root / "init" / path_segment(repo_id)

    def prepare_init_workspace(self, repo_id: str, remote_url: str,
                               branch: str) -> Path:
        """Checkout-only clone for the metadata init pass (spec §4.3).

        Semantically identical to prepare_read_only_workspace — clone, or fetch
        and hard-reset to origin/<branch> — but with its own path so an init
        clone is distinguishable from a task's. It replaces the previous misuse
        of prepare_workspace, which created a `sprintbaton/init-<repoId>` work
        branch that was never committed to and never pushed, for what is a
        read-only browse."""
        workspace = self.init_workspace_for(repo_id)
        workspace.parent.mkdir(parents=True, exist_ok=True)
        if not (workspace / ".git").exists():
            self._clone_via_mirror(remote_url, branch, workspace, repo_id)
        else:
            self._fetch_via_mirror(remote_url, workspace, repo_id)
            self._run(["git", "checkout", branch], cwd=workspace)
            self._run(["git", "reset", "--hard", f"origin/{branch}"], cwd=workspace)
        self._ensure_local_exclude(workspace)
        return workspace

    # --- local bare mirrors (workspace-mirrors-and-cleanup spec §4) ---------

    def mirror_path(self, repo_id: str = "", remote_url: str = "") -> Path:
        """This repo's mirror. Keyed by repo id; a service built without one
        (unit tests, ad-hoc callers) keys by a hash of the remote URL — still
        inside this (user, project), so the tenancy guarantee holds."""
        key = repo_id or self._repo_id
        if not key:
            url = remote_url or self._remote_url
            key = "remote-" + hashlib.sha256(url.encode()).hexdigest()[:16]
        return mirror_dir(self._mirror_root, self._user_id, self._project_id, key)

    @contextmanager
    def _mirror_lock(self, mirror: Path) -> Iterator[None]:
        """A host-local flock per mirror, held across the refresh and the clone
        or fetch that follows (spec §4.5). Always a local flock whatever
        SPRINTBATON_LOCK_BACKEND says: the mirror is per host, so a Redis lock
        would serialize pods that share nothing."""
        key = "__".join(mirror.relative_to(self._mirror_root).parts[1::2])
        locker = FileLock(str(self._mirror_root / ".locks"))
        with locker.lock(key, wait_seconds=MIRROR_LOCK_WAIT_SECONDS) as acquired:
            if not acquired:
                raise GitTransientError(f"timed out waiting for the mirror lock on {mirror}")
            yield

    def _remote_for(self, remote_url: str, workspace: Path | None) -> str:
        if remote_url:
            return remote_url
        if self._remote_url:
            return self._remote_url
        if workspace is not None:
            return self._run(["git", "remote", "get-url", "origin"],
                             cwd=workspace).strip()
        raise RuntimeError("no remote URL to refresh the mirror from")

    def _refresh_mirror(self, mirror: Path, remote_url: str) -> None:
        """Create the mirror lazily, else fetch only what changed (spec §4.3).
        Never falls back to a stale mirror: auth and network failures raise
        exactly as a direct clone would. A mirror failing for any other reason
        is deleted and recreated once, then the failure surfaces (§4.6)."""
        try:
            self._refresh_mirror_once(mirror, remote_url)
        except _MirrorCorrupt as exc:
            log.warning("mirror_recreated", extra={"mirror": str(mirror),
                                                   "reason": str(exc)[:500]})
            shutil.rmtree(mirror, ignore_errors=True)
            try:
                self._refresh_mirror_once(mirror, remote_url)
            except _MirrorCorrupt as again:
                raise RuntimeError(str(again)) from again

    def _refresh_mirror_once(self, mirror: Path, remote_url: str) -> None:
        try:
            if not (mirror / "HEAD").exists():
                shutil.rmtree(mirror, ignore_errors=True)
                mirror.parent.mkdir(parents=True, exist_ok=True)
                # Not `clone --mirror`: its +refs/*:refs/* refspec also pulls
                # GitHub's refs/pull/*, one ref per PR ever opened.
                self._run(["git", "clone", "--bare", "--no-tags", remote_url,
                           str(mirror)], cwd=mirror.parent)
                self._run(["git", "config", "remote.origin.fetch",
                           "+refs/heads/*:refs/heads/*"], cwd=mirror)
                return
            # A re-pointed Repository just works.
            self._run(["git", "remote", "set-url", "origin", remote_url], cwd=mirror)
            self._run(["git", "fetch", "--prune", "--no-tags", "origin"], cwd=mirror)
        except (GitAuthenticationError, GitTransientError):
            raise
        except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
            raise _MirrorCorrupt(str(exc)) from exc

    def _clone_via_mirror(self, remote_url: str, branch: str, workspace: Path,
                          repo_id: str = "") -> None:
        """Refresh the mirror, then clone from it by path (spec §4.4).

        A path, never --local: an implicit local clone hardlinks the object
        files and falls back to copying when link(2) fails (EXDEV across
        mounts), while an explicit --local aborts. Hardlinks, not alternates:
        the clone owns its objects, so a gc on the mirror can never prune one a
        live task depends on. origin is re-pointed at the real remote, so
        pushes and every URL in the logs are exactly what they were."""
        remote = self._remote_for(remote_url, None)
        mirror = self.mirror_path(repo_id, remote)
        with self._mirror_lock(mirror):
            self._refresh_mirror(mirror, remote)
            self._run(["git", "clone", "--no-tags", "--branch", branch,
                       str(mirror), str(workspace)], cwd=workspace.parent)
        self._run(["git", "remote", "set-url", "origin", remote], cwd=workspace)

    def _fetch_via_mirror(self, remote_url: str, workspace: Path,
                          repo_id: str = "") -> None:
        """Refresh the mirror, then update the clone's origin/* from it —
        a local fetch that never touches the network, so every existing
        `origin/<branch>` reference keeps its meaning (spec §4.4)."""
        remote = self._remote_for(remote_url, workspace)
        mirror = self.mirror_path(repo_id, remote)
        with self._mirror_lock(mirror):
            self._refresh_mirror(mirror, remote)
            self._run(["git", "fetch", "--prune", "--no-tags", str(mirror),
                       "+refs/heads/*:refs/remotes/origin/*"], cwd=workspace)

    def _ensure_local_exclude(self, workspace: Path) -> None:
        """Keep .sprintbaton/ (the per-task workspace, task-workspace spec §7)
        out of every commit SprintBaton makes without touching the project's
        tracked .gitignore: .git/info/exclude has .gitignore semantics but is
        scoped to the local clone only — never pushed, never visible to the
        user's repository. Skipped when an operator opts into tracking
        .sprintbaton/ themselves (classification provenance spec §8)."""
        if not self._exclude_sprintbaton:
            return
        exclude = workspace / ".git" / "info" / "exclude"
        if exclude.exists() and ".sprintbaton/" in exclude.read_text():
            return
        exclude.parent.mkdir(parents=True, exist_ok=True)
        with exclude.open("a") as f:
            f.write("\n.sprintbaton/\n")

    def merge_conflict_files(self, workspace: Path, base_branch: str) -> list[str]:
        """Dry-run test-merge of origin/<base_branch> into the current branch —
        the same computation GitHub performs to decide PR mergeability, done
        locally so it's available synchronously at PR-open time instead of
        waiting on GitHub's eventually-consistent `mergeable` field
        (conflict-resolution spec §5.2). On conflict, leaves the merge in
        progress (conflict markers on disk, MERGE_HEAD set) for the Conflict
        Resolution Agent to resolve in place. On a clean merge, aborts
        immediately so history is untouched until a human actually merges the
        PR — identical to today's behavior in the no-conflict case."""
        self._fetch_via_mirror("", workspace)
        # Deliberately not _run: a conflicted merge *is* a non-zero exit this
        # method needs to inspect, not treat as a failure.
        result = subprocess.run(
            ["git", "merge", "--no-commit", "--no-ff", f"origin/{base_branch}"],
            cwd=workspace, capture_output=True, text=True, timeout=600,
            env=self._env(),
        )
        if result.returncode == 0:
            # Best-effort: an "Already up to date." merge leaves no MERGE_HEAD
            # to abort, and a strict abort would fail loudly on it.
            self.abort_merge(workspace)
            return []
        conflicted = self._run(
            ["git", "diff", "--name-only", "--diff-filter=U"], cwd=workspace
        ).splitlines()
        return [f for f in conflicted if f.strip()]

    def abort_merge(self, workspace: Path) -> None:
        """Best-effort cleanup — a no-op if no merge is in progress."""
        subprocess.run(["git", "merge", "--abort"], cwd=workspace,
                       capture_output=True, text=True, timeout=60,
                       env=self._env())

    def commit_and_push(self, workspace: Path, branch: str, message: str) -> bool:
        self._run(["git", "add", "-A"], cwd=workspace)
        status = self._run(["git", "status", "--porcelain"], cwd=workspace)
        if not status.strip():
            return False
        # Identity as -c overrides, never `git config` (spec §5.1): it must
        # not persist into the clone's .git/config, where it survives and is
        # readable by any agent with Bash in this workspace. git derives both
        # author and committer from user.* when GIT_AUTHOR_*/GIT_COMMITTER_*
        # are unset, so the identity is uniform.
        self._run(["git",
                   "-c", f"user.name={self._author_name}",
                   "-c", f"user.email={self._author_email}",
                   "commit", "-m", message], cwd=workspace)
        self._run(["git", "push", "-u", "origin", branch], cwd=workspace)
        return True

    def remote_head_sha(self, remote_url: str, branch: str) -> str:
        """Resolve a branch's tip SHA on the remote without a clone (git
        ls-remote), giving the provenance store a 40-byte handle on the whole
        codebase the task was based on (classification provenance spec §2).
        Best-effort — returns "" on any failure."""
        if not remote_url:
            return ""
        try:
            out = self._run(
                ["git", "ls-remote", remote_url, f"refs/heads/{branch}"],
                cwd=self._project_root)
        except (RuntimeError, subprocess.SubprocessError):
            return ""
        parts = out.split()
        return parts[0] if parts else ""

    def _run(self, cmd: list[str], cwd: Path | str) -> str:
        """Every git subprocess goes through here so the credential overlay and
        the stderr redaction can never be forgotten at one call site."""
        result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                                timeout=_GIT_TIMEOUT_SECONDS, env=self._env())
        if result.returncode != 0:
            stderr = redact_secrets(result.stderr, self._token)
            printable = redact_secrets(" ".join(cmd), self._token)
            if _AUTH_FAILURE_RE.search(stderr):
                raise GitAuthenticationError(
                    f"git authentication failed using {self._credential_source}: "
                    f"{printable}\n{stderr}")
            if _TRANSIENT_FAILURE_RE.search(stderr):
                raise GitTransientError(
                    f"git remote unreachable: {printable}\n{stderr}")
            raise RuntimeError(f"git command failed: {printable}\n{stderr}")
        return result.stdout

    # --- GitHub API --------------------------------------------------------

    def open_pull_request(self, github_repo: str, head: str, base: str,
                          title: str, body: str) -> str:
        resp = self._gh.post(f"/repos/{github_repo}/pulls",
                             json={"title": title, "head": head, "base": base, "body": body})
        resp.raise_for_status()
        return resp.json()["html_url"]

    def merge_pull_request(self, github_repo: str, pr_number: int) -> None:
        resp = self._gh.put(f"/repos/{github_repo}/pulls/{pr_number}/merge",
                            json={"merge_method": "squash"})
        resp.raise_for_status()

    def pull_request_merged(self, github_repo: str, pr_number: int) -> bool:
        resp = self._gh.get(f"/repos/{github_repo}/pulls/{pr_number}/merge")
        if resp.status_code == 204:
            return True
        if resp.status_code == 404:
            return False
        resp.raise_for_status()
        return False

    @staticmethod
    def pr_number_from_url(pr_url: str) -> int | None:
        match = re.search(r"/pull/(\d+)", pr_url or "")
        return int(match.group(1)) if match else None

    def promote(self, github_repo: str, from_branch: str, to_branch: str, title: str) -> str:
        """Open a promotion PR (dev -> staging, staging -> production).

        Returns "" when the branches are already identical (GitHub rejects the
        PR with 422 "no commits between") — the promotion is trivially done.
        """
        resp = self._gh.post(f"/repos/{github_repo}/pulls",
                             json={"title": title, "head": from_branch, "base": to_branch,
                                   "body": "Automated promotion by SprintBaton."})
        if resp.status_code == 422:
            log.info("promotion PR skipped — no commits between branches",
                     extra={"repo": github_repo, "from": from_branch, "to": to_branch})
            return ""
        resp.raise_for_status()
        return resp.json()["html_url"]

    def list_pr_review_comments(self, github_repo: str, pr_number: int) -> list[str]:
        resp = self._gh.get(f"/repos/{github_repo}/pulls/{pr_number}/comments")
        resp.raise_for_status()
        return [c.get("body", "") for c in resp.json()]

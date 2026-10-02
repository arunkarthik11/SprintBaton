"""Worker-side transfer: building what goes into a session, and applying what
comes back (hosted-sandbox-isolation spec §6.3–§6.4).

Everything here runs in the worker and treats the sandbox's output as hostile
data. `apply_changeset` is the security-critical function of the whole design:
it is the only place bytes produced by untrusted code are written into the
worker's authoritative clone, which the worker's own git later commits.
"""

from __future__ import annotations

import errno
import hashlib
import io
import json
import logging
import os
import stat
import subprocess
import tarfile
import tempfile
from collections.abc import Iterable
from pathlib import Path

from sprintbaton.sandbox.base import (
    ChangeSet,
    ChangeSetRejected,
    FileChange,
    normalize_rel,
)

log = logging.getLogger(__name__)

GIT_TIMEOUT_SECONDS = 120
_HASH_CHUNK = 1024 * 1024


# ------------------------------------------------------------------ snapshot


def file_digest(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_HASH_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def build_manifest(root: str | Path) -> tuple[dict[str, dict], list[str]]:
    """The working tree of `root` without any `.git` (§6.3), as
    {rel: {"kind", "sha"/"target", "mode"}}, plus the tree-relative roots of
    every git repository found in it ("" for the root itself).

    Symlinks are recorded as links and never followed; sockets, fifos and
    devices are skipped."""
    root = Path(root)
    manifest: dict[str, dict] = {}
    repos: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        rel_dir = os.path.relpath(dirpath, root)
        rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")
        if ".git" in dirnames or ".git" in filenames:
            repos.append(rel_dir)
        dirnames[:] = sorted(d for d in dirnames if d.lower() != ".git"
                             and not os.path.islink(os.path.join(dirpath, d)))
        # Symlinked directories are recorded as links (os.walk lists them in
        # dirnames when followlinks=False; they were filtered out above).
        names = sorted(set(filenames) | {d for d in os.listdir(dirpath)
                                         if os.path.islink(os.path.join(dirpath, d))})
        for name in names:
            if name.lower() == ".git":
                continue
            full = os.path.join(dirpath, name)
            rel = f"{rel_dir}/{name}" if rel_dir else name
            st = os.lstat(full)
            if stat.S_ISLNK(st.st_mode):
                manifest[rel] = {"kind": "symlink", "target": os.readlink(full)}
            elif stat.S_ISREG(st.st_mode):
                manifest[rel] = {"kind": "file", "sha": file_digest(full),
                                 "mode": _portable_mode(st.st_mode)}
    return manifest, repos


def _portable_mode(mode: int) -> int:
    """What survives a transfer: the executable bit, nothing else — never
    setuid/setgid/sticky, never group/world write (§6.4)."""
    return 0o755 if mode & 0o111 else 0o644


def git_head(repo: str | Path) -> str:
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                            capture_output=True, text=True, timeout=GIT_TIMEOUT_SECONDS)
    return result.stdout.strip() if result.returncode == 0 else ""


def _git(args: list[str], cwd: str | Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          timeout=GIT_TIMEOUT_SECONDS)


def shallow_git_copy(repo: str | Path, depth: int, dest: str | Path) -> Path | None:
    """A disposable `.git` for the sandbox (§6.3): a shallow clone of the
    worker repo's current branch, depth `depth`, with an index matching HEAD
    so `git status` in the run reports the working tree's real changes.

    Built by the worker's own git from its own repository — the worker never
    runs git on anything the sandbox wrote (invariant 4). Returns the `.git`
    directory, or None when the repo has no commit yet."""
    repo = Path(repo)
    head = git_head(repo)
    if not head:
        return None
    branch = _git(["symbolic-ref", "-q", "--short", "HEAD"], repo).stdout.strip()
    target = Path(dest) / "copy"
    args = ["clone", "--quiet", "--no-checkout", "--depth", str(max(1, depth))]
    if branch:
        args += ["--branch", branch, "--single-branch"]
    result = _git([*args, f"file://{repo.resolve()}", str(target)], dest)
    if result.returncode != 0:
        log.warning("shallow git copy failed", extra={"stderr": result.stderr[-500:]})
        return None
    if not branch:
        # Detached HEAD: pin the copy to the worker's exact commit.
        _git(["fetch", "--quiet", "--depth", str(max(1, depth)), "origin", head], target)
        _git(["update-ref", "--no-deref", "HEAD", head], target)
    _git(["read-tree", "HEAD"], target)
    # The copy's origin points at the worker's path; name the real remote
    # instead (credential-free — tokens reach git only through GIT_ASKPASS).
    origin = _git(["remote", "get-url", "origin"], repo).stdout.strip()
    _git(["remote", "set-url", "origin", _strip_userinfo(origin) or "sandbox:offline"],
         target)
    exclude = repo / ".git" / "info" / "exclude"
    if exclude.is_file():
        (target / ".git" / "info").mkdir(parents=True, exist_ok=True)
        (target / ".git" / "info" / "exclude").write_bytes(exclude.read_bytes())
    return target / ".git"


def _strip_userinfo(url: str) -> str:
    if "://" in url and "@" in url.split("://", 1)[1].split("/", 1)[0]:
        scheme, rest = url.split("://", 1)
        host_part, _, path = rest.partition("/")
        return f"{scheme}://{host_part.rsplit('@', 1)[1]}/{path}"
    return url


def build_upload(root: str | Path, manifest: dict[str, dict], paths: Iterable[str],
                 git_repos: Iterable[str], depth: int) -> bytes:
    """A tar of the requested tree paths (under `tree/`) plus one shallow git
    copy per requested repository (under `git/<n>/`, indexed by
    `git-index.json`). The worker builds it from its own clone; the service
    extracts it with the same link-safe rules it applies to everything."""
    root = Path(root)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar, \
            tempfile.TemporaryDirectory(prefix="sprintbaton-git-") as tmp:
        for rel in sorted(paths):
            entry = manifest.get(rel)
            if entry is None:
                continue
            info = tarfile.TarInfo(f"tree/{rel}")
            if entry["kind"] == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = entry["target"]
                tar.addfile(info)
            else:
                data = (root / rel).read_bytes()
                info.size = len(data)
                info.mode = entry["mode"]
                tar.addfile(info, io.BytesIO(data))
        index: dict[str, str] = {}
        for n, repo_rel in enumerate(sorted(git_repos)):
            work = Path(tmp) / str(n)
            work.mkdir()
            git_dir = shallow_git_copy(root / repo_rel if repo_rel else root, depth, work)
            if git_dir is None:
                continue
            index[str(n)] = repo_rel
            tar.add(str(git_dir), arcname=f"git/{n}", recursive=True)
        payload = json.dumps(index).encode()
        info = tarfile.TarInfo("git-index.json")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


# ----------------------------------------------------------------- change set


def scan_for_secrets(changes: ChangeSet, secrets: Iterable[str]) -> None:
    """Refuse a change set carrying the exact value of any credential injected
    into the run (§8.3 step 4) — otherwise a model that wrote the token into a
    file would have it committed and pushed into the owner's repository.

    Exact match on every byte: file content (binary included), symlink
    targets, and paths. The error names the file, never the secret."""
    needles = [s for s in secrets if s]
    if not needles:
        return
    encoded = [(s, s.encode()) for s in needles]
    for change in changes.changes:
        for text, raw in encoded:
            if text in change.path:
                # Never echo a path that itself carries the secret.
                raise ChangeSetRejected(
                    "change set refused: a changed path contains a credential "
                    "that was injected into the run")
            if raw in change.data or text in change.target:
                raise ChangeSetRejected(
                    f"change set refused: {change.path!r} contains a credential "
                    f"that was injected into the run")


def apply_changeset(root: str | Path, changes: ChangeSet, *, max_bytes: int,
                    allowed: Iterable[str] | None = None) -> int:
    """Apply a ChangeSet to the worker's clone at `root` (§6.4). Returns the
    number of entries applied.

    Every entry is validated before anything is written, so a rejected set
    leaves the clone untouched:

    - size is bounded (`max_bytes`, the whole set);
    - every path is canonical, relative, and has no `.git` component
      (`base.normalize_rel`); unless `allowed` names explicit subtrees, nothing
      under `.sprintbaton/` (the worker-owned task mirror) is accepted;
    - only regular files, symlinks and deletions exist — modes keep only the
      executable bit, so setuid/setgid never survive.

    Writing never follows a link: every directory component is opened with
    O_NOFOLLOW relative to its parent's descriptor, so a symlink anywhere on
    the way — pre-existing or created by an earlier entry — rejects the entry
    instead of redirecting the write outside the clone. A symlink *entry* is
    written as a link, never through.
    """
    if changes.size > max_bytes:
        raise ChangeSetRejected(
            f"change set refused: {changes.size} bytes exceeds the "
            f"{max_bytes}-byte limit (SPRINTBATON_SANDBOX_MAX_CHANGESET_BYTES)")
    allowed_prefixes = [normalize_rel(a) for a in allowed] if allowed else None
    validated: list[tuple[list[str], FileChange]] = []
    for change in changes.changes:
        try:
            rel = normalize_rel(change.path)
        except ValueError as e:
            raise ChangeSetRejected(f"change set refused: {e}") from None
        if allowed_prefixes is not None:
            if not any(rel == p or rel.startswith(p + "/") for p in allowed_prefixes):
                raise ChangeSetRejected(
                    f"change set refused: {rel!r} is outside the writable paths")
        elif rel.split("/", 1)[0] == ".sprintbaton":
            raise ChangeSetRejected(
                f"change set refused: {rel!r} is under the worker-owned .sprintbaton/")
        if change.kind == "symlink" and ("\x00" in change.target or not change.target):
            raise ChangeSetRejected(f"change set refused: bad link target for {rel!r}")
        validated.append((rel.split("/"), change))

    root_fd = os.open(str(root), os.O_RDONLY | os.O_DIRECTORY)
    try:
        for parts, change in validated:
            try:
                _apply_one(root_fd, parts, change)
            except OSError as e:
                raise ChangeSetRejected(
                    f"change set refused at {'/'.join(parts)!r}: {e.strerror or e}"
                ) from None
    finally:
        os.close(root_fd)
    return len(validated)


def _open_parent(root_fd: int, parts: list[str], create: bool) -> int | None:
    """Walk to the directory holding `parts[-1]` without following links.
    Returns a directory fd (caller closes), or None when a component is
    missing and `create` is False."""
    fd = os.dup(root_fd)
    try:
        for name in parts[:-1]:
            try:
                nxt = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                              dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    os.close(fd)
                    return None
                os.mkdir(name, 0o755, dir_fd=fd)
                nxt = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                              dir_fd=fd)
            except OSError as e:
                if e.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise OSError(e.errno, "a path component is a symlink or not a directory") from None
                raise
            os.close(fd)
            fd = nxt
        return fd
    except BaseException:
        os.close(fd)
        raise


def _remove_entry(dir_fd: int, name: str) -> None:
    """Remove whatever is at `name` (file, link or directory) without following."""
    try:
        st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(st.st_mode):
        sub = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
        try:
            for child in os.listdir(sub):
                _remove_entry(sub, child)
        finally:
            os.close(sub)
        os.rmdir(name, dir_fd=dir_fd)
    else:
        os.unlink(name, dir_fd=dir_fd)


def _apply_one(root_fd: int, parts: list[str], change: FileChange) -> None:
    name = parts[-1]
    parent = _open_parent(root_fd, parts, create=change.kind != "delete")
    if parent is None:
        return  # deleting something whose directory is already gone
    try:
        if change.kind == "delete":
            try:
                st = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                return
            if not stat.S_ISDIR(st.st_mode):
                os.unlink(name, dir_fd=parent)
            return
        if change.kind == "symlink":
            _remove_entry(parent, name)
            os.symlink(change.target, name, dir_fd=parent)
            return
        # A regular file. Replace a link or directory rather than write through it.
        try:
            st = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISREG(st.st_mode):
                _remove_entry(parent, name)
        except FileNotFoundError:
            pass
        mode = _portable_mode(change.mode)
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
                     mode, dir_fd=parent)
        try:
            view = memoryview(change.data)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fchmod(fd, mode)
        finally:
            os.close(fd)
    finally:
        os.close(parent)

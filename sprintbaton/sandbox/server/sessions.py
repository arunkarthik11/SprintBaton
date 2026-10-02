"""Sessions, trees and confined file access (hosted-sandbox-isolation spec §6).

A session is one task. On disk:

    <root>/sessions/<sid>/meta.json         owner, task, trees (mount/mode/writable)
    <root>/sessions/<sid>/trees/<name>/     a tree's working copy (+ its .git copies)
    <root>/sessions/<sid>/treemeta/<name>.json   baseline manifest + git heads
    <root>/sessions/<sid>/scratch/          per-run scratch dirs   (SCRATCH_ROOT)
    <root>/sessions/<sid>/state/            per-session state      (STATE_ROOT)
    <root>/sessions/<sid>/runs/<run>/       a run's private dir (egress socket)
    <root>/caches/<owner-hash>/             per-tenant dependency cache (/cache)

Every API file operation is expressed in **session paths** and resolved here,
in session space: a symlink is followed only by re-resolving its target as a
session path, so a link pointing anywhere outside the session's own areas —
`/proc/self/environ`, another session, the service's storage root — is
refused rather than followed on the service's filesystem. The resolved path is
then opened component by component with O_NOFOLLOW, so a link swapped in after
the check fails instead of redirecting the open.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import posixpath
import shutil
import stat
import tarfile
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

from sprintbaton.sandbox.base import (
    CACHE_MOUNT,
    COLLECT_EXCLUDED_BY_DEFAULT,
    SCRATCH_ROOT,
    STATE_ROOT,
    ChangeSet,
    DirEntry,
    FileChange,
    SandboxError,
    normalize_rel,
    within,
)

MAX_LINK_HOPS = 40
MAX_READ_BYTES = 64 * 1024 * 1024
_HASH_CHUNK = 1024 * 1024

# Session paths a tree may never be mounted at or under: the run's own system
# directories and the service-provided areas (§10.2).
RESERVED_MOUNTS = ("/proc", "/dev", "/sys", "/usr", "/bin", "/sbin", "/lib",
                   "/lib32", "/lib64", "/etc", "/opt", "/run", "/sprintbaton",
                   CACHE_MOUNT, "/home/sandbox")


class NotInSession(SandboxError):
    """A path resolves outside every area of the session."""


class ReadOnlyPath(SandboxError):
    """A write targeted a path the session does not let the caller write."""


class ChangeSetTooLarge(SandboxError):
    pass


def session_id_for(owner_id: str, task_id: str) -> str:
    """Deterministic per (owner, task): any worker — including one restarted
    since — re-attaches to the same session without persisting a handle (§6.6)."""
    digest = hashlib.sha256(f"{owner_id}\x00{task_id}".encode()).hexdigest()
    return f"s-{digest[:32]}"


def owner_cache_key(owner_id: str) -> str:
    return hashlib.sha256(owner_id.encode()).hexdigest()[:32]


def _portable_mode(mode: int) -> int:
    return 0o755 if mode & 0o111 else 0o644


def _digest_fd(fd: int) -> str:
    h = hashlib.sha256()
    while True:
        chunk = os.read(fd, _HASH_CHUNK)
        if not chunk:
            return h.hexdigest()
        h.update(chunk)


# ----------------------------------------------------- no-follow primitives


def open_nofollow(root: Path, parts: list[str], flags: int, mode: int = 0o644,
                  create_dirs: bool = False) -> int:
    """Open root/parts[...] without following a link at any component."""
    fd = os.open(str(root), os.O_RDONLY | os.O_DIRECTORY)
    try:
        for name in parts[:-1]:
            try:
                nxt = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                if not create_dirs:
                    raise
                os.mkdir(name, 0o755, dir_fd=fd)
                nxt = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
        if not parts:
            return os.dup(fd)
        return os.open(parts[-1], flags | os.O_NOFOLLOW, mode, dir_fd=fd)
    finally:
        os.close(fd)


def remove_nofollow(root: Path, parts: list[str]) -> None:
    """Remove root/parts (file, link or directory tree) without following."""
    try:
        parent = open_nofollow(root, parts[:-1] + ["."], os.O_RDONLY | os.O_DIRECTORY) \
            if len(parts) > 1 else os.open(str(root), os.O_RDONLY | os.O_DIRECTORY)
    except (FileNotFoundError, NotADirectoryError, OSError):
        return
    try:
        _remove_at(parent, parts[-1])
    finally:
        os.close(parent)


def _remove_at(dir_fd: int, name: str) -> None:
    try:
        st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(st.st_mode):
        sub = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
        try:
            for child in os.listdir(sub):
                _remove_at(sub, child)
        finally:
            os.close(sub)
        os.rmdir(name, dir_fd=dir_fd)
    else:
        os.unlink(name, dir_fd=dir_fd)


def write_nofollow(root: Path, parts: list[str], data: bytes, mode: int) -> None:
    """Write a regular file, replacing (never writing through) a link or dir."""
    fd = open_nofollow(root, parts[:-1] + ["."], os.O_RDONLY | os.O_DIRECTORY,
                       create_dirs=True) if len(parts) > 1 else \
        os.open(str(root), os.O_RDONLY | os.O_DIRECTORY)
    try:
        try:
            st = os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISREG(st.st_mode):
                _remove_at(fd, parts[-1])
        except FileNotFoundError:
            pass
        out = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
                      | os.O_NONBLOCK, mode, dir_fd=fd)
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(out, view):]
            os.fchmod(out, mode)
        finally:
            os.close(out)
    finally:
        os.close(fd)


def symlink_nofollow(root: Path, parts: list[str], target: str) -> None:
    fd = open_nofollow(root, parts[:-1] + ["."], os.O_RDONLY | os.O_DIRECTORY,
                       create_dirs=True) if len(parts) > 1 else \
        os.open(str(root), os.O_RDONLY | os.O_DIRECTORY)
    try:
        _remove_at(fd, parts[-1])
        os.symlink(target, parts[-1], dir_fd=fd)
    finally:
        os.close(fd)


# ------------------------------------------------------------------- trees


@dataclass
class Tree:
    name: str
    mount: str
    mode: str                         # "rw" | "ro"
    writable: list[str] = field(default_factory=list)
    # rel -> {"kind", "sha"|"target", "mode"}: what the worker last shipped.
    baseline: dict[str, dict] = field(default_factory=dict)
    # rel -> [size, mtime_ns, sha]: avoids rehashing unchanged files.
    stat_cache: dict[str, list] = field(default_factory=dict)
    git_heads: dict[str, str] = field(default_factory=dict)
    pending: dict | None = None

    def writable_rel(self, rel: str) -> bool:
        if self.mode == "rw":
            return True
        return any(rel == w or rel.startswith(w + "/") for w in self.writable)

    def meta(self) -> dict:
        return {"name": self.name, "mount": self.mount, "mode": self.mode,
                "writable": self.writable}


@dataclass
class Area:
    """One place a session path can land: a tree, scratch, or state."""

    root: Path          # storage directory
    mount: str          # session path it appears at
    tree: Tree | None   # None for scratch/state (always writable)

    def writable(self, rel: str) -> bool:
        return True if self.tree is None else self.tree.writable_rel(rel)


class Session:
    def __init__(self, directory: Path, owner_id: str, task_id: str,
                 cache_root: Path):
        self.id = directory.name
        self.dir = directory
        self.owner_id = owner_id
        self.task_id = task_id
        self.cache_dir = cache_root / owner_cache_key(owner_id)
        self.trees: dict[str, Tree] = {}
        self.last_used = time.time()
        self.lock = threading.RLock()
        for sub in ("trees", "treemeta", "scratch", "state", "runs"):
            (directory / sub).mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    # -------------------------------------------------------------- storage

    @property
    def scratch_dir(self) -> Path:
        return self.dir / "scratch"

    @property
    def state_root(self) -> Path:
        return self.dir / "state"

    def tree_dir(self, name: str) -> Path:
        return self.dir / "trees" / name

    def save(self) -> None:
        meta = {"owner_id": self.owner_id, "task_id": self.task_id,
                "last_used": self.last_used,
                "trees": [t.meta() for t in self.trees.values()]}
        _atomic_json(self.dir / "meta.json", meta)
        for tree in self.trees.values():
            _atomic_json(self.dir / "treemeta" / f"{tree.name}.json", {
                "baseline": tree.baseline, "stat_cache": tree.stat_cache,
                "git_heads": tree.git_heads})

    @classmethod
    def load(cls, directory: Path, cache_root: Path) -> Session | None:
        try:
            meta = json.loads((directory / "meta.json").read_text())
        except (OSError, ValueError):
            return None
        session = cls(directory, meta["owner_id"], meta["task_id"], cache_root)
        session.last_used = float(meta.get("last_used", time.time()))
        for tm in meta.get("trees", []):
            tree = Tree(name=tm["name"], mount=tm["mount"], mode=tm["mode"],
                        writable=list(tm.get("writable", [])))
            try:
                extra = json.loads((directory / "treemeta" / f"{tree.name}.json").read_text())
                tree.baseline = extra.get("baseline", {})
                tree.stat_cache = extra.get("stat_cache", {})
                tree.git_heads = extra.get("git_heads", {})
            except (OSError, ValueError):
                pass
            session.trees[tree.name] = tree
        return session

    def touch(self) -> None:
        self.last_used = time.time()

    # ------------------------------------------------------------ resolution

    def areas(self) -> list[Area]:
        areas = [Area(self.tree_dir(t.name), t.mount, t) for t in self.trees.values()]
        areas.append(Area(self.scratch_dir, SCRATCH_ROOT, None))
        areas.append(Area(self.state_root, STATE_ROOT, None))
        # Longest mount first, so a nested tree wins over its parent.
        return sorted(areas, key=lambda a: len(a.mount), reverse=True)

    def _area_of(self, path: str) -> tuple[Area, str]:
        for area in self.areas():
            if within(path, area.mount):
                rel = posixpath.relpath(path, area.mount)
                return area, "" if rel == "." else rel
        raise NotInSession(f"not a path in this session: {path}")

    def resolve(self, path: str, *, follow_final: bool = True) -> tuple[Area, list[str]]:
        """Resolve a session path to (area, symlink-free relative parts)."""
        if not isinstance(path, str) or not path.startswith("/") or "\x00" in path:
            raise NotInSession(f"not an absolute session path: {path!r}")
        path = posixpath.normpath(path)
        for _ in range(MAX_LINK_HOPS):
            area, rel = self._area_of(path)
            parts = rel.split("/") if rel else []
            cur = area.root
            redirected = None
            for i, part in enumerate(parts):
                candidate = cur / part
                try:
                    st = os.lstat(candidate)
                except FileNotFoundError:
                    return area, parts
                last = i == len(parts) - 1
                if stat.S_ISLNK(st.st_mode) and (follow_final or not last):
                    target = os.readlink(candidate)
                    base = posixpath.join(area.mount, *parts[:i]) if i else area.mount
                    new = target if target.startswith("/") else posixpath.join(base, target)
                    redirected = posixpath.normpath(posixpath.join(new, *parts[i + 1:]))
                    break
                cur = candidate
            if redirected is None:
                return area, parts
            path = redirected
        raise NotInSession(f"too many levels of symbolic links: {path}")

    # ---------------------------------------------------------------- files

    def read_file(self, path: str) -> bytes:
        area, parts = self.resolve(path)
        try:
            fd = open_nofollow(area.root, parts, os.O_RDONLY | os.O_NONBLOCK)
        except OSError as e:
            raise SandboxError(f"cannot read {path}: {e.strerror}") from None
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise SandboxError(f"not a file: {path}")
            chunks, total = [], 0
            while True:
                chunk = os.read(fd, _HASH_CHUNK)
                if not chunk:
                    return b"".join(chunks)
                total += len(chunk)
                if total > MAX_READ_BYTES:
                    raise SandboxError(f"file too large to read: {path}")
                chunks.append(chunk)
        finally:
            os.close(fd)

    def write_file(self, path: str, data: bytes) -> None:
        area, parts = self.resolve(path)
        rel = "/".join(parts)
        if not parts or not area.writable(rel):
            raise ReadOnlyPath(f"not writable in this session: {path}")
        try:
            write_nofollow(area.root, parts, data, 0o644)
        except OSError as e:
            raise SandboxError(f"cannot write {path}: {e.strerror}") from None

    def list_dir(self, path: str) -> list[DirEntry]:
        area, parts = self.resolve(path)
        try:
            fd = open_nofollow(area.root, parts, os.O_RDONLY | os.O_DIRECTORY) if parts \
                else os.open(str(area.root), os.O_RDONLY | os.O_DIRECTORY)
        except OSError as e:
            raise SandboxError(f"cannot list {path}: {e.strerror}") from None
        try:
            out = []
            for name in sorted(os.listdir(fd)):
                st = os.stat(name, dir_fd=fd, follow_symlinks=False)
                kind = ("symlink" if stat.S_ISLNK(st.st_mode) else
                        "dir" if stat.S_ISDIR(st.st_mode) else
                        "file" if stat.S_ISREG(st.st_mode) else "other")
                out.append(DirEntry(name=name, kind=kind,
                                    size=st.st_size if kind == "file" else 0))
            return out
        finally:
            os.close(fd)

    def make_scratch(self, prefix: str) -> str:
        safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in prefix)[:48]
        name = f"sprintbaton-out-{safe}-{os.urandom(6).hex()}"
        (self.scratch_dir / name).mkdir(mode=0o700)
        return posixpath.join(SCRATCH_ROOT, name)

    def discard_scratch(self, path: str) -> None:
        if not within(path, SCRATCH_ROOT) or posixpath.normpath(path) == SCRATCH_ROOT:
            raise NotInSession(f"not a scratch path: {path}")
        rel = posixpath.relpath(posixpath.normpath(path), SCRATCH_ROOT)
        remove_nofollow(self.scratch_dir, [normalize_rel(rel)])

    def state_dir(self, name: str) -> str:
        rel = normalize_rel(name)
        (self.state_root / rel).mkdir(parents=True, exist_ok=True)
        return posixpath.join(STATE_ROOT, rel)

    # ----------------------------------------------------------------- trees

    def plan_tree(self, name: str, mount: str, mode: str, writable: list[str],
                  manifest: dict[str, dict], git_heads: dict[str, str]
                  ) -> tuple[list[str], list[str]]:
        """Stage an incremental refresh of a tree (§6.3): returns the paths and
        git repositories the worker must upload. A tree replaces any tree it
        overlaps — the worker's clone is authoritative (invariant 7), so a
        dropped tree loses nothing."""
        mount = posixpath.normpath(mount)
        if not mount.startswith("/") or mount == "/":
            raise SandboxError(f"invalid mount: {mount!r}")
        for reserved in RESERVED_MOUNTS:
            if within(mount, reserved) or within(reserved, mount):
                raise SandboxError(f"mount {mount!r} overlaps reserved {reserved!r}")
        if mode not in ("rw", "ro"):
            raise SandboxError(f"invalid tree mode: {mode!r}")
        writable = [normalize_rel(w) for w in writable]
        for rel in manifest:
            normalize_rel(rel)
        with self.lock:
            for other in list(self.trees.values()):
                if other.name == name and other.mount == mount:
                    continue
                if other.name == name or within(mount, other.mount) or within(other.mount, mount):
                    self.drop_tree(other.name)
            tree = self.trees.get(name)
            if tree is None:
                tree = Tree(name=name, mount=mount, mode=mode)
                self.trees[name] = tree
                self.tree_dir(name).mkdir(parents=True, exist_ok=True)
            tree.mode, tree.writable = mode, writable
            current = self._scan(tree, list(manifest.keys()))
            need = [rel for rel, entry in manifest.items()
                    if not _same_entry(current.get(rel), entry)]
            # Delete only what the worker deleted since the last baseline —
            # never files the sandbox created (e.g. an installed node_modules
            # that .gitignore keeps out of every collect).
            for rel in tree.baseline:
                if rel not in manifest:
                    remove_nofollow(self.tree_dir(name), rel.split("/"))
                    tree.stat_cache.pop(rel, None)
            need_git = [repo for repo, head in git_heads.items()
                        if tree.git_heads.get(repo) != head]
            tree.pending = {"manifest": manifest, "git_heads": git_heads}
            self.save()
            return need, need_git

    def upload_tree(self, name: str, stream: BinaryIO, max_bytes: int) -> None:
        """Extract the worker's upload and commit the staged baseline."""
        with self.lock:
            tree = self.trees.get(name)
            if tree is None or tree.pending is None:
                raise SandboxError(f"no staged plan for tree {name!r}")
            root = self.tree_dir(name)
            staging = self.dir / "trees" / f".{name}.git-staging"
            shutil.rmtree(staging, ignore_errors=True)
            staging.mkdir()
            git_index: dict[str, str] = {}
            total = 0
            try:
                with tarfile.open(fileobj=stream, mode="r|") as tar:
                    for member in tar:
                        total += member.size
                        if total > max_bytes:
                            raise SandboxError("upload too large")
                        if member.name == "git-index.json":
                            git_index = json.loads(tar.extractfile(member).read())
                        elif member.name.startswith("tree/"):
                            self._extract_tree_member(tar, member, root)
                        elif member.name.startswith("git/"):
                            _extract_git_member(tar, member, staging)
                for n, repo_rel in git_index.items():
                    parts = [normalize_rel(repo_rel)] if repo_rel else []
                    repo_parts = parts[0].split("/") if parts else []
                    dest_parts = repo_parts + [".git"]
                    remove_nofollow(root, dest_parts)
                    if repo_parts:
                        fd = open_nofollow(root, repo_parts + ["."],
                                           os.O_RDONLY | os.O_DIRECTORY, create_dirs=True)
                        os.close(fd)
                    src = staging / str(n)
                    if src.is_dir():
                        os.rename(src, root.joinpath(*dest_parts))
            finally:
                shutil.rmtree(staging, ignore_errors=True)
            manifest = tree.pending["manifest"]
            tree.git_heads = {repo: head for repo, head in tree.pending["git_heads"].items()
                              if repo in git_index.values() or tree.git_heads.get(repo) == head}
            tree.baseline = manifest
            tree.stat_cache = {}
            self._scan(tree, list(manifest.keys()))
            tree.pending = None
            self.save()

    def _extract_tree_member(self, tar: tarfile.TarFile, member: tarfile.TarInfo,
                             root: Path) -> None:
        parts = normalize_rel(member.name[len("tree/"):]).split("/")
        if member.issym():
            symlink_nofollow(root, parts, member.linkname)
        elif member.isreg():
            data = tar.extractfile(member).read()
            write_nofollow(root, parts, data, _portable_mode(member.mode))
        # directories are implied; devices, fifos and hard links are dropped

    def drop_tree(self, name: str) -> None:
        with self.lock:
            self.trees.pop(name, None)
            shutil.rmtree(self.tree_dir(name), ignore_errors=True)
            try:
                (self.dir / "treemeta" / f"{name}.json").unlink()
            except FileNotFoundError:
                pass
            self.save()

    # --------------------------------------------------------------- collect

    def collect(self, name: str, paths: list[str] | None, max_bytes: int,
                list_candidates: Callable[[Tree], list[str] | None]) -> ChangeSet:
        """What changed in a tree since its baseline (§6.4).

        With `paths`, exactly those subtrees are compared (the init pass's
        `.sprintbaton/`). Without, the whole tree minus `.git` and
        `.sprintbaton`; new files are discovered through the sandbox-side
        git (`list_candidates`, run *inside a sandbox run*, so .gitignore'd
        output such as node_modules never travels), falling back to a full
        walk when the tree is not a repository."""
        with self.lock:
            tree = self.trees.get(name)
            if tree is None:
                raise SandboxError(f"unknown tree {name!r}")
            root = self.tree_dir(name)
            if paths:
                prefixes = [normalize_rel(p) for p in paths]
                baseline = {r: e for r, e in tree.baseline.items()
                            if any(r == p or r.startswith(p + "/") for p in prefixes)}
                candidates = set(baseline)
                for prefix in prefixes:
                    candidates.update(_walk(root, prefix))
            else:
                baseline = {r: e for r, e in tree.baseline.items() if _collectable(r)}
                listed = list_candidates(tree)
                if listed is None:
                    listed = list(_walk(root, ""))
                candidates = set(baseline) | {r for r in listed if _collectable(r)}
            current = self._scan(tree, sorted(candidates))
            changes: list[FileChange] = []
            total = 0
            for rel in sorted(candidates):
                now, then = current.get(rel), baseline.get(rel)
                if _same_entry(now, then):
                    continue
                if now is None:
                    if then is not None:
                        changes.append(FileChange(path=rel, kind="delete"))
                    continue
                if now["kind"] == "symlink":
                    change = FileChange(path=rel, kind="symlink", target=now["target"])
                else:
                    fd = open_nofollow(root, rel.split("/"), os.O_RDONLY | os.O_NONBLOCK)
                    try:
                        data = _read_all(fd, max_bytes - total)
                    finally:
                        os.close(fd)
                    change = FileChange(path=rel, kind="file", data=data, mode=now["mode"])
                total += len(change.data) + len(change.target) + len(rel)
                if total > max_bytes:
                    raise ChangeSetTooLarge(
                        f"change set exceeds {max_bytes} bytes")
                changes.append(change)
            self.save()
            return ChangeSet(tree=name, changes=changes)

    def _scan(self, tree: Tree, rels: Iterable[str]) -> dict[str, dict]:
        """Current entries for `rels` in the tree's storage (None = absent),
        hashing only files whose (size, mtime) changed since last seen."""
        root = self.tree_dir(tree.name)
        out: dict[str, dict] = {}
        for rel in rels:
            try:
                parts = normalize_rel(rel).split("/")
            except ValueError:
                continue
            path = root.joinpath(*parts)
            try:
                st = os.lstat(path)
            except (FileNotFoundError, NotADirectoryError):
                tree.stat_cache.pop(rel, None)
                continue
            if stat.S_ISLNK(st.st_mode):
                out[rel] = {"kind": "symlink", "target": os.readlink(path)}
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            cached = tree.stat_cache.get(rel)
            if cached and cached[0] == st.st_size and cached[1] == st.st_mtime_ns:
                sha = cached[2]
            else:
                try:
                    fd = open_nofollow(root, parts, os.O_RDONLY | os.O_NONBLOCK)
                except OSError:
                    continue
                try:
                    sha = _digest_fd(fd)
                finally:
                    os.close(fd)
                tree.stat_cache[rel] = [st.st_size, st.st_mtime_ns, sha]
            out[rel] = {"kind": "file", "sha": sha, "mode": _portable_mode(st.st_mode)}
        return out


def _same_entry(a: dict | None, b: dict | None) -> bool:
    if a is None or b is None:
        return a is b
    if a["kind"] != b["kind"]:
        return False
    if a["kind"] == "symlink":
        return a.get("target") == b.get("target")
    return a.get("sha") == b.get("sha") and a.get("mode") == b.get("mode")


def _collectable(rel: str) -> bool:
    parts = rel.split("/")
    return (parts[0] not in COLLECT_EXCLUDED_BY_DEFAULT
            and not any(p.lower() == ".git" for p in parts))


def _walk(root: Path, prefix: str) -> Iterable[str]:
    """Every file/link under root/prefix, relative to root; never follows."""
    base = root.joinpath(*prefix.split("/")) if prefix else root
    try:
        st = os.lstat(base)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(st.st_mode):
        if prefix:
            yield prefix
        return
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        rel_dir = os.path.relpath(dirpath, root)
        rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")
        links = [d for d in dirnames if os.path.islink(os.path.join(dirpath, d))]
        dirnames[:] = [d for d in dirnames if d.lower() != ".git" and d not in links]
        for name in [*filenames, *links]:
            if name.lower() == ".git":
                continue
            yield f"{rel_dir}/{name}" if rel_dir else name


def _read_all(fd: int, budget: int) -> bytes:
    chunks, total = [], 0
    while True:
        chunk = os.read(fd, _HASH_CHUNK)
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > budget:
            raise ChangeSetTooLarge("change set exceeds the size limit")
        chunks.append(chunk)


def _extract_git_member(tar: tarfile.TarFile, member: tarfile.TarInfo,
                        staging: Path) -> None:
    """A shallow `.git` copy the worker built: directories and regular files
    only, every component validated."""
    rest = member.name[len("git/"):]
    parts = rest.split("/")
    if (not parts or any(p in ("", ".", "..") or "\x00" in p for p in parts)
            or rest.startswith("/")):
        raise SandboxError(f"invalid git member: {member.name!r}")
    if member.isdir():
        fd = open_nofollow(staging, parts + ["."], os.O_RDONLY | os.O_DIRECTORY,
                           create_dirs=True)
        os.close(fd)
    elif member.isreg():
        write_nofollow(staging, parts, tar.extractfile(member).read(),
                       _portable_mode(member.mode))
    else:
        raise SandboxError(f"unexpected git member type: {member.name!r}")


def _atomic_json(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, separators=(",", ":")))
    os.replace(tmp, path)


class SessionStore:
    """All sessions of this pod, persisted under `root` (an emptyDir, so a
    container restart keeps them and a pod restart loses them — §6.6)."""

    def __init__(self, root: Path, max_sessions: int, ttl_seconds: int):
        self.root = root
        self.sessions_dir = root / "sessions"
        self.cache_root = root / "caches"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self.max_sessions = max_sessions
        self.ttl_seconds = ttl_seconds
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()
        for directory in sorted(self.sessions_dir.iterdir()):
            session = Session.load(directory, self.cache_root)
            if session is not None:
                self._sessions[session.id] = session

    def open(self, owner_id: str, task_id: str) -> Session:
        sid = session_id_for(owner_id, task_id)
        with self._lock:
            session = self._sessions.get(sid)
            if session is None:
                self._evict_expired_locked()
                if len(self._sessions) >= self.max_sessions:
                    raise SandboxError("sandbox is at its session limit")
                session = Session(self.sessions_dir / sid, owner_id, task_id,
                                  self.cache_root)
                session.save()
                self._sessions[sid] = session
            session.touch()
            return session

    def get(self, sid: str) -> Session | None:
        with self._lock:
            session = self._sessions.get(sid)
        if session is not None:
            session.touch()
        return session

    def close(self, sid: str) -> None:
        with self._lock:
            session = self._sessions.pop(sid, None)
        if session is not None:
            shutil.rmtree(session.dir, ignore_errors=True)

    def evict_expired(self) -> int:
        with self._lock:
            return self._evict_expired_locked()

    def _evict_expired_locked(self) -> int:
        now = time.time()
        stale = [sid for sid, s in self._sessions.items()
                 if now - s.last_used > self.ttl_seconds]
        for sid in stale:
            session = self._sessions.pop(sid)
            shutil.rmtree(session.dir, ignore_errors=True)
        return len(stale)

    def count(self) -> int:
        with self._lock:
            return len(self._sessions)


__all__ = ["Session", "SessionStore", "Tree", "Area", "NotInSession", "ReadOnlyPath",
           "ChangeSetTooLarge", "session_id_for", "errno"]

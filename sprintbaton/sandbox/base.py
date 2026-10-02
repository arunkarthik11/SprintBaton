"""The sandbox seam's protocols and value types (hosted-sandbox-isolation spec §5.1).

Stdlib-only by construction: this is the one module the sandbox service
(`sandbox/server/`, which runs in an image carrying none of the orchestrator)
is allowed to import from the rest of `sprintbaton` (spec §10.1). A test
enforces both halves of that.

Paths. A tree is mounted in the session at a **session path** that is, by
design, the same string as the worker clone it was snapshot from (see
`TreeSnapshot.mount`). So every path a prompt, a location guide or a model sees
is valid in the session without translation — the worker path simply names a
directory the run's mount namespace provides. The worker's own directory is
never visible to a run; only the session's copy is.
"""

from __future__ import annotations

import base64
import json
import posixpath
import struct
from dataclasses import dataclass, field
from typing import BinaryIO, Literal, Protocol

TreeMode = Literal["rw", "ro"]

# The loopback port a run's egress bridge listens on inside its own network
# namespace (spec §8.1). A constant is safe because every run has a private
# namespace; it lets the worker build a run's environment before the run exists.
BRIDGE_PORT = 18080
BRIDGE_URL = f"http://127.0.0.1:{BRIDGE_PORT}"

# Session paths the sandbox service provides besides the trees.
SCRATCH_ROOT = "/sprintbaton/scratch"      # per-run scratch dirs (answer files)
STATE_ROOT = "/sprintbaton/state"          # per-session, survives runs (CLI resume)
CACHE_MOUNT = "/cache"                     # per-tenant dependency cache (§6.1)

# The first line the sandbox service's relay sends to the broker on every
# egress connection, carrying the run token the run itself never sees (§8.1).
RUN_TOKEN_PREAMBLE = b"SPRINTBATON-RUN-TOKEN "

# Top-level directories never collected back from a session: the sandbox-side
# git copy is disposable and untrusted (§6.3), and `.sprintbaton/` is the
# worker-materialized task mirror, excluded exactly as `.git/info/exclude`
# keeps it out of commits (§6.3). An explicit `collect(paths=...)` may still
# name `.sprintbaton` — the init pass does.
NEVER_COLLECT = frozenset({".git"})
COLLECT_EXCLUDED_BY_DEFAULT = frozenset({".git", ".sprintbaton"})


class SandboxError(RuntimeError):
    """A sandbox operation failed (protocol, transport or confinement)."""


class SandboxUnavailableError(SandboxError):
    """Hosted mode cannot reach a usable sandbox. Fail closed (invariant 8):
    never fall back to local execution."""


class ChangeSetRejected(SandboxError):
    """A ChangeSet failed validation and was not applied to the worker's clone
    (spec §6.4 / §8.3 step 4)."""


def normalize_rel(path: str) -> str:
    """Validate and normalize a tree-relative POSIX path.

    The single path rule shared by the service (upload extraction, file I/O,
    collect) and the worker (`transfer.apply_changeset`): relative, no `..`,
    no NUL, nothing under `.git/`. Raises ValueError on anything else — callers
    treat that as hostile input, never as something to repair.
    """
    if not isinstance(path, str) or not path or "\x00" in path:
        raise ValueError(f"invalid path: {path!r}")
    if path.startswith("/") or "\\" in path:
        raise ValueError(f"absolute or non-POSIX path: {path!r}")
    parts = path.split("/")
    if any(p in ("", ".", "..") for p in parts):
        # Rejected rather than normalized: `a/../b` is never produced by an
        # honest walker, so its presence is itself the signal.
        raise ValueError(f"non-canonical path: {path!r}")
    # Any `.git` component, not only the top one: git recurses into an embedded
    # repository's `.git` (its config included) during `status`, so a planted
    # `sub/.git/config` would be executed by the worker's own git.
    if any(p.lower() in NEVER_COLLECT for p in parts):
        raise ValueError(f"path under .git: {path!r}")
    return posixpath.join(*parts)


def within(child: str, parent: str) -> bool:
    """Is session path `child` equal to or under session path `parent`?
    Lexical — both sides are normalized absolute POSIX paths."""
    child = posixpath.normpath(child)
    parent = posixpath.normpath(parent)
    return child == parent or child.startswith(parent.rstrip("/") + "/")


# ------------------------------------------------------------------ values


@dataclass(frozen=True)
class SessionHandle:
    """Identifies one session (one task) and where it lives (§6.6). `address`
    is the sandbox pod holding it — the Service with one replica in v1, so the
    handle is already the shape horizontal scaling needs (§13.3)."""

    session_id: str
    address: str = ""


@dataclass(frozen=True)
class DirEntry:
    name: str
    kind: Literal["file", "dir", "symlink", "other"]
    size: int = 0


@dataclass(frozen=True)
class TreeSnapshot:
    """A worker clone to materialize into a session (§6.3).

    `source` is the worker's directory; `mount` the session path the tree
    appears at (defaults to `source`, see the module docstring). `writable`
    lists tree-relative subpaths a run may write even when the tree is `ro` —
    the metadata init pass's `.sprintbaton/` (§6.2). The service ships the
    working tree without `.git`, plus a shallow copy of each git repository
    found in it (depth `git_depth`), so `git status/diff/log` still work in the
    run against a disposable, sandbox-side `.git`."""

    source: str
    mount: str = ""
    writable: tuple[str, ...] = ()
    git_depth: int = 50

    @property
    def mount_path(self) -> str:
        return posixpath.normpath(self.mount or self.source)


@dataclass(frozen=True)
class EgressGrant:
    """Network access for one run (§8). Without a grant a run has no network
    at all. `token` is the run token the worker's broker minted; the sandbox
    service's relay attaches it, the run never sees it."""

    token: str


@dataclass(frozen=True)
class RunSpec:
    argv: list[str]
    cwd: str                        # a session path
    env: dict[str, str]             # the COMPLETE environment; nothing is inherited
    timeout_seconds: int = 300
    egress: EgressGrant | None = None
    stdin: bytes | None = None      # buffered runs only; spawn() streams instead

    def to_json(self) -> dict:
        return {
            "argv": list(self.argv), "cwd": self.cwd, "env": dict(self.env),
            "timeout_seconds": self.timeout_seconds,
            "egress": {"token": self.egress.token} if self.egress else None,
            "stdin": base64.b64encode(self.stdin).decode() if self.stdin else None,
        }

    @classmethod
    def from_json(cls, data: dict) -> RunSpec:
        egress = data.get("egress")
        stdin = data.get("stdin")
        return cls(
            argv=[str(a) for a in data["argv"]], cwd=str(data["cwd"]),
            env={str(k): str(v) for k, v in (data.get("env") or {}).items()},
            timeout_seconds=int(data.get("timeout_seconds", 300)),
            egress=EgressGrant(token=str(egress["token"])) if egress else None,
            stdin=base64.b64decode(stdin) if stdin else None,
        )


@dataclass(frozen=True)
class RunResult:
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False

    def to_json(self) -> dict:
        return {"exit_code": self.exit_code, "stdout": self.stdout,
                "stderr": self.stderr, "timed_out": self.timed_out}

    @classmethod
    def from_json(cls, data: dict) -> RunResult:
        return cls(exit_code=int(data["exit_code"]), stdout=data.get("stdout", ""),
                   stderr=data.get("stderr", ""),
                   timed_out=bool(data.get("timed_out", False)))


@dataclass(frozen=True)
class FileChange:
    """One entry of a ChangeSet. `kind` "file" carries content and mode,
    "symlink" a link target (never followed), "delete" nothing."""

    path: str
    kind: Literal["file", "symlink", "delete"]
    data: bytes = field(default=b"", repr=False)
    mode: int = 0o644
    target: str = ""

    def to_json(self) -> dict:
        out: dict = {"path": self.path, "kind": self.kind}
        if self.kind == "file":
            out["data"] = base64.b64encode(self.data).decode()
            out["mode"] = self.mode
        elif self.kind == "symlink":
            out["target"] = self.target
        return out

    @classmethod
    def from_json(cls, data: dict) -> FileChange:
        kind = data.get("kind")
        if kind not in ("file", "symlink", "delete"):
            raise ValueError(f"unknown change kind: {kind!r}")
        return cls(
            path=str(data["path"]), kind=kind,
            data=base64.b64decode(data.get("data") or ""),
            mode=int(data.get("mode", 0o644)),
            target=str(data.get("target", "")),
        )


@dataclass
class ChangeSet:
    """What a write-capable run changed in one tree (§5.1, §6.4). Applied to
    the worker's clone by `transfer.apply_changeset` — never extracted blindly."""

    tree: str
    changes: list[FileChange] = field(default_factory=list)

    @property
    def size(self) -> int:
        return sum(len(c.data) + len(c.target) + len(c.path) for c in self.changes)

    def __bool__(self) -> bool:
        return bool(self.changes)

    def to_json(self) -> dict:
        return {"tree": self.tree, "changes": [c.to_json() for c in self.changes]}

    @classmethod
    def from_json(cls, data: dict) -> ChangeSet:
        return cls(tree=str(data.get("tree", "")),
                   changes=[FileChange.from_json(c) for c in data.get("changes", [])])


# ------------------------------------------------------------ spawn framing
# A spawned process's stdio crosses one bidirectional byte stream as frames:
# 1-byte kind, 4-byte big-endian length, payload. Shared by the service and
# the worker client so the two can never disagree about the wire format.

FRAME_SPEC = b"R"       # client -> service: the RunSpec (JSON), first frame only
FRAME_STDIN = b"I"      # client -> service: stdin bytes
FRAME_EOF = b"E"        # client -> service: close stdin
FRAME_KILL = b"K"       # client -> service: kill the run
FRAME_STDOUT = b"O"     # service -> client
FRAME_STDERR = b"e"     # service -> client
FRAME_EXIT = b"X"       # service -> client: exit code (JSON), last frame
MAX_FRAME = 16 * 1024 * 1024


def encode_frame(kind: bytes, payload: bytes = b"") -> bytes:
    return kind + struct.pack(">I", len(payload)) + payload


def read_frame(stream: BinaryIO) -> tuple[bytes, bytes] | None:
    """Read one frame, or None at a clean end of stream."""
    header = _read_exact(stream, 5)
    if header is None:
        return None
    kind, length = header[:1], struct.unpack(">I", header[1:])[0]
    if length > MAX_FRAME:
        raise SandboxError(f"frame too large: {length}")
    payload = _read_exact(stream, length) if length else b""
    if payload is None:
        raise SandboxError("stream ended mid-frame")
    return kind, payload


def _read_exact(stream: BinaryIO, n: int) -> bytes | None:
    buf = bytearray()
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            if not buf:
                return None
            raise SandboxError("stream ended mid-frame")
        buf.extend(chunk)
    return bytes(buf)


def dumps(data: dict) -> bytes:
    return json.dumps(data, separators=(",", ":")).encode()


# --------------------------------------------------------------- protocols


class RunProcess(Protocol):
    """A spawned, streaming run (§5.1 `spawn`) — used for a CLI binary whose
    stdio the caller drives, e.g. `claude` behind `SandboxTransport`."""

    def write_stdin(self, data: bytes) -> None: ...
    def close_stdin(self) -> None: ...
    def read_stdout(self, max_bytes: int = 65536) -> bytes:
        """Blocking; b"" once stdout is exhausted."""
    def stderr_text(self) -> str: ...
    def wait(self, timeout: float | None = None) -> int: ...
    def kill(self) -> None: ...


class SandboxSession(Protocol):
    """One task's sandbox state (§6.1): trees and the sandbox-side git copy
    live as long as the session; processes, mounts and credentials live one run."""

    handle: SessionHandle

    def put_tree(self, name: str, snapshot: TreeSnapshot, mode: TreeMode) -> str:
        """Materialize (or incrementally refresh) a tree; returns its session path."""
    def drop_tree(self, name: str) -> None: ...
    def write_file(self, path: str, data: bytes) -> None: ...
    def read_file(self, path: str) -> bytes: ...
    def list_dir(self, path: str) -> list[DirEntry]: ...
    def run(self, spec: RunSpec) -> RunResult: ...
    def spawn(self, spec: RunSpec) -> RunProcess: ...
    def collect(self, tree: str, paths: list[str] | None = None) -> ChangeSet: ...
    def make_scratch(self, prefix: str = "run") -> str:
        """A fresh, private per-run directory (session path) — the home of a
        read-only run's answer file. Discard with `discard_scratch`."""
    def discard_scratch(self, path: str) -> None: ...
    def state_dir(self, name: str) -> str:
        """A per-session directory that survives across runs (e.g. the
        `claude` binary's config/session files, for resume — §7.2)."""
    def tool_env(self) -> dict[str, str]:
        """The base environment a model-issued command runs with. The complete
        environment is always passed explicitly (§5.1); this is where it starts."""
    def close(self) -> None: ...


class Sandbox(Protocol):
    # False for the tool-mode passthrough (runs share the worker's filesystem
    # and environment, accepted by §11.2); True wherever runs are isolated.
    isolated: bool

    def open_session(self, owner_id: str, task_id: str) -> SandboxSession: ...
    def resume_session(self, handle: SessionHandle) -> SandboxSession | None: ...
    def health(self) -> None:
        """Raise SandboxUnavailableError unless the sandbox can run work."""

"""LocalPassthroughSandbox — tool mode's implementation of the seam
(hosted-sandbox-isolation spec §5.3, §11.1).

A session *is* the existing clone directory: `put_tree` copies nothing and
returns the clone's own path, `collect` is a no-op because every write already
landed in place, and runs are plain subprocesses. So tool mode's behavior is
today's, while the harnesses call one seam in both modes.

The environment is still passed explicitly (`RunSpec.env`, never inherited by
the subprocess call), but `tool_env()` starts it from this process's own
environment — the tool-mode user's own shell, which §11.2 accepts as residual
risk to the user themselves, not a cross-tenant exposure.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

from sprintbaton.sandbox.base import (
    ChangeSet,
    DirEntry,
    RunProcess,
    RunResult,
    RunSpec,
    SessionHandle,
    TreeMode,
    TreeSnapshot,
)


class LocalRunProcess:
    """RunProcess over a local Popen."""

    def __init__(self, proc: subprocess.Popen):
        self._proc = proc
        self._stderr: list[bytes] = []
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_thread.start()

    def _drain_stderr(self) -> None:
        assert self._proc.stderr is not None
        for chunk in iter(lambda: self._proc.stderr.read(65536), b""):
            self._stderr.append(chunk)

    def write_stdin(self, data: bytes) -> None:
        assert self._proc.stdin is not None
        self._proc.stdin.write(data)
        self._proc.stdin.flush()

    def close_stdin(self) -> None:
        if self._proc.stdin and not self._proc.stdin.closed:
            self._proc.stdin.close()

    def read_stdout(self, max_bytes: int = 65536) -> bytes:
        assert self._proc.stdout is not None
        return self._proc.stdout.read1(max_bytes)

    def stderr_text(self) -> str:
        return b"".join(self._stderr).decode(errors="replace")

    def wait(self, timeout: float | None = None) -> int:
        code = self._proc.wait(timeout)
        self._stderr_thread.join(timeout=1)
        return code

    def kill(self) -> None:
        if self._proc.poll() is None:
            self._proc.kill()


class LocalSession:
    def __init__(self, owner_id: str, task_id: str):
        self.handle = SessionHandle(session_id=f"local:{owner_id}:{task_id}")
        self._task_id = task_id

    def put_tree(self, name: str, snapshot: TreeSnapshot, mode: TreeMode) -> str:
        # No copy: the clone IS the tree. The mount must therefore be the
        # source — a distinct mount path is only meaningful remotely.
        if snapshot.mount and os.path.normpath(snapshot.mount) != os.path.normpath(snapshot.source):
            raise ValueError("the local sandbox mounts a tree only at its own path")
        return snapshot.source

    def drop_tree(self, name: str) -> None:
        return None

    def write_file(self, path: str, data: bytes) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def read_file(self, path: str) -> bytes:
        return Path(path).read_bytes()

    def list_dir(self, path: str) -> list[DirEntry]:
        entries = []
        for child in sorted(Path(path).iterdir()):
            if child.is_symlink():
                kind = "symlink"
            elif child.is_dir():
                kind = "dir"
            elif child.is_file():
                kind = "file"
            else:
                kind = "other"
            size = child.lstat().st_size if kind == "file" else 0
            entries.append(DirEntry(name=child.name, kind=kind, size=size))
        return entries

    def run(self, spec: RunSpec) -> RunResult:
        try:
            result = subprocess.run(
                spec.argv, cwd=spec.cwd, env=spec.env, input=spec.stdin,
                capture_output=True, timeout=spec.timeout_seconds,
            )
        except subprocess.TimeoutExpired as e:
            return RunResult(exit_code=124, timed_out=True,
                             stdout=_text(e.stdout), stderr=_text(e.stderr))
        return RunResult(exit_code=result.returncode, stdout=_text(result.stdout),
                         stderr=_text(result.stderr))

    def spawn(self, spec: RunSpec) -> RunProcess:
        proc = subprocess.Popen(
            spec.argv, cwd=spec.cwd, env=spec.env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        return LocalRunProcess(proc)

    def collect(self, tree: str, paths: list[str] | None = None) -> ChangeSet:
        # Writes already landed in the clone (§5.3).
        return ChangeSet(tree=tree)

    def make_scratch(self, prefix: str = "run") -> str:
        return str(Path(tempfile.mkdtemp(prefix=f"sprintbaton-out-{prefix}-")).resolve())

    def discard_scratch(self, path: str) -> None:
        shutil.rmtree(path, ignore_errors=True)

    def state_dir(self, name: str) -> str:
        path = Path(tempfile.gettempdir()) / "sprintbaton-state" / self._task_id / name
        path.mkdir(parents=True, exist_ok=True)
        return str(path)

    def tool_env(self) -> dict[str, str]:
        return dict(os.environ)

    def close(self) -> None:
        return None


def _text(data: bytes | str | None) -> str:
    if data is None:
        return ""
    return data if isinstance(data, str) else data.decode(errors="replace")


class LocalPassthroughSandbox:
    isolated = False

    def open_session(self, owner_id: str, task_id: str) -> LocalSession:
        return LocalSession(owner_id, task_id)

    def resume_session(self, handle: SessionHandle) -> LocalSession | None:
        _, owner_id, task_id = (handle.session_id.split(":", 2) + ["", ""])[:3]
        return LocalSession(owner_id, task_id)

    def health(self) -> None:
        return None


LOCAL_SANDBOX = LocalPassthroughSandbox()

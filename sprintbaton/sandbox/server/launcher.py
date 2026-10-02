"""Run construction (hosted-sandbox-isolation spec §8.1, §10.2).

`BwrapLauncher` is the real one: every run is a single `bwrap` invocation with
user/pid/net/ipc/uts namespaces, `--clearenv` plus exactly `RunSpec.env`, the
toolchain bound read-only, the session's trees at their session paths with
their rw/ro mode, fresh `/proc` and `/tmp`, and the seccomp profile. A run with
an egress grant gets its private Unix socket bound in and starts under
`runinit.py`, which opens the loopback bridge.

`DirectLauncher` provides **no isolation** and exists for the protocol tests
and local development only: it runs commands as plain subprocesses, rewriting
session paths to their storage directories. The service refuses to start with
it unless explicitly configured (`SPRINTBATON_SANDBOX_LAUNCHER=direct`).
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from sprintbaton.sandbox.base import (
    BRIDGE_PORT,
    BRIDGE_URL,
    CACHE_MOUNT,
    RUN_TOKEN_PREAMBLE,
    SCRATCH_ROOT,
    STATE_ROOT,
    RunSpec,
    SandboxError,
    SandboxUnavailableError,
)
from sprintbaton.sandbox.server.config import ServerConfig
from sprintbaton.sandbox.server.sessions import Session

RUN_SOCKET_DIR = "/run/sprintbaton"
RUN_SOCKET = f"{RUN_SOCKET_DIR}/egress.sock"
RUNINIT = str(Path(__file__).with_name("runinit.py"))


class EgressRelay:
    """The service side of a run's egress path (§8.1): accepts the run's
    connections (on its private Unix socket, or loopback TCP for the direct
    launcher), connects each to the worker's broker, and sends the run token
    first. The token lives here, never in the run."""

    def __init__(self, broker_address: str, token: str, *,
                 unix_path: Path | None = None):
        host, _, port = broker_address.rpartition(":")
        if not host or not port:
            raise SandboxError("no broker address configured "
                               "(SPRINTBATON_SANDBOX_BROKER_ADDRESS)")
        self._broker = (host, int(port))
        self._token = token.encode()
        if unix_path is not None:
            self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._listener.bind(str(unix_path))
            os.chmod(unix_path, 0o666)
            self.address = str(unix_path)
        else:
            self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._listener.bind(("127.0.0.1", 0))
            self.address = f"127.0.0.1:{self._listener.getsockname()[1]}"
        self._listener.listen(64)
        self._closed = threading.Event()
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while not self._closed.is_set():
            try:
                client, _ = self._listener.accept()
            except OSError:
                return
            threading.Thread(target=self._relay, args=(client,), daemon=True).start()

    def _relay(self, client: socket.socket) -> None:
        try:
            upstream = socket.create_connection(self._broker, timeout=30)
            upstream.settimeout(None)
        except OSError:
            client.close()
            return
        try:
            upstream.sendall(RUN_TOKEN_PREAMBLE + self._token + b"\r\n")
        except OSError:
            client.close()
            upstream.close()
            return
        _pipe(client, upstream)

    def close(self) -> None:
        self._closed.set()
        try:
            self._listener.close()
        except OSError:
            pass


def _pipe(a: socket.socket, b: socket.socket) -> None:
    def forward(src: socket.socket, dst: socket.socket) -> None:
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass
        finally:
            try:
                dst.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    t = threading.Thread(target=forward, args=(b, a), daemon=True)
    t.start()
    forward(a, b)
    t.join()
    a.close()
    b.close()


@dataclass
class Launched:
    proc: subprocess.Popen
    _cleanups: list = field(default_factory=list)

    def cleanup(self) -> None:
        for fn in reversed(self._cleanups):
            try:
                fn()
            except Exception:
                pass
        self._cleanups.clear()


class RunLauncher(Protocol):
    def health(self) -> None: ...
    def launch(self, session: Session, spec: RunSpec, *, streaming: bool) -> Launched: ...


def _run_dir(session: Session) -> Path:
    path = session.dir / "runs" / os.urandom(8).hex()
    path.mkdir(mode=0o700, parents=True)
    return path


class BwrapLauncher:
    def __init__(self, config: ServerConfig):
        self.config = config

    def health(self) -> None:
        """Fail loudly when bwrap cannot create the namespaces a run needs —
        the node must permit unprivileged user namespaces, or the container
        hold CAP_SYS_ADMIN (spec §15 q2)."""
        if shutil.which(self.config.bwrap) is None:
            raise SandboxUnavailableError(f"{self.config.bwrap!r} is not installed")
        if self.config.seccomp_path and not Path(self.config.seccomp_path).is_file():
            raise SandboxUnavailableError(
                f"seccomp profile {self.config.seccomp_path!r} is missing")
        token_file = os.environ.get("SPRINTBATON_SANDBOX_TOKEN_FILE", "")
        for bind in self.config.ro_binds:
            if token_file and (token_file == bind or token_file.startswith(bind.rstrip("/") + "/")):
                raise SandboxUnavailableError(
                    f"the sandbox token file {token_file!r} is under the read-only "
                    f"bind {bind!r}, which every run can read")
        probe = [self.config.bwrap, "--unshare-user", "--unshare-pid", "--unshare-net",
                 "--die-with-parent", "--clearenv", *self._ro_bind_args(),
                 "--proc", "/proc", "--dev", "/dev", "--", "/bin/true"]
        result = subprocess.run(probe, capture_output=True, text=True, timeout=30,
                                env={"PATH": "/usr/bin:/bin"})
        if result.returncode != 0:
            raise SandboxUnavailableError(
                "bubblewrap cannot create a sandbox on this node: "
                f"{result.stderr.strip()[-500:]} — enable unprivileged user "
                "namespaces or grant the sandbox container CAP_SYS_ADMIN")

    def _ro_bind_args(self) -> list[str]:
        args: list[str] = []
        for path in self.config.ro_binds:
            if os.path.islink(path):
                args += ["--symlink", os.readlink(path), path]
            elif os.path.exists(path):
                args += ["--ro-bind", path, path]
        return args

    def build_argv(self, session: Session, spec: RunSpec, run_dir: Path | None,
                   seccomp_fd: int | None) -> list[str]:
        a = [self.config.bwrap, "--unshare-user", "--unshare-pid", "--unshare-net",
             "--unshare-ipc", "--unshare-uts", "--unshare-cgroup-try",
             "--die-with-parent", "--new-session", "--clearenv"]
        for key, value in spec.env.items():
            a += ["--setenv", key, value]
        a += self._ro_bind_args()
        a += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
              "--tmpfs", self.config.home, "--dir", "/sprintbaton"]
        for tree in sorted(session.trees.values(), key=lambda t: t.mount.count("/")):
            storage = str(session.tree_dir(tree.name))
            a += ["--bind" if tree.mode == "rw" else "--ro-bind", storage, tree.mount]
            if tree.mode == "ro":
                for rel in tree.writable:
                    sub = session.tree_dir(tree.name).joinpath(*rel.split("/"))
                    sub.mkdir(parents=True, exist_ok=True)
                    a += ["--bind", str(sub), f"{tree.mount}/{rel}"]
        a += ["--bind", str(session.scratch_dir), SCRATCH_ROOT,
              "--bind", str(session.state_root), STATE_ROOT,
              "--bind", str(session.cache_dir), CACHE_MOUNT]
        if run_dir is not None:
            a += ["--bind", str(run_dir), RUN_SOCKET_DIR]
        if seccomp_fd is not None:
            a += ["--seccomp", str(seccomp_fd)]
        a += ["--chdir", spec.cwd, "--"]
        if run_dir is not None:
            a += [sys.executable, RUNINIT, "--bridge-port", str(BRIDGE_PORT),
                  "--socket", RUN_SOCKET, "--"]
        return a + list(spec.argv)

    def launch(self, session: Session, spec: RunSpec, *, streaming: bool) -> Launched:
        launched_cleanups: list = []
        run_dir = None
        if spec.egress is not None:
            run_dir = _run_dir(session)
            relay = EgressRelay(self.config.broker_address, spec.egress.token,
                                unix_path=run_dir / "egress.sock")
            launched_cleanups += [lambda: shutil.rmtree(run_dir, ignore_errors=True),
                                  relay.close]
        seccomp_fd = None
        if self.config.seccomp_path:
            seccomp_fd = os.open(self.config.seccomp_path, os.O_RDONLY)
            launched_cleanups.append(lambda: os.close(seccomp_fd))
        argv = self.build_argv(session, spec, run_dir, seccomp_fd)
        try:
            proc = subprocess.Popen(
                argv, stdin=subprocess.PIPE if (streaming or spec.stdin) else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
                pass_fds=(seccomp_fd,) if seccomp_fd is not None else (),
                # bwrap itself inherits nothing from the service either.
                env={"PATH": "/usr/local/bin:/usr/bin:/bin"},
            )
        except BaseException:
            Launched(proc=None, _cleanups=launched_cleanups).cleanup()  # type: ignore[arg-type]
            raise
        return Launched(proc=proc, _cleanups=launched_cleanups)


class DirectLauncher:
    """No isolation — protocol tests and local development only."""

    def __init__(self, config: ServerConfig):
        self.config = config

    def health(self) -> None:
        return None

    @staticmethod
    def _mappings(session: Session) -> list[tuple[str, str]]:
        maps = [(t.mount, str(session.tree_dir(t.name))) for t in session.trees.values()]
        maps += [(SCRATCH_ROOT, str(session.scratch_dir)),
                 (STATE_ROOT, str(session.state_root)),
                 (CACHE_MOUNT, str(session.cache_dir))]
        return sorted(maps, key=lambda m: len(m[0]), reverse=True)

    def translate(self, session: Session, value: str) -> str:
        for mount, storage in self._mappings(session):
            if value == mount or value.startswith(mount + "/"):
                return storage + value[len(mount):]
        # Embedded occurrences (e.g. a shell command naming a file).
        for mount, storage in self._mappings(session):
            value = value.replace(mount + "/", storage + "/")
        return value

    def launch(self, session: Session, spec: RunSpec, *, streaming: bool) -> Launched:
        cleanups: list = []
        env = {k: self.translate(session, v) for k, v in spec.env.items()}
        if spec.egress is not None:
            relay = EgressRelay(self.config.broker_address, spec.egress.token)
            cleanups.append(relay.close)
            env = {k: v.replace(BRIDGE_URL, f"http://{relay.address}") for k, v in env.items()}
        proc = subprocess.Popen(
            [self.translate(session, a) for a in spec.argv],
            cwd=self.translate(session, spec.cwd), env=env,
            stdin=subprocess.PIPE if (streaming or spec.stdin) else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
        )
        return Launched(proc=proc, _cleanups=cleanups)


def build_launcher(config: ServerConfig) -> RunLauncher:
    if config.launcher == "bwrap":
        return BwrapLauncher(config)
    if config.launcher == "direct":
        return DirectLauncher(config)
    raise SandboxError(f"unknown launcher {config.launcher!r} (bwrap | direct)")

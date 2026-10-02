"""RemoteSandbox — hosted mode's client of the sandbox service
(hosted-sandbox-isolation spec §5, §6).

Everything crosses an authenticated HTTP API; nothing is shared on disk with
the sandbox pod (invariant 2). A tree is refreshed incrementally: the worker
sends its clone's manifest, the service answers which paths (and which git
copies) it lacks, and only those travel (`transfer.build_upload`).
"""

from __future__ import annotations

import base64
import http.client
import json
import queue
import ssl
import threading
from urllib.parse import urlsplit

from sprintbaton.sandbox.base import (
    BRIDGE_URL,
    CACHE_MOUNT,
    FRAME_EOF,
    FRAME_EXIT,
    FRAME_KILL,
    FRAME_SPEC,
    FRAME_STDERR,
    FRAME_STDIN,
    FRAME_STDOUT,
    ChangeSet,
    ChangeSetRejected,
    DirEntry,
    RunResult,
    RunSpec,
    SandboxError,
    SandboxUnavailableError,
    SessionHandle,
    TreeMode,
    TreeSnapshot,
    dumps,
    encode_frame,
    read_frame,
)
from sprintbaton.sandbox.transfer import build_manifest, build_upload, git_head

DEFAULT_TIMEOUT = 120
UPLOAD_TIMEOUT = 1800
UPGRADE_PROTOCOL = "sprintbaton-stdio"

# The base environment of a model-issued command in a sandbox run (§5.1): a
# fixed set, never derived from the worker's own environment. Proxy variables
# point at the run's loopback bridge; with no egress grant nothing listens
# there and every connection is refused — which is the point.
SANDBOX_TOOL_ENV: dict[str, str] = {
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "HOME": "/home/sandbox",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "TERM": "dumb",
    "TMPDIR": "/tmp",
    "HTTPS_PROXY": BRIDGE_URL, "https_proxy": BRIDGE_URL,
    "HTTP_PROXY": BRIDGE_URL, "http_proxy": BRIDGE_URL,
    "NO_PROXY": "localhost,127.0.0.1", "no_proxy": "localhost,127.0.0.1",
    "GIT_CONFIG_NOSYSTEM": "1",
    # Per-tenant dependency caches (§6.1): one tenant can never poison
    # another's build, because /cache is that tenant's own directory.
    "npm_config_cache": f"{CACHE_MOUNT}/npm",
    "YARN_CACHE_FOLDER": f"{CACHE_MOUNT}/yarn",
    "PIP_CACHE_DIR": f"{CACHE_MOUNT}/pip",
    "GOMODCACHE": f"{CACHE_MOUNT}/go/mod",
    "GOCACHE": f"{CACHE_MOUNT}/go/build",
    "CARGO_HOME": f"{CACHE_MOUNT}/cargo",
    "GRADLE_USER_HOME": f"{CACHE_MOUNT}/gradle",
    "PLAYWRIGHT_BROWSERS_PATH": "/ms-playwright",
}


class _Client:
    def __init__(self, url: str, token: str, timeout: float,
                 ssl_context: ssl.SSLContext | None):
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise SandboxUnavailableError(f"invalid SPRINTBATON_SANDBOX_URL: {url!r}")
        self.scheme = parts.scheme
        self.host = parts.hostname
        self.port = parts.port or (443 if parts.scheme == "https" else 80)
        self.prefix = parts.path.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.ssl_context = ssl_context
        self.address = f"{self.host}:{self.port}"

    def connection(self, timeout: float | None = None) -> http.client.HTTPConnection:
        t = timeout or self.timeout
        if self.scheme == "https":
            return http.client.HTTPSConnection(
                self.host, self.port, timeout=t,
                context=self.ssl_context or ssl.create_default_context())
        return http.client.HTTPConnection(self.host, self.port, timeout=t)

    def request(self, method: str, path: str, body: bytes | dict | None = None,
                *, timeout: float | None = None,
                content_type: str = "application/json") -> tuple[int, bytes]:
        if isinstance(body, dict):
            body = dumps(body)
        conn = self.connection(timeout)
        try:
            conn.request(method, self.prefix + path, body=body, headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": content_type,
            })
            resp = conn.getresponse()
            return resp.status, resp.read()
        except OSError as e:
            raise SandboxUnavailableError(f"sandbox unreachable at {self.address}: {e}") from e
        finally:
            conn.close()

    def call(self, method: str, path: str, body: bytes | dict | None = None,
             **kwargs) -> dict:
        status, raw = self.request(method, path, body, **kwargs)
        data = json.loads(raw or b"{}") if raw else {}
        if status == 413:
            raise ChangeSetRejected(f"sandbox refused: {data.get('error', 'too large')}")
        if status >= 400:
            raise SandboxError(f"sandbox {method} {path} failed ({status}): "
                               f"{data.get('error', raw[:200])}")
        return data


class RemoteRunProcess:
    """A spawned run's stdio over the upgraded, framed connection."""

    def __init__(self, conn: http.client.HTTPConnection, reader):
        self._conn = conn
        self._reader = reader
        self._stdout: queue.Queue[bytes | None] = queue.Queue()
        self._stderr: list[bytes] = []
        self._exit: int | None = None
        self._done = threading.Event()
        self._write_lock = threading.Lock()
        self._eof = False
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        try:
            while True:
                frame = read_frame(self._reader)
                if frame is None:
                    break
                kind, payload = frame
                if kind == FRAME_STDOUT:
                    self._stdout.put(payload)
                elif kind == FRAME_STDERR:
                    self._stderr.append(payload)
                elif kind == FRAME_EXIT:
                    self._exit = int(json.loads(payload)["exit_code"])
                    break
        except (OSError, SandboxError, ValueError):
            pass
        finally:
            self._stdout.put(None)
            self._done.set()
            try:
                self._conn.close()
            except OSError:
                pass

    def _send(self, kind: bytes, payload: bytes = b"") -> None:
        with self._write_lock:
            sock = self._conn.sock
            if sock is None:
                raise SandboxError("sandbox run connection is closed")
            sock.sendall(encode_frame(kind, payload))

    def write_stdin(self, data: bytes) -> None:
        self._send(FRAME_STDIN, data)

    def close_stdin(self) -> None:
        try:
            self._send(FRAME_EOF)
        except (OSError, SandboxError):
            pass

    def read_stdout(self, max_bytes: int = 65536) -> bytes:
        if self._eof:
            return b""
        chunk = self._stdout.get()
        if chunk is None:
            self._eof = True
            return b""
        return chunk

    def stderr_text(self) -> str:
        return b"".join(self._stderr).decode(errors="replace")

    def wait(self, timeout: float | None = None) -> int:
        if not self._done.wait(timeout):
            raise TimeoutError("sandbox run still running")
        return self._exit if self._exit is not None else -1

    def kill(self) -> None:
        try:
            self._send(FRAME_KILL)
        except (OSError, SandboxError):
            pass


class RemoteSession:
    def __init__(self, client: _Client, session_id: str):
        self._c = client
        self.handle = SessionHandle(session_id=session_id, address=client.address)
        self._base = f"/v1/sessions/{session_id}"

    # ---------------------------------------------------------------- trees

    def put_tree(self, name: str, snapshot: TreeSnapshot, mode: TreeMode) -> str:
        manifest, repos = build_manifest(snapshot.source)
        heads = {}
        for repo in repos:
            head = git_head(f"{snapshot.source}/{repo}" if repo else snapshot.source)
            if head:
                heads[repo] = head
        plan = self._c.call("POST", f"{self._base}/trees/{name}/plan", {
            "mount": snapshot.mount_path, "mode": mode,
            "writable": list(snapshot.writable), "manifest": manifest,
            "git_heads": heads,
        }, timeout=UPLOAD_TIMEOUT)
        body = build_upload(snapshot.source, manifest, plan.get("need", []),
                            plan.get("need_git", []), snapshot.git_depth)
        self._c.call("PUT", f"{self._base}/trees/{name}", body,
                     timeout=UPLOAD_TIMEOUT, content_type="application/x-tar")
        return snapshot.mount_path

    def drop_tree(self, name: str) -> None:
        self._c.call("DELETE", f"{self._base}/trees/{name}")

    def collect(self, tree: str, paths: list[str] | None = None) -> ChangeSet:
        data = self._c.call("POST", f"{self._base}/trees/{tree}/collect",
                            {"paths": paths}, timeout=UPLOAD_TIMEOUT)
        return ChangeSet.from_json(data)

    # ---------------------------------------------------------------- files

    def write_file(self, path: str, data: bytes) -> None:
        self._c.call("POST", f"{self._base}/files/write",
                     {"path": path, "data": base64.b64encode(data).decode()})

    def read_file(self, path: str) -> bytes:
        data = self._c.call("POST", f"{self._base}/files/read", {"path": path})
        return base64.b64decode(data.get("data") or "")

    def list_dir(self, path: str) -> list[DirEntry]:
        data = self._c.call("POST", f"{self._base}/files/list", {"path": path})
        return [DirEntry(name=e["name"], kind=e["kind"], size=int(e.get("size", 0)))
                for e in data.get("entries", [])]

    def make_scratch(self, prefix: str = "run") -> str:
        return self._c.call("POST", f"{self._base}/scratch", {"prefix": prefix})["path"]

    def discard_scratch(self, path: str) -> None:
        try:
            self._c.call("POST", f"{self._base}/scratch/discard", {"path": path})
        except SandboxError:
            pass  # best-effort; the session TTL reclaims it

    def state_dir(self, name: str) -> str:
        return self._c.call("POST", f"{self._base}/state", {"name": name})["path"]

    def tool_env(self) -> dict[str, str]:
        return dict(SANDBOX_TOOL_ENV)

    # ----------------------------------------------------------------- runs

    def run(self, spec: RunSpec) -> RunResult:
        data = self._c.call("POST", f"{self._base}/runs", spec.to_json(),
                            timeout=spec.timeout_seconds + 60)
        return RunResult.from_json(data)

    def spawn(self, spec: RunSpec) -> RemoteRunProcess:
        conn = self._c.connection(timeout=None)
        try:
            conn.putrequest("GET", self._c.prefix + f"{self._base}/spawn")
            conn.putheader("Authorization", f"Bearer {self._c.token}")
            conn.putheader("Upgrade", UPGRADE_PROTOCOL)
            conn.putheader("Connection", "Upgrade")
            conn.endheaders()
            resp = conn.getresponse()
        except OSError as e:
            conn.close()
            raise SandboxUnavailableError(f"sandbox unreachable: {e}") from e
        if resp.status != 101:
            body = resp.read()
            conn.close()
            raise SandboxError(f"sandbox spawn refused ({resp.status}): {body[:200]!r}")
        conn.sock.sendall(encode_frame(FRAME_SPEC, dumps(spec.to_json())))
        return RemoteRunProcess(conn, resp.fp)

    def close(self) -> None:
        self._c.call("DELETE", self._base)


class RemoteSandbox:
    isolated = True

    def __init__(self, url: str, token: str, *, timeout: float = DEFAULT_TIMEOUT,
                 ssl_context: ssl.SSLContext | None = None):
        if not url or not token:
            raise SandboxUnavailableError(
                "the remote sandbox needs SPRINTBATON_SANDBOX_URL and "
                "SPRINTBATON_SANDBOX_TOKEN")
        self._c = _Client(url, token, timeout, ssl_context)

    def open_session(self, owner_id: str, task_id: str) -> RemoteSession:
        data = self._c.call("POST", "/v1/sessions",
                            {"owner_id": owner_id, "task_id": task_id})
        return RemoteSession(self._c, data["session_id"])

    def resume_session(self, handle: SessionHandle) -> RemoteSession | None:
        status, _ = self._c.request("GET", f"/v1/sessions/{handle.session_id}")
        if status == 404:
            return None
        if status >= 400:
            raise SandboxError(f"sandbox resume failed ({status})")
        return RemoteSession(self._c, handle.session_id)

    def health(self) -> None:
        try:
            self._c.call("GET", "/v1/health", timeout=15)
        except SandboxError as e:
            raise SandboxUnavailableError(f"sandbox health check failed: {e}") from e


class UnavailableSandbox:
    """Hosted mode with no usable sandbox (§5.2). Every use fails closed —
    there is deliberately no local fallback (invariant 8); `serve` refuses to
    start on it before any task could get this far."""

    isolated = True

    def __init__(self, reason: str):
        self.reason = reason

    def open_session(self, owner_id: str, task_id: str):
        raise SandboxUnavailableError(self.reason)

    def resume_session(self, handle: SessionHandle):
        raise SandboxUnavailableError(self.reason)

    def health(self) -> None:
        raise SandboxUnavailableError(self.reason)

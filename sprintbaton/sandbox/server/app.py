"""The sandbox service's session API (hosted-sandbox-isolation spec §5.1, §6).

Stdlib HTTP (`ThreadingHTTPServer`) so the image needs no web framework.
Every request carries the worker<->sandbox bearer token. JSON bodies for
control calls; the tree upload is a raw tar body; `spawn` upgrades the
connection to a framed bidirectional byte stream (`base.encode_frame`).

    GET    /v1/health
    POST   /v1/sessions                              {owner_id, task_id} -> {session_id}
    GET    /v1/sessions/<sid>                        200 | 404 (resume)
    DELETE /v1/sessions/<sid>
    POST   /v1/sessions/<sid>/trees/<name>/plan      -> {need, need_git}
    PUT    /v1/sessions/<sid>/trees/<name>           tar body; commits the plan
    DELETE /v1/sessions/<sid>/trees/<name>
    POST   /v1/sessions/<sid>/trees/<name>/collect   {paths} -> ChangeSet
    POST   /v1/sessions/<sid>/files/read|write|list
    POST   /v1/sessions/<sid>/scratch                {prefix} -> {path}
    POST   /v1/sessions/<sid>/scratch/discard        {path}
    POST   /v1/sessions/<sid>/state                  {name} -> {path}
    POST   /v1/sessions/<sid>/runs                   RunSpec -> RunResult
    GET    /v1/sessions/<sid>/spawn                  Upgrade: sprintbaton-stdio
"""

from __future__ import annotations

import base64
import hmac
import json
import logging
import os
import signal
import ssl
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from sprintbaton.sandbox.base import (
    FRAME_EOF,
    FRAME_EXIT,
    FRAME_KILL,
    FRAME_SPEC,
    FRAME_STDERR,
    FRAME_STDIN,
    FRAME_STDOUT,
    RunResult,
    RunSpec,
    SandboxError,
    dumps,
    encode_frame,
    read_frame,
)
from sprintbaton.sandbox.server.config import ServerConfig
from sprintbaton.sandbox.server.launcher import RunLauncher, build_launcher
from sprintbaton.sandbox.server.sessions import (
    ChangeSetTooLarge,
    NotInSession,
    ReadOnlyPath,
    Session,
    SessionStore,
    Tree,
)

log = logging.getLogger("sprintbaton.sandbox.server")

MAX_JSON_BODY = 256 * 1024 * 1024
MAX_OUTPUT = 4 * 1024 * 1024
GIT_LIST_TIMEOUT = 120
UPGRADE_PROTOCOL = "sprintbaton-stdio"


class SandboxService:
    """The service's state: sessions plus the run launcher."""

    def __init__(self, config: ServerConfig, launcher: RunLauncher | None = None):
        if not config.token:
            raise SandboxError("SPRINTBATON_SANDBOX_TOKEN(_FILE) is required")
        self.config = config
        self.launcher = launcher or build_launcher(config)
        self.store = SessionStore(config.root, config.max_sessions,
                                  config.session_ttl_seconds)

    # ----------------------------------------------------------------- runs

    def run(self, session: Session, spec: RunSpec) -> RunResult:
        launched = self.launcher.launch(session, spec, streaming=False)
        proc = launched.proc
        try:
            try:
                out, err = proc.communicate(input=spec.stdin, timeout=spec.timeout_seconds)
                timed_out = False
            except subprocess.TimeoutExpired:
                _kill(proc)
                out, err = proc.communicate()
                timed_out = True
        finally:
            launched.cleanup()
        return RunResult(exit_code=124 if timed_out else proc.returncode,
                         stdout=out[-MAX_OUTPUT:].decode(errors="replace"),
                         stderr=err[-MAX_OUTPUT:].decode(errors="replace"),
                         timed_out=timed_out)

    def list_git_candidates(self, session: Session, tree: Tree) -> list[str] | None:
        """Non-ignored paths of a tree's root repository, listed by git running
        *inside a sandbox run* — the sandbox-side .git is untrusted, so the
        service never runs git on it in its own process (§6.3)."""
        if "" not in tree.git_heads:
            return None
        result = self.run(session, RunSpec(
            argv=["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
                  "--no-optional-locks", "ls-files", "-z", "--cached", "--others",
                  "--exclude-standard"],
            cwd=tree.mount, env={"PATH": "/usr/local/bin:/usr/bin:/bin",
                                 "HOME": self.config.home, "GIT_CONFIG_NOSYSTEM": "1"},
            timeout_seconds=GIT_LIST_TIMEOUT))
        if result.exit_code != 0:
            return None
        return [p for p in result.stdout.split("\x00") if p]

    def evict_loop(self, interval: float = 60.0) -> None:
        while True:
            time.sleep(interval)
            try:
                evicted = self.store.evict_expired()
                if evicted:
                    log.info("evicted idle sessions", extra={"count": evicted})
            except Exception:
                log.exception("session eviction failed")


def _kill(proc: subprocess.Popen) -> None:
    """Kill the run's whole process group — bwrap and, through the PID
    namespace, everything the run started (§10.2 teardown)."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


class _Handler(BaseHTTPRequestHandler):
    service: SandboxService
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:  # noqa: D401 - quiet default logger
        log.debug(fmt % args)

    # ------------------------------------------------------------- plumbing

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        expected = f"Bearer {self.service.config.token}"
        return hmac.compare_digest(header.encode(), expected.encode())

    def _send(self, status: int, payload: dict | None = None) -> None:
        body = dumps(payload or {})
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_JSON_BODY:
            raise SandboxError("request body too large")
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw or b"{}")

    def _route(self, method: str) -> None:
        if not self._authorized():
            return self._send(401, {"error": "unauthorized"})
        parts = [p for p in self.path.split("?", 1)[0].split("/") if p]
        try:
            if parts[:1] != ["v1"]:
                return self._send(404, {"error": "not found"})
            return self._dispatch(method, parts[1:])
        except NotInSession as e:
            return self._send(403, {"error": str(e)})
        except ReadOnlyPath as e:
            return self._send(403, {"error": str(e)})
        except ChangeSetTooLarge as e:
            return self._send(413, {"error": str(e)})
        except (SandboxError, ValueError, KeyError) as e:
            return self._send(400, {"error": str(e)})
        except Exception as e:  # pragma: no cover - defensive
            log.exception("sandbox request failed")
            return self._send(500, {"error": type(e).__name__})

    def do_GET(self) -> None:
        self._route("GET")

    def do_POST(self) -> None:
        self._route("POST")

    def do_PUT(self) -> None:
        self._route("PUT")

    def do_DELETE(self) -> None:
        self._route("DELETE")

    # -------------------------------------------------------------- routes

    def _dispatch(self, method: str, parts: list[str]) -> None:
        svc = self.service
        if parts == ["health"] and method == "GET":
            svc.launcher.health()
            return self._send(200, {"ok": True, "sessions": svc.store.count()})
        if parts == ["sessions"] and method == "POST":
            body = self._json()
            session = svc.store.open(str(body["owner_id"]), str(body["task_id"]))
            return self._send(200, {"session_id": session.id})
        if len(parts) < 2 or parts[0] != "sessions":
            return self._send(404, {"error": "not found"})
        sid, rest = parts[1], parts[2:]
        if not rest and method == "DELETE":
            svc.store.close(sid)
            return self._send(200)
        session = svc.store.get(sid)
        if session is None:
            return self._send(404, {"error": "no such session"})
        if not rest and method == "GET":
            return self._send(200, {"session_id": session.id})
        return self._session_route(session, method, rest)

    def _session_route(self, session: Session, method: str, rest: list[str]) -> None:
        svc = self.service
        head = rest[0]
        if head == "trees" and len(rest) >= 2:
            name = rest[1]
            if len(rest) == 3 and rest[2] == "plan" and method == "POST":
                body = self._json()
                need, need_git = session.plan_tree(
                    name, body["mount"], body["mode"], list(body.get("writable", [])),
                    dict(body.get("manifest", {})), dict(body.get("git_heads", {})))
                return self._send(200, {"need": need, "need_git": need_git})
            if len(rest) == 2 and method == "PUT":
                length = int(self.headers.get("Content-Length") or 0)
                session.upload_tree(name, _Limited(self.rfile, length),
                                    svc.config.max_upload_bytes)
                return self._send(200)
            if len(rest) == 2 and method == "DELETE":
                session.drop_tree(name)
                return self._send(200)
            if len(rest) == 3 and rest[2] == "collect" and method == "POST":
                body = self._json()
                changes = session.collect(
                    name, body.get("paths"), svc.config.max_changeset_bytes,
                    lambda tree: svc.list_git_candidates(session, tree))
                return self._send(200, changes.to_json())
        if head == "files" and len(rest) == 2 and method == "POST":
            body = self._json()
            op = rest[1]
            if op == "read":
                data = session.read_file(body["path"])
                return self._send(200, {"data": base64.b64encode(data).decode()})
            if op == "write":
                session.write_file(body["path"], base64.b64decode(body.get("data") or ""))
                return self._send(200)
            if op == "list":
                entries = session.list_dir(body["path"])
                return self._send(200, {"entries": [
                    {"name": e.name, "kind": e.kind, "size": e.size} for e in entries]})
        if head == "scratch" and method == "POST":
            body = self._json()
            if len(rest) == 1:
                return self._send(200, {"path": session.make_scratch(str(body.get("prefix", "run")))})
            if rest[1:] == ["discard"]:
                session.discard_scratch(str(body["path"]))
                return self._send(200)
        if head == "state" and method == "POST":
            body = self._json()
            return self._send(200, {"path": session.state_dir(str(body["name"]))})
        if head == "runs" and method == "POST":
            spec = RunSpec.from_json(self._json())
            return self._send(200, svc.run(session, spec).to_json())
        if head == "spawn" and method == "GET":
            return self._spawn(session)
        return self._send(404, {"error": "not found"})

    # --------------------------------------------------------------- spawn

    def _spawn(self, session: Session) -> None:
        if self.headers.get("Upgrade", "").lower() != UPGRADE_PROTOCOL:
            return self._send(400, {"error": "spawn requires Upgrade: " + UPGRADE_PROTOCOL})
        self.send_response(101)
        self.send_header("Upgrade", UPGRADE_PROTOCOL)
        self.send_header("Connection", "Upgrade")
        self.end_headers()
        self.wfile.flush()
        self.close_connection = True
        first = read_frame(self.rfile)
        if first is None or first[0] != FRAME_SPEC:
            return
        spec = RunSpec.from_json(json.loads(first[1]))
        launched = self.service.launcher.launch(session, spec, streaming=True)
        proc = launched.proc
        write_lock = threading.Lock()

        def send(kind: bytes, payload: bytes = b"") -> None:
            with write_lock:
                try:
                    self.wfile.write(encode_frame(kind, payload))
                    self.wfile.flush()
                except OSError:
                    pass

        def pump(stream, kind: bytes) -> None:
            for chunk in iter(lambda: stream.read1(65536), b""):
                send(kind, chunk)

        out_t = threading.Thread(target=pump, args=(proc.stdout, FRAME_STDOUT), daemon=True)
        err_t = threading.Thread(target=pump, args=(proc.stderr, FRAME_STDERR), daemon=True)
        out_t.start()
        err_t.start()
        timer = threading.Timer(spec.timeout_seconds, _kill, args=(proc,))
        timer.daemon = True
        timer.start()

        def client_input() -> None:
            try:
                while True:
                    frame = read_frame(self.rfile)
                    if frame is None:
                        break
                    kind, payload = frame
                    if kind == FRAME_STDIN and proc.stdin:
                        proc.stdin.write(payload)
                        proc.stdin.flush()
                    elif kind == FRAME_EOF and proc.stdin:
                        proc.stdin.close()
                    elif kind == FRAME_KILL:
                        _kill(proc)
                        break
            except (OSError, SandboxError, ValueError):
                pass
            # The client's stream ended: nothing can read the run's output
            # any more, so the run ends too (stdin EOF is its own frame).
            if proc.poll() is None:
                _kill(proc)

        in_t = threading.Thread(target=client_input, daemon=True)
        in_t.start()
        try:
            code = proc.wait()
            out_t.join(timeout=10)
            err_t.join(timeout=10)
            send(FRAME_EXIT, dumps({"exit_code": code}))
        finally:
            timer.cancel()
            launched.cleanup()


class _Limited:
    """A read-only view of at most `length` bytes of the request body."""

    def __init__(self, stream, length: int):
        self._stream = stream
        self._left = length

    def read(self, n: int = -1) -> bytes:
        if self._left <= 0:
            return b""
        n = self._left if n is None or n < 0 else min(n, self._left)
        data = self._stream.read(n)
        self._left -= len(data)
        return data


def make_server(service: SandboxService) -> ThreadingHTTPServer:
    handler = type("SandboxHandler", (_Handler,), {"service": service})
    server = ThreadingHTTPServer((service.config.host, service.config.port), handler)
    server.daemon_threads = True
    if service.config.tls_cert and service.config.tls_key:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(service.config.tls_cert, service.config.tls_key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    return server


def serve(config: ServerConfig | None = None) -> None:  # pragma: no cover - process entry
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper())
    config = config or ServerConfig.from_env()
    service = SandboxService(config)
    service.launcher.health()  # fail loudly at boot (spec §15 q2)
    threading.Thread(target=service.evict_loop, daemon=True).start()
    server = make_server(service)
    log.info("sandbox service listening on %s:%s", config.host, server.server_address[1])
    server.serve_forever()

"""The egress/credential broker — runs in the worker (hosted-sandbox-isolation
spec §8.2).

A sandbox run has no network interface but loopback. Its only way out is a
per-run bridge the sandbox service relays to this broker, prefixing every
connection with the run's **run token** — which the run itself never sees
(§8.1). The broker serves exactly two routes:

1. **A forward proxy with an allow-list** — `CONNECT host:port` to a host in
   SPRINTBATON_SANDBOX_EGRESS_ALLOWLIST (package registries by default). No TLS
   interception: the run talks TLS end to end with the registry.
2. **A model-API reverse proxy with credential injection** — only for a token
   minted with an `UpstreamCredential` (a `broker`-delivery harness, §8.3). The
   run's placeholder credential header is replaced with the owner's real one
   and the request forwarded over TLS to the token's upstream (Provider.baseUrl,
   default https://api.anthropic.com). Everything else about the request and
   the response passes through verbatim — `anthropic-beta` above all, whose
   value set carries the OAuth capability a subscription credential needs and
   changes between CLI releases, so it is never allow-listed (§8.2).

A token is bound to one (owner, task, run, credential) and held only in this
process's memory, so a run cannot spend another owner's credential even in
principle, and a leaked token dies with its run.
"""

from __future__ import annotations

import http.client
import logging
import secrets
import socket
import socketserver
import ssl
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urlsplit

from sprintbaton.sandbox.base import RUN_TOKEN_PREAMBLE

log = logging.getLogger(__name__)

DEFAULT_ANTHROPIC_UPSTREAM = "https://api.anthropic.com"
DEFAULT_EGRESS_ALLOWLIST = (
    "registry.npmjs.org", "registry.yarnpkg.com",
    "pypi.org", "files.pythonhosted.org",
    "proxy.golang.org", "sum.golang.org",
    "repo1.maven.org", "repo.maven.apache.org",
    "crates.io", "index.crates.io", "static.crates.io",
    "rubygems.org",
)
DEFAULT_TOKEN_TTL_SECONDS = 4 * 3600
UPSTREAM_TIMEOUT_SECONDS = 600
_MAX_HEAD = 64 * 1024
_MAX_BODY = 64 * 1024 * 1024
_CHUNK = 64 * 1024

# The model-API endpoints the `claude` binary calls through ANTHROPIC_BASE_URL
# (Claude Code gateway compatibility guide). Everything else is a 404.
_FORWARDED_ENDPOINTS: dict[str, frozenset[str]] = {
    "/v1/messages": frozenset({"POST"}),
    "/v1/messages/count_tokens": frozenset({"POST"}),
    "/v1/models": frozenset({"GET"}),
}
_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "proxy-connection", "te", "trailer", "trailers", "transfer-encoding",
    "upgrade", "host", "content-length",
})
# Credential headers a run may have sent — dropped before injection (§8.2).
_CREDENTIAL_HEADERS = frozenset({"authorization", "x-api-key", "cookie",
                                 "anthropic-auth-token"})


@dataclass(frozen=True)
class UpstreamCredential:
    """The owner's resolved credential and where to spend it."""

    kind: Literal["subscription", "metered"]
    value: str = field(repr=False)
    upstream: str = DEFAULT_ANTHROPIC_UPSTREAM
    provider: str = "anthropic"


@dataclass(frozen=True)
class RunToken:
    token: str = field(repr=False)
    owner_id: str
    task_id: str
    run_id: str
    credential: UpstreamCredential | None
    expires_at: float
    # Hosts this run may CONNECT to beyond the allow-list: the model API of an
    # `env`-delivery run, whose process holds the credential and talks TLS to
    # the provider itself (§8.3).
    extra_hosts: tuple[str, ...] = ()


class RunTokenRegistry:
    """Live run tokens, in worker memory only."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._tokens: dict[str, RunToken] = {}
        self._lock = threading.Lock()
        self._clock = clock

    def mint(self, *, owner_id: str, task_id: str,
             credential: UpstreamCredential | None = None,
             ttl_seconds: int = DEFAULT_TOKEN_TTL_SECONDS,
             extra_hosts: tuple[str, ...] = ()) -> RunToken:
        token = RunToken(token=secrets.token_urlsafe(32), owner_id=owner_id,
                         task_id=task_id, run_id=secrets.token_hex(8),
                         credential=credential,
                         expires_at=self._clock() + ttl_seconds,
                         extra_hosts=tuple(extra_hosts))
        with self._lock:
            self._purge()
            self._tokens[token.token] = token
        return token

    def lookup(self, token: str) -> RunToken | None:
        with self._lock:
            found = self._tokens.get(token)
            if found is None:
                return None
            if found.expires_at <= self._clock():
                del self._tokens[token]
                return None
            return found

    def revoke(self, token: str) -> None:
        with self._lock:
            self._tokens.pop(token, None)

    def _purge(self) -> None:
        now = self._clock()
        for key in [k for k, v in self._tokens.items() if v.expires_at <= now]:
            del self._tokens[key]


def host_allowed(host: str, allowlist: tuple[str, ...]) -> bool:
    """Exact host match, or a `*.suffix` entry matching a subdomain."""
    host = host.lower().rstrip(".")
    for entry in allowlist:
        entry = entry.strip().lower()
        if not entry:
            continue
        if entry.startswith("*."):
            if host.endswith(entry[1:]):
                return True
        elif host == entry:
            return True
    return False


# Upstream connection factory: (scheme, host, port, timeout) -> HTTPConnection.
# A seam so tests can point the reverse proxy at a local plain-HTTP server.
ConnectionFactory = Callable[[str, str, int, float], http.client.HTTPConnection]


def _default_connection(scheme: str, host: str, port: int,
                        timeout: float) -> http.client.HTTPConnection:
    if scheme == "https":
        return http.client.HTTPSConnection(host, port, timeout=timeout,
                                           context=ssl.create_default_context())
    return http.client.HTTPConnection(host, port, timeout=timeout)


def _default_tunnel(host: str, port: int, timeout: float) -> socket.socket:
    return socket.create_connection((host, port), timeout=timeout)


class EgressBroker:
    """The broker's TCP server plus the run-token registry it checks."""

    def __init__(self, host: str = "0.0.0.0", port: int = 8081,
                 allowlist: tuple[str, ...] = DEFAULT_EGRESS_ALLOWLIST,
                 registry: RunTokenRegistry | None = None,
                 connection_factory: ConnectionFactory = _default_connection,
                 tunnel_factory: Callable[[str, int, float], socket.socket] = _default_tunnel,
                 allowed_ports: tuple[int, ...] = (443,)):
        self.host = host
        self.port = port
        self.allowlist = tuple(allowlist)
        self.allowed_ports = allowed_ports
        self.registry = registry or RunTokenRegistry()
        self._connect = connection_factory
        self._tunnel = tunnel_factory
        self._server: socketserver.ThreadingTCPServer | None = None
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------ tokens

    def mint(self, *, owner_id: str, task_id: str,
             credential: UpstreamCredential | None = None,
             ttl_seconds: int = DEFAULT_TOKEN_TTL_SECONDS,
             extra_hosts: tuple[str, ...] = ()) -> str:
        return self.registry.mint(owner_id=owner_id, task_id=task_id,
                                  credential=credential, ttl_seconds=ttl_seconds,
                                  extra_hosts=extra_hosts).token

    def revoke(self, token: str) -> None:
        self.registry.revoke(token)

    # ------------------------------------------------------------ server

    def start(self) -> tuple[str, int]:
        broker = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                broker.handle_connection(self.connection, self.rfile, self.wfile)

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self._server = Server((self.host, self.port), Handler)
        self.port = self._server.server_address[1]
        server = self._server
        self._thread = threading.Thread(
            target=lambda: server.serve_forever(poll_interval=0.2),
            name="sprintbaton-egress-broker", daemon=True)
        self._thread.start()
        log.info("egress broker listening", extra={"port": self.port})
        return self.host, self.port

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    # ------------------------------------------------------------ protocol

    def handle_connection(self, conn: socket.socket, rfile, wfile) -> None:
        preamble = rfile.readline(512)
        if not preamble.startswith(RUN_TOKEN_PREAMBLE):
            return _respond(wfile, 407, "missing run token")
        token = self.registry.lookup(preamble[len(RUN_TOKEN_PREAMBLE):].strip().decode(
            errors="replace"))
        if token is None:
            return _respond(wfile, 407, "unknown or expired run token")
        head = _read_head(rfile)
        if head is None:
            return _respond(wfile, 400, "malformed request")
        method, target, headers = head
        if method == "CONNECT":
            return self._connect_tunnel(conn, rfile, wfile, target,
                                        self.allowlist + token.extra_hosts)
        if not target.startswith("/"):
            # Absolute-form plain-HTTP proxying: registries are HTTPS, so
            # there is nothing legitimate to serve here.
            return _respond(wfile, 403, "only CONNECT and the model-API route are served")
        return self._model_api(token, method, target, headers, rfile, wfile)

    def _connect_tunnel(self, conn: socket.socket, rfile, wfile, target: str,
                        allowlist: tuple[str, ...]) -> None:
        host, _, port_text = target.rpartition(":")
        host = host.strip("[]")
        try:
            port = int(port_text)
        except ValueError:
            return _respond(wfile, 400, "bad CONNECT target")
        if port not in self.allowed_ports or not host_allowed(host, allowlist):
            log.info("egress refused", extra={"host": host, "port": port})
            return _respond(wfile, 403, f"{host}:{port} is not on the egress allow-list")
        try:
            upstream = self._tunnel(host, port, UPSTREAM_TIMEOUT_SECONDS)
        except OSError as e:
            return _respond(wfile, 502, f"cannot reach {host}: {e}")
        wfile.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        wfile.flush()
        # Client -> upstream reads through rfile, so bytes the client
        # pipelined past the CONNECT head (already in its buffer) go first.
        _pipe(rfile.read1, conn, upstream)

    def _model_api(self, token: RunToken, method: str, target: str,
                   headers: list[tuple[str, str]], rfile, wfile) -> None:
        path = target.split("?", 1)[0]
        allowed_methods = _FORWARDED_ENDPOINTS.get(path)
        if allowed_methods is None or method not in allowed_methods:
            return _respond(wfile, 404, "not found")
        if token.credential is None:
            return _respond(wfile, 403, "this run has no model-API access")
        body = _read_body(rfile, headers)
        if body is None:
            return _respond(wfile, 400, "malformed or oversized body")
        cred = token.credential
        upstream = urlsplit(cred.upstream)
        scheme = upstream.scheme or "https"
        port = upstream.port or (443 if scheme == "https" else 80)
        forwarded = [(k, v) for k, v in headers
                     if k.lower() not in _HOP_BY_HOP and k.lower() not in _CREDENTIAL_HEADERS]
        if cred.kind == "subscription":
            forwarded.append(("Authorization", f"Bearer {cred.value}"))
        else:
            forwarded.append(("x-api-key", cred.value))
        prefix = upstream.path.rstrip("/")
        try:
            conn = self._connect(scheme, upstream.hostname or "", port,
                                 UPSTREAM_TIMEOUT_SECONDS)
            conn.putrequest(method, prefix + target, skip_host=True,
                            skip_accept_encoding=True)
            conn.putheader("Host", upstream.netloc.rsplit("@", 1)[-1])
            for key, value in forwarded:
                conn.putheader(key, value)
            conn.putheader("Content-Length", str(len(body)))
            conn.endheaders(body)
            resp = conn.getresponse()
        except OSError as e:
            return _respond(wfile, 502, f"upstream unreachable: {e}")
        wfile.write(f"HTTP/1.1 {resp.status} {resp.reason}\r\n".encode())
        for key, value in resp.getheaders():
            # Response headers pass through verbatim — the
            # anthropic-ratelimit-unified-* set in particular, which the CLI
            # reads to tell a plan limit from a throttle (§8.2).
            if key.lower() in _HOP_BY_HOP:
                continue
            wfile.write(f"{key}: {value}\r\n".encode())
        # Close-delimited body: correct for streamed (SSE) responses without
        # buffering them, and for fixed-length ones alike.
        wfile.write(b"Connection: close\r\n\r\n")
        wfile.flush()
        try:
            while True:
                chunk = resp.read1(_CHUNK)
                if not chunk:
                    break
                wfile.write(chunk)
                wfile.flush()
        finally:
            conn.close()


def _respond(wfile, status: int, message: str) -> None:
    reason = http.client.responses.get(status, "Error")
    body = (message + "\n").encode()
    try:
        wfile.write(f"HTTP/1.1 {status} {reason}\r\nContent-Type: text/plain\r\n"
                    f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                    + body)
        wfile.flush()
    except OSError:
        pass


def _read_head(rfile) -> tuple[str, str, list[tuple[str, str]]] | None:
    total = 0
    request_line = rfile.readline(_MAX_HEAD)
    total += len(request_line)
    parts = request_line.decode("latin-1").strip().split(" ")
    if len(parts) != 3 or not parts[2].startswith("HTTP/"):
        return None
    headers: list[tuple[str, str]] = []
    while True:
        line = rfile.readline(_MAX_HEAD)
        total += len(line)
        if total > _MAX_HEAD or not line:
            return None
        if line in (b"\r\n", b"\n"):
            break
        key, sep, value = line.decode("latin-1").partition(":")
        if not sep:
            return None
        headers.append((key.strip(), value.strip()))
    return parts[0].upper(), parts[1], headers


def _header(headers: list[tuple[str, str]], name: str) -> str | None:
    for key, value in headers:
        if key.lower() == name:
            return value
    return None


def _read_body(rfile, headers: list[tuple[str, str]]) -> bytes | None:
    if (_header(headers, "transfer-encoding") or "").lower() == "chunked":
        out = bytearray()
        while True:
            size_line = rfile.readline(1024)
            try:
                size = int(size_line.split(b";")[0].strip(), 16)
            except ValueError:
                return None
            if size == 0:
                while rfile.readline(1024) not in (b"\r\n", b"\n", b""):
                    pass
                return bytes(out)
            if len(out) + size > _MAX_BODY:
                return None
            out.extend(rfile.read(size))
            rfile.readline(8)
    length = _header(headers, "content-length")
    if length is None:
        return b""
    try:
        n = int(length)
    except ValueError:
        return None
    if n < 0 or n > _MAX_BODY:
        return None
    data = rfile.read(n)
    return data if len(data) == n else None


def _pipe(client_read: Callable[[int], bytes], client: socket.socket,
          upstream: socket.socket) -> None:
    """Shuttle bytes both ways until either side closes."""

    def upstream_to_client() -> None:
        try:
            while True:
                data = upstream.recv(_CHUNK)
                if not data:
                    break
                client.sendall(data)
        except OSError:
            pass
        finally:
            try:
                client.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    t = threading.Thread(target=upstream_to_client, daemon=True)
    t.start()
    try:
        while True:
            data = client_read(_CHUNK)
            if not data:
                break
            upstream.sendall(data)
    except OSError:
        pass
    finally:
        try:
            upstream.shutdown(socket.SHUT_WR)
        except OSError:
            pass
    t.join(timeout=UPSTREAM_TIMEOUT_SECONDS)
    upstream.close()

"""Sandbox-service configuration, read from the environment (no pydantic, no
Settings: the service imports nothing outside `sandbox.base`)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_RO_BINDS = ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/lib32",
                    "/etc", "/opt")


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    return int(raw) if raw.strip() else default


def read_token() -> str:
    """The worker<->sandbox token: a mounted file is preferred over an env var,
    since a process's initial environment stays readable in /proc for its
    whole life."""
    path = os.environ.get("SPRINTBATON_SANDBOX_TOKEN_FILE", "")
    if path and Path(path).is_file():
        return Path(path).read_text().strip()
    return os.environ.get("SPRINTBATON_SANDBOX_TOKEN", "").strip()


@dataclass
class ServerConfig:
    token: str
    root: Path
    host: str = "0.0.0.0"
    port: int = 8090
    # host:port of the worker's egress broker, reached through the relay (§8.1).
    broker_address: str = ""
    launcher: str = "bwrap"                   # "bwrap" | "direct" (dev/tests only)
    bwrap: str = "bwrap"
    seccomp_path: str = ""                    # compiled BPF (seccomp.py); "" = none
    ro_binds: tuple[str, ...] = DEFAULT_RO_BINDS
    home: str = "/home/sandbox"
    max_sessions: int = 4
    session_ttl_seconds: int = 86400
    max_changeset_bytes: int = 100 * 1024 * 1024
    max_upload_bytes: int = 2 * 1024 * 1024 * 1024
    tls_cert: str = ""
    tls_key: str = ""
    extra_env: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> ServerConfig:
        binds = os.environ.get("SPRINTBATON_SANDBOX_RO_BINDS", "")
        return cls(
            token=read_token(),
            root=Path(os.environ.get("SPRINTBATON_SANDBOX_ROOT", "/var/lib/sprintbaton-sandbox")),
            host=os.environ.get("SPRINTBATON_SANDBOX_HOST", "0.0.0.0"),
            port=_int("SPRINTBATON_SANDBOX_PORT", 8090),
            broker_address=os.environ.get("SPRINTBATON_SANDBOX_BROKER_ADDRESS", ""),
            launcher=os.environ.get("SPRINTBATON_SANDBOX_LAUNCHER", "bwrap"),
            bwrap=os.environ.get("SPRINTBATON_SANDBOX_BWRAP", "bwrap"),
            seccomp_path=os.environ.get("SPRINTBATON_SANDBOX_SECCOMP", ""),
            ro_binds=tuple(b for b in binds.split(",") if b) if binds else DEFAULT_RO_BINDS,
            max_sessions=_int("SPRINTBATON_SANDBOX_MAX_SESSIONS", 4),
            session_ttl_seconds=_int("SPRINTBATON_SANDBOX_SESSION_TTL_SECONDS", 86400),
            max_changeset_bytes=_int("SPRINTBATON_SANDBOX_MAX_CHANGESET_BYTES",
                                     100 * 1024 * 1024),
            tls_cert=os.environ.get("SPRINTBATON_SANDBOX_TLS_CERT", ""),
            tls_key=os.environ.get("SPRINTBATON_SANDBOX_TLS_KEY", ""),
        )

"""Selecting the sandbox implementation (hosted-sandbox-isolation spec §5.2).

`SPRINTBATON_SANDBOX_BACKEND` = `none | remote`, derived from
`SPRINTBATON_MODE` like every other backend (`tool` -> `none`, `hosted` ->
`remote`). Hosted mode fails closed: `none`, a missing URL/token, or an
unreachable service all yield a sandbox that refuses every run, and
`sprintbaton serve` refuses to start on one (invariant 8).
"""

from __future__ import annotations

from sprintbaton.config.settings import Settings
from sprintbaton.sandbox.base import SandboxUnavailableError
from sprintbaton.sandbox.binding import SandboxRuntime
from sprintbaton.sandbox.broker import DEFAULT_EGRESS_ALLOWLIST, EgressBroker
from sprintbaton.sandbox.local import LOCAL_SANDBOX
from sprintbaton.sandbox.remote import RemoteSandbox, UnavailableSandbox

SANDBOX_BACKENDS = ("none", "remote")


def egress_allowlist(settings: Settings) -> tuple[str, ...]:
    raw = settings.sprintbaton_sandbox_egress_allowlist.strip()
    if not raw:
        return DEFAULT_EGRESS_ALLOWLIST
    return tuple(h.strip() for h in raw.split(",") if h.strip())


def build_sandbox_runtime(settings: Settings) -> SandboxRuntime:
    backend = settings.backend_for("sandbox")
    if backend not in SANDBOX_BACKENDS:
        raise ValueError(f"unknown SPRINTBATON_SANDBOX_BACKEND {backend!r} "
                         f"(expected one of {SANDBOX_BACKENDS})")
    limits = dict(git_depth=settings.sprintbaton_sandbox_git_depth,
                  max_changeset_bytes=settings.sprintbaton_sandbox_max_changeset_bytes)
    if backend == "none":
        if settings.sprintbaton_mode == "hosted":
            return SandboxRuntime(sandbox=UnavailableSandbox(
                "SPRINTBATON_SANDBOX_BACKEND=none in hosted mode: tenant code "
                "would run inside the worker. Hosted mode requires the sandbox "
                "service (SPRINTBATON_SANDBOX_BACKEND=remote)."), **limits)
        return SandboxRuntime(sandbox=LOCAL_SANDBOX, **limits)
    broker = EgressBroker(port=settings.sprintbaton_sandbox_broker_port,
                          allowlist=egress_allowlist(settings))
    try:
        sandbox = RemoteSandbox(settings.sprintbaton_sandbox_url,
                                settings.sprintbaton_sandbox_token)
    except SandboxUnavailableError as e:
        return SandboxRuntime(sandbox=UnavailableSandbox(str(e)), broker=broker, **limits)
    return SandboxRuntime(sandbox=sandbox, broker=broker, **limits)

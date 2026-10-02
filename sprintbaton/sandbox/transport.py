"""SandboxTransport — the `claude` binary in the sandbox, everything else in the
worker (hosted-sandbox-isolation spec §7.2).

The Claude Agent SDK accepts a custom `Transport` (`query(..., transport=...)`).
This one reuses the SDK's own command-line construction
(`SubprocessCLITransport._build_command`, so flags can never drift from the
SDK's) and replaces only *where the process runs*: `session.spawn`, with a
complete explicit environment, its stdio piped back over the session API.

What stays in the worker is exactly what must: the SDK, the PreToolUse/
PostToolUse hooks (Python callbacks over the control protocol — so the guard
is still enforced here), the in-process MCP `finish` tool, and message parsing.

The binary in the sandbox image is copied out of the same wheel the worker's
SDK came from (spec §10.1); the first exchange of every run checks the two
agree, and a mismatch fails the run loudly rather than risking a control-
protocol skew that would surface as an opaque parse error mid-task.
"""

from __future__ import annotations

import codecs
import logging
import re
from collections.abc import AsyncIterator
from typing import Any

import anyio
from claude_agent_sdk import ClaudeAgentOptions
from claude_agent_sdk._errors import CLIConnectionError, ProcessError
from claude_agent_sdk._internal.transport.subprocess_cli import (
    SubprocessCLITransport,
    _LineFramer,
    _parse_stdout_line,
)

from sprintbaton.sandbox.base import EgressGrant, RunProcess, RunSpec, SandboxError

log = logging.getLogger(__name__)

# Upper bound on one `claude` process (a role's whole run). The per-tier wall
# clock is enforced by the orchestrator; this only guarantees teardown.
PROCESS_TIMEOUT_SECONDS = 3 * 3600
VERSION_TIMEOUT_SECONDS = 30
_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+)")


def expected_cli_version() -> str:
    """The Claude Code version bundled in the worker's SDK wheel."""
    try:
        from claude_agent_sdk._cli_version import __cli_version__
    except ImportError:  # pragma: no cover - every supported SDK has it
        return ""
    return __cli_version__


class SandboxTransport(SubprocessCLITransport):
    def __init__(self, prompt: Any, options: ClaudeAgentOptions, *, session,
                 env: dict[str, str], egress: EgressGrant | None,
                 cli_path: str = "claude", expected_version: str | None = None,
                 on_close=None):
        super().__init__(prompt, options)
        self._cli_path = cli_path
        self._session = session
        self._run_env = dict(env)
        self._egress = egress
        self._expected_version = (expected_cli_version() if expected_version is None
                                  else expected_version)
        self._run: RunProcess | None = None
        self._on_close = on_close

    async def connect(self) -> None:
        if self._run is not None:
            return
        await anyio.to_thread.run_sync(self._check_version)
        cmd = self._build_command()
        spec = RunSpec(argv=cmd, cwd=self._cwd or "/", env=self._run_env,
                       timeout_seconds=PROCESS_TIMEOUT_SECONDS, egress=self._egress)
        try:
            self._run = await anyio.to_thread.run_sync(self._session.spawn, spec)
        except Exception as e:
            self._exit_error = CLIConnectionError(
                f"Failed to start Claude Code in the sandbox: {e}")
            raise self._exit_error from e
        self._ready = True

    def _check_version(self) -> None:
        if not self._expected_version:
            return
        result = self._session.run(RunSpec(
            argv=[self._cli_path, "--version"], cwd="/", env=self._run_env,
            timeout_seconds=VERSION_TIMEOUT_SECONDS))
        match = _VERSION_RE.search(result.stdout or "")
        found = match.group(1) if match else ""
        if found != self._expected_version:
            raise SandboxError(
                f"sandbox `claude` binary version {found or '(unknown)'!r} does not "
                f"match the worker's claude-agent-sdk bundle "
                f"{self._expected_version!r}. Build the worker and sandbox images "
                f"from one lockfile (hosted-sandbox-isolation spec §7.2).")

    async def write(self, data: str) -> None:
        async with self._write_lock:
            if not self._ready or self._run is None:
                raise CLIConnectionError("SandboxTransport is not ready for writing")
            try:
                await anyio.to_thread.run_sync(self._run.write_stdin, data.encode())
            except Exception as e:
                self._ready = False
                self._exit_error = CLIConnectionError(
                    f"Failed to write to the sandboxed process: {e}")
                raise self._exit_error from e

    async def end_input(self) -> None:
        async with self._write_lock:
            if self._run is not None:
                await anyio.to_thread.run_sync(self._run.close_stdin)

    def read_messages(self) -> AsyncIterator[dict[str, Any]]:
        return self._read()

    async def _read(self) -> AsyncIterator[dict[str, Any]]:
        if self._run is None:
            raise CLIConnectionError("Not connected")
        run = self._run
        framer = _LineFramer()
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        while True:
            chunk = await anyio.to_thread.run_sync(run.read_stdout)
            if not chunk:
                break
            for line in framer.push(decoder.decode(chunk)):
                if len(line) > self._max_buffer_size:
                    raise CLIConnectionError("message exceeded the maximum buffer size")
                data = _parse_stdout_line(line)
                if data is not None:
                    yield data
        tail = framer.flush() + decoder.decode(b"", final=True)
        try:
            data = _parse_stdout_line(tail)
        except Exception:
            data = None
        if data is not None:
            yield data
        code = await anyio.to_thread.run_sync(run.wait)
        if code != 0:
            self._exit_error = ProcessError(
                f"Command failed with exit code {code}", exit_code=code,
                stderr=run.stderr_text()[-4000:])
            raise self._exit_error

    async def close(self) -> None:
        with anyio.CancelScope(shield=True):
            self._ready = False
            run, self._run = self._run, None
            if run is not None:
                with anyio.move_on_after(10):
                    await anyio.to_thread.run_sync(run.close_stdin)
                try:
                    with anyio.fail_after(10):
                        await anyio.to_thread.run_sync(run.wait, 5)
                except Exception:
                    run.kill()
            if self._on_close is not None:
                self._on_close()
                self._on_close = None

    def is_ready(self) -> bool:
        return self._ready

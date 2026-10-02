"""The single_shot harness — one AnthropicModelProvider.complete call.

The default runtime for the non-coding task action agents (classification,
finalization, planning, review): it forwards the rendered prompt (plus an
optional structured-output schema) to the model once and returns the raw text
as HarnessResult.output_text. No workspace, no tools, no guardrails — those
spec fields are ignored by design.

Verbose mode (cli-logging spec §6.3): the one call per run has no turn
boundary to hook, so this is the one harness that streams at token
granularity. The *logged* record stays line-granular (deltas buffered, flushed
per line to the trace logger) so hosted log volume never scales with token
count; a tool-mode interactive run instead echoes raw characters straight to
the console (§8.2).
"""

import logging
from typing import Callable

from sprintbaton.harness.base import HarnessResult, HarnessTaskSpec, ModelSpec
from sprintbaton.models.provider import AnthropicModelProvider
from sprintbaton.observer import console
from sprintbaton.observer.verbosity import TRACE_LOGGER_NAME, Verbosity, get_verbosity

log = logging.getLogger(__name__)
trace_log = logging.getLogger(TRACE_LOGGER_NAME)


class _LineBuffer:
    """Buffers token deltas and flushes whole lines to the trace logger."""

    def __init__(self):
        self._pending = ""

    def push(self, delta: str) -> None:
        self._pending += delta
        while "\n" in self._pending:
            line, self._pending = self._pending.split("\n", 1)
            if line:
                trace_log.debug(line, extra={"trace_kind": "text"})

    def flush(self) -> None:
        if self._pending:
            trace_log.debug(self._pending, extra={"trace_kind": "text"})
            self._pending = ""


def _delta_callbacks():
    """(on_text_delta, finish) — both None unless verbose. Console installed:
    raw per-character live echo bypassing logging entirely; otherwise the
    line-buffered trace-record path (works for hosted JSON and tool --json)."""
    if get_verbosity() != Verbosity.VERBOSE:
        return None, None
    if console.is_installed():
        return console.live_echo, console.end_live_echo
    buffer = _LineBuffer()
    return buffer.push, buffer.flush


class SingleShotHarness:
    name = "single_shot"
    # No tools, so nothing crosses the sandbox seam: the one model call is
    # made in the worker (hosted-sandbox-isolation spec §7).
    sandbox_mode = "none"

    def __init__(self, provider_factory: Callable[[str], AnthropicModelProvider]
                 = AnthropicModelProvider):
        # Credential-agnostic singleton (per-user-provider-credentials spec
        # §4.6/§4.7): the key rides the per-call ModelSpec, so the client is
        # built per execute() from model.api_key ("" falls back to ambient
        # credentials). The factory parameter exists for tests.
        self._provider_factory = provider_factory

    def execute(self, spec: HarnessTaskSpec, model: ModelSpec) -> HarnessResult:
        if model.provider != "anthropic":
            raise ValueError(
                f"single_shot only supports provider 'anthropic', got {model.provider!r}"
            )
        provider = self._provider_factory(model.api_key)
        on_text_delta, finish_stream = _delta_callbacks()
        response = provider.complete(
            model=model.model_id,
            system=spec.system_prompt,
            messages=[{"role": "user", "content": spec.user_message}],
            output_schema=spec.output_schema,
            adaptive_thinking=spec.adaptive_thinking,
            max_tokens=spec.max_tokens,
            on_text_delta=on_text_delta,
        )
        if finish_stream is not None:
            finish_stream()
        text = provider.first_text(response)
        return HarnessResult(
            summary=text[:200],
            completed=True,
            usage=provider.usage_of(response),
            output_text=text,
        )

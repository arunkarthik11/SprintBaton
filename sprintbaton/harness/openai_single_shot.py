"""The openai_single_shot harness — one OpenAI Responses API call, the OpenAI
sibling of `single_shot` (multi-provider-parity spec §4.2).

Mirrors `harness/single_shot.py` exactly, one client swap: it forwards the
rendered prompt (plus an optional structured-output schema) to the model once
and returns the raw text as HarnessResult.output_text. No workspace, no tools,
no guardrails — the same "single categorical decision from text fully provided
in the prompt" role the three classification-shaped actions run in, where tool
access buys nothing.

Depends on the thin `openai` client (pip extra "openai"), NOT the full
`openai-agents` framework `openai_agent_sdk` uses — the same lighter-weight
choice `single_shot` makes depending on bare `anthropic` rather than
claude-agent-sdk (§4.2). The import is lazy (inside execute()) because `openai`
is an OPTIONAL dependency: importing it at module-load time would break
HarnessRegistry construction for every base-install user (§6).

API surface verified against the OpenAI Python SDK Responses API (docs, 2026-07):
`client.responses.create(model, instructions, input, text={"format": ...})`
returns an object with `.output_text` and `.usage`
(`input_tokens`/`output_tokens`, `.input_tokens_details.cached_tokens`).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

from sprintbaton.entities.usage import TokenUsage
from sprintbaton.harness.base import HarnessResult, HarnessTaskSpec, ModelSpec
from sprintbaton.observer.verbosity import TRACE_CONSOLE_CHARS, TRACE_LOGGER_NAME
from sprintbaton.dependencies import require_module

log = logging.getLogger(__name__)
trace_log = logging.getLogger(TRACE_LOGGER_NAME)


def _default_client_factory(api_key: str) -> Any:
    # Lazy import: `openai` is an optional extra (§6). "" api_key falls back to
    # the OPENAI_API_KEY env var the client reads ambiently.
    require_module("openai", package="openai", extra="openai",
                   harness="openai_single_shot")
    from openai import OpenAI

    return OpenAI(api_key=api_key) if api_key else OpenAI()


def _usage_of(response: Any) -> TokenUsage:
    """Reduce an OpenAI Responses `usage` object to TokenUsage — best-effort
    over the documented field names, defaulting to zero so a shape drift never
    raises into the run."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return TokenUsage()
    cached = 0
    details = getattr(usage, "input_tokens_details", None)
    if details is not None:
        cached = int(getattr(details, "cached_tokens", 0) or 0)
    return TokenUsage(
        inputTokens=int(getattr(usage, "input_tokens", 0) or 0),
        outputTokens=int(getattr(usage, "output_tokens", 0) or 0),
        cacheReadInputTokens=cached,
    )


class OpenAiSingleShotHarness:
    name = "openai_single_shot"
    # No tools, so nothing crosses the sandbox seam: the one model call is
    # made in the worker (hosted-sandbox-isolation spec §7).
    sandbox_mode = "none"
    provider = "openai"

    def __init__(self, client_factory: Callable[[str], Any] = _default_client_factory):
        # Credential-agnostic singleton, same as SingleShotHarness: the key
        # rides the per-call ModelSpec.api_key, so the client is built per
        # execute(). The factory parameter is the test seam (a fake client with
        # a .responses.create) so unit tests need no installed SDK (§8).
        self._client_factory = client_factory

    def execute(self, spec: HarnessTaskSpec, model: ModelSpec) -> HarnessResult:
        if model.provider != "openai":
            raise ValueError(
                f"openai_single_shot only supports provider 'openai', "
                f"got {model.provider!r}")
        client = self._client_factory(model.api_key)
        kwargs: dict[str, Any] = {
            "model": model.model_id,
            "instructions": spec.system_prompt,
            "input": spec.user_message,
            "max_output_tokens": spec.max_tokens,
        }
        if spec.output_schema:
            # Responses API structured output: a strict json_schema the final
            # text must satisfy (the OpenAI analogue of Anthropic's
            # output_config json_schema).
            kwargs["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": "result",
                    "schema": spec.output_schema,
                    "strict": False,
                }
            }
        response = client.responses.create(**kwargs)
        text = _output_text(response)
        if trace_log.isEnabledFor(logging.DEBUG) and text:
            trace_log.debug(text[:TRACE_CONSOLE_CHARS], extra={"trace_kind": "text"})
        return HarnessResult(
            summary=text[:200],
            completed=True,
            usage=_usage_of(response),
            output_text=text,
        )


def _output_text(response: Any) -> str:
    """The aggregated text of a Responses result. The SDK exposes `.output_text`
    as a convenience; fall back to walking `.output` blocks if a fake/older
    shape lacks it."""
    text = getattr(response, "output_text", None)
    if text:
        return str(text)
    parts: list[str] = []
    for item in getattr(response, "output", []) or []:
        for block in getattr(item, "content", []) or []:
            chunk = getattr(block, "text", None)
            if chunk:
                parts.append(str(chunk))
    if parts:
        return "".join(parts)
    # Last resort: a raw dict-shaped fake.
    if isinstance(response, dict):
        return str(response.get("output_text", "")) or json.dumps(response)
    return ""

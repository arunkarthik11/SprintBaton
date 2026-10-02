"""The gemini_single_shot harness — one google-genai generate_content call, the
Google sibling of `single_shot` (multi-provider-parity spec §4.2).

Mirrors `harness/single_shot.py` exactly, one client swap. Depends on the thin
`google-genai` client (pip extra "google-genai"), NOT the full `google-adk`
framework `gemini_agent_sdk` uses — the lighter-weight opt-in path (§4.2). The
import is lazy (inside execute()) because `google-genai` is an OPTIONAL
dependency (§6).

API surface verified against the google-genai Python SDK (docs, 2026-07):
`genai.Client(api_key=...).models.generate_content(model, contents, config)`
returns an object with `.text` and `.usage_metadata`
(`prompt_token_count`/`candidates_token_count`/`cached_content_token_count`).
Structured output is requested via `config.response_mime_type` +
`config.response_json_schema`.
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
    # Lazy import: `google-genai` is an optional extra (§6). "" api_key falls
    # back to GEMINI_API_KEY / GOOGLE_API_KEY the client reads ambiently.
    require_module("google.genai", package="google-genai", extra="google-genai",
                   harness="gemini_single_shot")
    from google import genai

    return genai.Client(api_key=api_key) if api_key else genai.Client()


def _usage_of(response: Any) -> TokenUsage:
    """Reduce a google-genai `usage_metadata` to TokenUsage — best-effort over
    the documented field names, defaulting to zero so a shape drift never
    raises into the run."""
    usage = getattr(response, "usage_metadata", None)
    if usage is None:
        return TokenUsage()
    return TokenUsage(
        inputTokens=int(getattr(usage, "prompt_token_count", 0) or 0),
        outputTokens=int(getattr(usage, "candidates_token_count", 0) or 0),
        cacheReadInputTokens=int(getattr(usage, "cached_content_token_count", 0) or 0),
    )


class GeminiSingleShotHarness:
    name = "gemini_single_shot"
    # No tools, so nothing crosses the sandbox seam: the one model call is
    # made in the worker (hosted-sandbox-isolation spec §7).
    sandbox_mode = "none"
    provider = "google"

    def __init__(self, client_factory: Callable[[str], Any] = _default_client_factory):
        # Credential-agnostic singleton; the key rides ModelSpec.api_key. The
        # factory is the test seam (a fake client with a
        # .models.generate_content) so unit tests need no installed SDK (§8).
        self._client_factory = client_factory

    def execute(self, spec: HarnessTaskSpec, model: ModelSpec) -> HarnessResult:
        if model.provider != "google":
            raise ValueError(
                f"gemini_single_shot only supports provider 'google', "
                f"got {model.provider!r}")
        client = self._client_factory(model.api_key)
        # config is a plain dict — google-genai accepts a GenerateContentConfig
        # OR its dict form, so we never import the config type.
        config: dict[str, Any] = {
            "system_instruction": spec.system_prompt,
            "max_output_tokens": spec.max_tokens,
        }
        if spec.output_schema:
            config["response_mime_type"] = "application/json"
            config["response_json_schema"] = spec.output_schema
        response = client.models.generate_content(
            model=model.model_id,
            contents=spec.user_message,
            config=config,
        )
        text = _response_text(response)
        if trace_log.isEnabledFor(logging.DEBUG) and text:
            trace_log.debug(text[:TRACE_CONSOLE_CHARS], extra={"trace_kind": "text"})
        return HarnessResult(
            summary=text[:200],
            completed=True,
            usage=_usage_of(response),
            output_text=text,
        )


def _response_text(response: Any) -> str:
    """The aggregated text of a generate_content result. `.text` is the SDK's
    convenience accessor; fall back to walking candidates for a fake/older
    shape."""
    text = getattr(response, "text", None)
    if text:
        return str(text)
    for candidate in getattr(response, "candidates", []) or []:
        content = getattr(candidate, "content", None)
        for part in getattr(content, "parts", []) or []:
            chunk = getattr(part, "text", None)
            if chunk:
                return str(chunk)
    if isinstance(response, dict):
        return str(response.get("text", "")) or json.dumps(response)
    return ""

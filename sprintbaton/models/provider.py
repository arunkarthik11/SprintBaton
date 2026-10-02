"""ModelProvider — encapsulates Anthropic API execution logic.

All model calls in SprintBaton go through this class so the Observer can track
per-model usage and so tier selection stays a parameter of the caller.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Callable

from sprintbaton.dependencies import require_module
from sprintbaton.entities.usage import TokenUsage, usage_from_mapping

if TYPE_CHECKING:
    import anthropic

log = logging.getLogger(__name__)

DEFAULT_MAX_TOKENS = 16000


class AnthropicModelProvider:
    def __init__(self, api_key: str = "", base_url: str = ""):
        # Falls back to ANTHROPIC_API_KEY / ambient credentials when empty.
        # base_url is Provider.baseUrl (hosted-sandbox-isolation spec §9.3) —
        # empty means Anthropic's own endpoint.
        kwargs: dict[str, Any] = {}
        if api_key:
            kwargs["api_key"] = api_key
        if base_url:
            kwargs["base_url"] = base_url
        # Lazy (pluggable-hosted-backends spec §4.8): `anthropic` is the
        # `anthropic` extra, not a base dependency.
        sdk = require_module("anthropic", package="anthropic", extra="anthropic",
                             harness="single_shot/raw_tool_loop")
        self._client = sdk.Anthropic(**kwargs)

    def complete(
        self,
        *,
        model: str,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        output_schema: dict[str, Any] | None = None,
        adaptive_thinking: bool = False,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        on_text_delta: Callable[[str], None] | None = None,
    ) -> anthropic.types.Message:
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools
        if output_schema:
            kwargs["output_config"] = {
                "format": {"type": "json_schema", "schema": output_schema}
            }
        if adaptive_thinking:
            kwargs["thinking"] = {"type": "adaptive"}

        # on_text_delta receives text as it is generated (cli-logging spec
        # §6.3) — the returned Message is unchanged, so callers that only
        # want the final result are unaffected.
        if on_text_delta is not None or max_tokens > DEFAULT_MAX_TOKENS:
            with self._client.messages.stream(**kwargs) as stream:
                if on_text_delta is not None:
                    for text in stream.text_stream:
                        on_text_delta(text)
                return stream.get_final_message()
        return self._client.messages.create(**kwargs)

    @staticmethod
    def usage_of(response: anthropic.types.Message) -> TokenUsage:
        """Reduce an API response to a TokenUsage, through the provider-
        neutral reducer every Anthropic-shaped usage record shares."""
        return usage_from_mapping(response.usage)

    @staticmethod
    def first_text(response: anthropic.types.Message) -> str:
        return next((b.text for b in response.content if b.type == "text"), "")

    @classmethod
    def parse_json(cls, response: anthropic.types.Message) -> dict[str, Any]:
        return json.loads(cls.first_text(response))

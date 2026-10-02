"""TokenUsage — the unit of the token computation infrastructure.

Every model call is reduced to a TokenUsage (see usage_from_mapping below —
provider-neutral, so importing the core entities never imports a provider SDK);
usages are summed across tool-loop iterations and attached to TaskActionResponses
and TaskActionEvents so per-state consumption can be analysed long-term.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class TokenUsage(BaseModel):
    inputTokens: int = 0                 # non-cached input tokens
    outputTokens: int = 0
    cacheCreationInputTokens: int = 0
    cacheReadInputTokens: int = 0

    @property
    def totalInputTokens(self) -> int:
        return self.inputTokens + self.cacheCreationInputTokens + self.cacheReadInputTokens

    @property
    def totalTokens(self) -> int:
        return self.totalInputTokens + self.outputTokens

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            inputTokens=self.inputTokens + other.inputTokens,
            outputTokens=self.outputTokens + other.outputTokens,
            cacheCreationInputTokens=(self.cacheCreationInputTokens
                                      + other.cacheCreationInputTokens),
            cacheReadInputTokens=self.cacheReadInputTokens + other.cacheReadInputTokens,
        )


_USAGE_FIELDS = {
    "inputTokens": "input_tokens",
    "outputTokens": "output_tokens",
    "cacheCreationInputTokens": "cache_creation_input_tokens",
    "cacheReadInputTokens": "cache_read_input_tokens",
}


def usage_from_mapping(usage: Any) -> TokenUsage:
    """Reduce an Anthropic-shaped usage record — a dict (the claude CLI's
    result JSON, the Agent SDK's ResultMessage.usage) or an object with the
    same attribute names (the Messages API's response.usage) — to a
    TokenUsage. The single place token accounting reads that wire format
    (pluggable-hosted-backends spec §4.8); missing or null fields count 0."""
    if usage is None:
        return TokenUsage()
    get = usage.get if isinstance(usage, dict) else (
        lambda name, default=None: getattr(usage, name, default))
    return TokenUsage(**{field: int(get(wire, 0) or 0)
                         for field, wire in _USAGE_FIELDS.items()})

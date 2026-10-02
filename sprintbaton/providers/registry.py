"""BUILTIN_PROVIDERS — the static, code-owned provider table (provider-
registration spec §4.1, §8.2).

`anthropic` is the zero-registration built-in (data, not an import — its SDKs
are the `anthropic`/`claude-agent-sdk` extras, pluggable-hosted-backends spec
§4.8); `openai`/`google` are code-known but
only usable once a user runs `sprintbaton providers add` (which checks the CLI
binary, captures a credential, and persists a row). `open_hands_llm` is here to
retroactively document the informal optional dependency `harness/open_hands.py`
has always lazy-imported (spec §7.1) — a housekeeping entry, not a behavior
change.

`known_names(user_id)` (ProviderService) is the union of these keys and that
user's persisted `Provider.name`s — never a database round-trip for a built-in.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sprintbaton.entities.enums import CredentialProvider
from sprintbaton.entities.provider import HarnessDependency


@dataclass(frozen=True)
class BuiltinProvider:
    name: str
    providerType: str                 # litellm/harness routing string a harness guards on
    harnessNames: tuple[str, ...] = ()
    credentialProvider: CredentialProvider | None = None
    cliBinary: str | None = None      # external binary checked (never installed) via shutil.which
    # Per-harness pip footprint (multi-provider-parity spec §4.3): one provider
    # now backs harnesses with different (or zero) dependency footprints, so the
    # singular provider-level pipExtra/importCheckModule became this map, keyed
    # by harness name. A harness absent from the map needs nothing installed.
    harnessDependencies: dict[str, HarnessDependency] = field(default_factory=dict)
    # A sensible default model id `providers add` seeds its starter
    # AgentDefinition with (spec §8.2 step 6) — a placeholder the user tunes.
    defaultModelId: str = ""
    description: str = ""


BUILTIN_PROVIDERS: dict[str, BuiltinProvider] = {
    "anthropic": BuiltinProvider(
        name="anthropic",
        providerType="anthropic",
        harnessNames=("single_shot", "raw_tool_loop", "claude_agent_sdk",
                      "claude_code_cli"),
        credentialProvider=CredentialProvider.ANTHROPIC,
        harnessDependencies={
            "single_shot": HarnessDependency(
                pipExtra="anthropic", importCheckModule="anthropic"),
            "raw_tool_loop": HarnessDependency(
                pipExtra="anthropic", importCheckModule="anthropic"),
            "claude_agent_sdk": HarnessDependency(
                pipExtra="claude-agent-sdk", importCheckModule="claude_agent_sdk"),
            "claude_code_cli": HarnessDependency(),  # pure subprocess wrapper
        },
        defaultModelId="claude-sonnet-5",
        description="Anthropic Claude — the zero-registration built-in.",
    ),
    "openai": BuiltinProvider(
        name="openai",
        providerType="openai",
        # codex_cli (advisory subprocess) + the two in-process harnesses this
        # spec adds: openai_single_shot (classification) and openai_agent_sdk
        # (full read/write parity) — multi-provider-parity spec §4.1/§4.2.
        harnessNames=("codex_cli", "openai_single_shot", "openai_agent_sdk"),
        credentialProvider=CredentialProvider.OPENAI,
        cliBinary="codex",
        harnessDependencies={
            "codex_cli": HarnessDependency(),  # pure subprocess wrapper (§6.1)
            "openai_single_shot": HarnessDependency(
                pipExtra="openai", importCheckModule="openai"),
            "openai_agent_sdk": HarnessDependency(
                pipExtra="openai-agents", importCheckModule="agents"),
        },
        defaultModelId="gpt-5-codex",
        description="OpenAI Codex/Agents SDK — codex_cli advisory-only; "
                    "openai_agent_sdk full parity.",
    ),
    "google": BuiltinProvider(
        name="google",
        providerType="google",
        harnessNames=("gemini_cli", "gemini_single_shot", "gemini_agent_sdk"),
        credentialProvider=CredentialProvider.GEMINI,
        cliBinary="gemini",
        harnessDependencies={
            "gemini_cli": HarnessDependency(),  # pure subprocess wrapper (§6.1)
            "gemini_single_shot": HarnessDependency(
                pipExtra="google-genai", importCheckModule="google.genai"),
            "gemini_agent_sdk": HarnessDependency(
                pipExtra="google-adk", importCheckModule="google.adk"),
        },
        defaultModelId="gemini-3-pro",
        description="Google Gemini CLI/ADK — gemini_cli advisory-only; "
                    "gemini_agent_sdk full parity.",
    ),
    "open_hands_llm": BuiltinProvider(
        name="open_hands_llm",
        providerType="open_hands_llm",
        harnessNames=("open_hands",),
        credentialProvider=CredentialProvider.OPEN_HANDS_LLM,
        harnessDependencies={
            "open_hands": HarnessDependency(
                pipExtra="openhands", importCheckModule="openhands"),
        },
        description="OpenHands runtime — the pre-existing lazy-import gap, now "
                    "a declared extra (spec §7.1).",
    ),
}


def builtin(name: str) -> BuiltinProvider | None:
    return BUILTIN_PROVIDERS.get(name)

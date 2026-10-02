"""The gemini_cli harness — a subprocess wrapper around the Google Gemini CLI
(`gemini`), architecturally identical to claude_code_cli (provider-registration
spec §6).

Assumed already installed and authenticated with `gemini auth login`. That login
is the *only* auth path: SprintBaton never forwards an API key to this harness
and strips GEMINI_API_KEY/GOOGLE_API_KEY from the subprocess environment, so an
ambient key can never silently decide the billing account of a run the user set
up as a login (cli-subscription-auth-parity spec §4.3). Pure subprocess/stdlib,
no pip extra.

Write-capable for execution/conflict_resolution via Gemini's own native hooks
(subprocess-cli-write-parity spec §4); not conformance-passed, and tool-mode
only: the login session lives on this machine's disk, and the CLI runs its
tools on the host, so it cannot be sandboxed (hosted-sandbox-isolation §7.3).

Flags are best-effort against the Gemini CLI's documented non-interactive mode
(`gemini -p`) and left as the single flagged verification point (spec §6.2,
§16).
"""

from __future__ import annotations

from sprintbaton.harness.base import HarnessTaskSpec, ModelSpec
from sprintbaton.harness.subprocess_cli import (
    DEFAULT_TIMEOUT_SECONDS,
    GEMINI_DIALECT,
    SubprocessCliHarness,
)

# Documented for headless CI: trusts the current workspace for the session
# (subprocess-cli-write-parity spec §2.3). This asserts trust over a clone
# SprintBaton itself created and populated — it overrides no user judgment
# about third-party content, which is why it is used freely where Codex's
# blanket hook-trust bypass is refused outright.
TRUST_WORKSPACE_VAR = "GEMINI_CLI_TRUST_WORKSPACE"


class GeminiCliHarness(SubprocessCliHarness):
    name = "gemini_cli"
    # Login-only, same as codex_cli (cli-subscription-auth-parity spec §4.1).
    supports_subscription_auth = True
    supports_metered_auth = False
    # "" — the session lives under ~/.gemini/, not an env var (§4.2).
    subscription_token_var = ""
    provider = "google"
    api_key_env_var = "GEMINI_API_KEY"
    # The Gemini CLI honors GOOGLE_API_KEY too, so stripping one is not enough.
    extra_key_env_vars = ("GOOGLE_API_KEY",)
    # BeforeTool matches any built-in tool by regex and its deny decision is
    # documented to prevent execution, so guardrails are genuinely enforced —
    # no backstop and no capability probe needed (spec §4.1).
    hook_dialect = GEMINI_DIALECT
    supports_write_execution = True
    # As for codex_cli: writable_paths is honored via the shared hook path.
    # Gemini enforces write denies and needs no capability probe, so the
    # in-place metadata edit is guarded here without either caveat.
    supports_writable_paths = True

    def __init__(self, binary: str = "gemini", *,
                 timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
                 scratch_root: str = ""):
        super().__init__(binary, timeout_seconds=timeout_seconds,
                         scratch_root=scratch_root)

    def _env(self, model: ModelSpec, spec: HarnessTaskSpec | None = None,
             **kwargs) -> dict[str, str]:
        env = super()._env(model, spec, **kwargs)
        env[TRUST_WORKSPACE_VAR] = "true"
        return env

    def _command(self, spec: HarnessTaskSpec, model: ModelSpec) -> list[str]:
        return [self._binary, "--model", model.model_id, "-p", "-"]

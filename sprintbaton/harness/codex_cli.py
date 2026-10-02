"""The codex_cli harness — a subprocess wrapper around the OpenAI Codex CLI
(`codex`), architecturally identical to claude_code_cli (provider-registration
spec §6).

Assumed already installed and authenticated with `codex login`. That login is
the *only* auth path: SprintBaton never forwards an API key to this harness and
strips OPENAI_API_KEY from the subprocess environment, so an ambient key can
never silently decide the billing account of a run the user set up as a login
(cli-subscription-auth-parity spec §4.3). Pure subprocess/stdlib, no in-process
SDK, so it needs no pip extra.

Write-capable for execution/conflict_resolution via Codex's own native hooks
(subprocess-cli-write-parity spec §4); not conformance-passed, and tool-mode
only: the login session lives on this machine's disk, and the CLI runs its
tools on the host, so it cannot be sandboxed (hosted-sandbox-isolation §7.3).

Flags are best-effort against the Codex CLI's documented non-interactive mode
(`codex exec`) and left as the single flagged verification point (spec §6.2,
§16) — the architecture is what this file commits to, not the exact flag
strings.
"""

from __future__ import annotations

from sprintbaton.harness.base import HarnessTaskSpec, ModelSpec
from sprintbaton.harness.subprocess_cli import (
    CODEX_DIALECT,
    DEFAULT_TIMEOUT_SECONDS,
    SubprocessCliHarness,
)


class CodexCliHarness(SubprocessCliHarness):
    name = "codex_cli"
    # A `codex login` session is the only auth path (cli-subscription-auth-
    # parity spec §4.1). supports_metered_auth = False is what makes a
    # never-satisfiable owner fail at AgentDefinition *creation* rather than
    # once per task, exactly as it already does for claude_code_cli.
    supports_subscription_auth = True
    supports_metered_auth = False
    # "" — the session lives in ~/.codex/auth.json, not an env var, so it
    # cannot be handed to a hosted pod and is never probed (§4.2).
    subscription_token_var = ""
    provider = "openai"
    api_key_env_var = "OPENAI_API_KEY"
    # Guardrails run through Codex's native PreToolUse/PostToolUse hooks
    # (subprocess-cli-write-parity spec §4). Two known upstream defects are
    # carried explicitly rather than hidden: write denies are not enforced
    # (#27833 — handled by the §5 detect-and-abort backstop), and hooks may not
    # execute at all (#32491 — measured per install by the §7 probe, which
    # warns rather than blocks).
    hook_dialect = CODEX_DIALECT
    supports_write_execution = True
    # Read-only runs honor writable_paths through the same hook subprocess and
    # the same evaluate_hook_read_only(writable_roots=...) call claude_code_cli
    # uses — spec.writable_paths rides in on SPRINTBATON_GUARD_WRITABLE_ROOTS
    # (subprocess_cli._env). No --tools narrowing is needed: `codex exec` is
    # never told to drop its edit tools, so the guard is the sole enforcement
    # point. The project-initialization-task spec §7.1 table originally said
    # False here on the grounds that "guardrails are prompt-advisory only" —
    # true before the native hooks of subprocess-cli-write-parity spec §4, and
    # stale since.
    supports_writable_paths = True

    def __init__(self, binary: str = "codex", *,
                 timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
                 scratch_root: str = ""):
        super().__init__(binary, timeout_seconds=timeout_seconds,
                         scratch_root=scratch_root)

    def _command(self, spec: HarnessTaskSpec, model: ModelSpec) -> list[str]:
        return [self._binary, "exec", "--model", model.model_id]

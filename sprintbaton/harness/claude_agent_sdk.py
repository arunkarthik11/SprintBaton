"""The claude_agent_sdk harness — runs the Coding Model's tool loop on the
Claude Agent SDK (migration spec §8) instead of the hand-rolled loop.

Resolution of the spec's §7/§8 TBDs against claude-agent-sdk 0.2.x:

- Guard hard-stop (EH): a PreToolUse hook filtered to the Bash tool checks the
  shared GuardrailPolicy. On a destructive command it denies the call
  (permissionDecision "deny") AND aborts the whole run (continue_: False),
  records the IrreversibleOperationError, and `run()` re-raises it after the
  stream drains — so the exception propagates exactly the way the raw loop's
  does (unattended hard stop, no retry).
- files_edited: PostToolUse hook on Write|Edit, counting per workspace-relative
  path (the SDK owns file I/O directly).
- consecutive_check_failures: PostToolUse hook on Bash, re-applying the same
  CHECK_COMMAND_MARKERS heuristic; the tool_response shape is not contractual
  across CLI versions, so failure detection is best-effort over the known
  shapes (exit-code fields, is_error flags, error content blocks).
- usage: reduced from ResultMessage.usage to the same TokenUsage shape the
  analytics pipeline expects.
- finish tool: registered via an in-process MCP server with the same input
  schema as the raw loop's custom tool, so the HarnessResult-building code is
  a direct port. AskUserQuestion was evaluated and rejected: it assumes a
  synchronous in-session answer, while SprintBaton's clarification flow is
  asynchronous (task comments) — finish's `question` field is kept instead.
- Conversation resume: ClaudeAgentOptions.resume <- spec.conversation_id;
  HarnessResult.conversation_id <- ResultMessage.session_id. Same-role/tier
  resume only — the orchestrator enforces the tier rule.
- Async/sync boundary: bridged locally with asyncio.run(); async is not
  propagated up through the agent/service/orchestrator layers.
- Workspace confinement: a PreToolUse hook on the file tools rejects
  path-escaping inputs — the SDK's own cwd scoping is not relied on (§10.2).

Read-only mode (execution-tier-agents spec §9.4-§9.5) — the default for every
caller except execution (HarnessTaskSpec.read_only, default True):

- No Edit; Bash gated by the fail-closed guard.check_read_only allowlist
  (recoverable deny — the agent may retry a compliant command; a genuinely
  catastrophic command still hard-stops exactly as in execution mode).
- Write is scoped to exactly one scratch answer file inside a per-run scratch
  directory the task's sandbox session creates (`make_scratch` — a mkdtemp
  under the passthrough). The directory name carries the task id
  (sprintbaton-out-<task_id>-<random>) and is unique per run, so
  concurrent tasks can never collide on or pick up each other's answer file:
  each run only ever reads back the exact path it created.
- The final answer travels via that file, not tool-call-argument schemas: the
  user message is suffixed with instructions to write the answer (JSON matching
  spec.output_schema when set, plain text otherwise) to the scratch file; the
  harness reads it back into HarnessResult.output_text after the run — the same
  shape single_shot returns, so parse_json_output needs no downstream changes.
- No workspace_path -> no filesystem/Bash tools at all (only the scratch Write
  and finish), degrading to a pure tmp-file-answer completion.
- writable_paths (project-initialization-task spec §7.1): when non-empty, Edit
  joins the tool set and Write/Edit are also permitted on targets inside any
  writable root (symlinks followed) — the in-place metadata editing shape. Bash
  stays allowlist-gated and nothing else loosens.
- stop_reason (§7.2): a ResultMessage of subtype `error_max_turns` is a
  `turn_limit` stop, returned normally so the partial on-disk work survives. A
  read-only run that ended in an error with no answer, whose error text looks
  like a model-API outage, raises TransientHarnessError instead (§5.6).

Auth mode — orthogonal to read_only (hosted-sandbox-isolation spec §9.2):

- Metered: the owner's key rides ModelSpec.api_key.
- Subscription: the owner's own `claude setup-token` credential rides
  ModelSpec.auth_token; empty only in tool mode, where the host's `claude
  login` session is the credential.

Where the `claude` binary runs (spec §7.2): under the tool-mode passthrough the
SDK spawns it locally with an env *overlay* (`_auth_env`). Under an isolated
sandbox it runs in the task's sandbox session through `SandboxTransport`, with a
complete explicit environment (`sandbox_run_env`) and — this harness declares
`credential_delivery = "broker"` — only a *placeholder* credential: the worker's
egress broker swaps in the owner's real one on the way to the model API (§8.3).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from typing import TYPE_CHECKING

from sprintbaton.dependencies import require_module
from sprintbaton.entities.usage import TokenUsage, usage_from_mapping
from sprintbaton.harness.base import (
    FINISH_TOOL_DESCRIPTION,
    FINISH_TOOL_NAME,
    FINISH_TOOL_SCHEMA,
    GuardrailPolicy,
    HarnessResult,
    HarnessTaskSpec,
    ModelSpec,
    TransientHarnessError,
    looks_transient,
    fs_safe,
    within_roots,
    workspace_diff,
)
from sprintbaton.harness.raw_tool_loop import CHECK_COMMAND_MARKERS
from sprintbaton.models.guard import IrreversibleOperationError
from sprintbaton.observer.verbosity import TRACE_CONSOLE_CHARS, TRACE_LOGGER_NAME
from sprintbaton.sandbox.base import BRIDGE_URL
from sprintbaton.sandbox.binding import (
    LOCAL_RUNTIME,
    BoundWorkspace,
    SandboxRuntime,
    open_binding,
)
from sprintbaton.sandbox.broker import DEFAULT_ANTHROPIC_UPSTREAM, UpstreamCredential

if TYPE_CHECKING:
    from claude_agent_sdk import ClaudeAgentOptions, HookContext

log = logging.getLogger(__name__)
trace_log = logging.getLogger(TRACE_LOGGER_NAME)

def _sdk():
    """The Claude Agent SDK, imported on first use (pluggable-hosted-backends
    spec §4.8): `claude-agent-sdk` is an optional extra, so this module — and
    every module borrowing its SDK-free helpers — imports without it."""
    return require_module("claude_agent_sdk", package="claude-agent-sdk",
                          extra="claude-agent-sdk", harness="claude_agent_sdk")


def query(*, prompt, options, **kwargs):
    """The SDK's query(), resolved lazily; a module-level name so tests can
    substitute a scripted stream."""
    return _sdk().query(prompt=prompt, options=options, **kwargs)


# The SDK's ResultMessage class, resolved lazily by _result_message_type; a
# module-level test seam (tests substitute a duck-typed stand-in).
ResultMessage: type | None = None


def _result_message_type() -> type:
    return ResultMessage if ResultMessage is not None else _sdk().ResultMessage


MCP_SERVER_NAME = "sprintbaton"
FINISH_MCP_TOOL = f"mcp__{MCP_SERVER_NAME}__{FINISH_TOOL_NAME}"

# Strict superset of raw_tool_loop's three tools (bash/read_file/write_file);
# Edit gives string-replace edits instead of whole-file rewrites (minimal-diff
# convention). git push stays unreachable: guard.py blocks it in the Bash hook
# and no other push-capable tool is exposed.
CORE_TOOLS = ["Read", "Write", "Edit", "Bash", "Glob", "Grep"]
# Read-only mode: no Edit; Bash allowlist-gated; Write scoped to the one
# scratch answer file (execution-tier-agents spec §9.4).
READ_ONLY_TOOLS = ["Read", "Glob", "Grep", "Bash", "Write"]
# Read-only mode with writable_paths (project-initialization-task spec §7.1):
# Edit joins, gated to targets inside a writable root.
WRITABLE_ROOT_TOOLS = ["Edit"]
# The one CLI result subtype that means "cut off by max_turns" (§7.2).
MAX_TURNS_SUBTYPE = "error_max_turns"
FILE_WRITE_MATCHER = "Write|Edit"
FILE_PATH_MATCHER = "Read|Write|Edit|Glob|Grep"
_PATH_INPUT_KEYS = ("file_path", "path", "notebook_path")
_WRITE_TOOLS = frozenset({"Write", "Edit", "NotebookEdit"})


def _bash_check_failed(tool_response: Any) -> bool:
    """Best-effort: did a Bash tool call exit non-zero? The PostToolUse
    tool_response shape is not contractual across CLI versions, so probe the
    known exit-code and error-flag shapes and default to success."""
    if isinstance(tool_response, dict):
        for key in ("exit_code", "exitCode", "returncode", "return_code", "code"):
            if key in tool_response:
                try:
                    return int(tool_response[key]) != 0
                except (TypeError, ValueError):
                    continue
        if tool_response.get("is_error") or tool_response.get("isError"):
            return True
        if tool_response.get("interrupted"):
            return True
        content = tool_response.get("content")
        if content is not None:
            return _bash_check_failed(content)
        return False
    if isinstance(tool_response, list):
        return any(isinstance(block, dict)
                   and (block.get("is_error") or block.get("isError"))
                   for block in tool_response)
    return False


def _is_check_command(command: str) -> bool:
    return any(marker in command for marker in CHECK_COMMAND_MARKERS)


class _RunState:
    """Per-run mutable state shared between the hook callbacks, the finish
    tool, and the HarnessResult assembly. Reconstructs the escalation signals
    the raw loop gets for free by owning tool dispatch (spec §7)."""

    def __init__(self, workspace_root: Path | None, guardrails: GuardrailPolicy,
                 output_path: Path | None = None,
                 writable_roots: tuple[Path, ...] = ()):
        self.root = workspace_root
        self.guardrails = guardrails
        self.output_path = output_path  # the one permitted Write target (read-only mode)
        # Extra Write/Edit roots in read-only mode (project-initialization-task
        # spec §7.1) — empty for every advisory role.
        self.writable_roots = tuple(Path(r).resolve() for r in writable_roots)
        self.finish: dict = {}
        self.file_edit_counts: dict[str, int] = {}
        self.consecutive_check_failures = 0
        self.blocked: IrreversibleOperationError | None = None
        self.result_message: Any = None  # the SDK's ResultMessage

    # --- PreToolUse -----------------------------------------------------

    async def pre_bash(self, hook_input: dict, tool_use_id: str | None,
                       context: HookContext) -> dict:
        command = (hook_input.get("tool_input") or {}).get("command", "")
        hard_stop = self._hard_stop_if_catastrophic(command)
        if hard_stop is not None:
            return hard_stop
        return {}

    def _hard_stop_if_catastrophic(self, command: str) -> dict | None:
        try:
            self.guardrails.check_command(command)
        except IrreversibleOperationError as e:
            self.blocked = e
            # Deny the call AND abort the run: the EH hard-stop must end the
            # task, not become a silently-denied call the loop continues past.
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": str(e),
                },
                "continue_": False,
                "stopReason": f"irreversible operation blocked ({e.reason})",
            }
        return None

    async def pre_bash_read_only(self, hook_input: dict, tool_use_id: str | None,
                                 context: HookContext) -> dict:
        command = (hook_input.get("tool_input") or {}).get("command", "")
        # Read-only mode narrows what's *permitted*, it does not loosen what's
        # *catastrophic*: a guard.check_command match still hard-stops.
        hard_stop = self._hard_stop_if_catastrophic(command)
        if hard_stop is not None:
            return hard_stop
        try:
            self.guardrails.check_read_only(command)
        except IrreversibleOperationError as e:
            # Much lower stakes than a catastrophic attempt: deny this one
            # call and let the agent try a compliant command instead.
            return self._deny(f"read-only run: {e.reason}: {e.command}")
        return {}

    async def pre_file(self, hook_input: dict, tool_use_id: str | None,
                       context: HookContext) -> dict:
        tool_input = hook_input.get("tool_input") or {}
        for key in _PATH_INPUT_KEYS:
            raw = tool_input.get(key)
            if raw and self._escapes_workspace(str(raw)):
                # Recoverable, like the raw loop's ValueError tool error: deny
                # the call but let the run continue.
                return self._deny(f"path escapes the workspace: {raw}")
        return {}

    async def pre_file_read_only(self, hook_input: dict, tool_use_id: str | None,
                                 context: HookContext) -> dict:
        tool_name = hook_input.get("tool_name", "")
        tool_input = hook_input.get("tool_input") or {}
        if tool_name in _WRITE_TOOLS:
            raw = tool_input.get("file_path", "")
            target = self._resolve(str(raw)) if raw else None
            if (target is not None and tool_name in ("Write", "Edit")
                    and self.writable_roots
                    and within_roots(target, self.writable_roots)):
                return {}
            if tool_name != "Write":
                return self._deny(f"read-only run: {tool_name} is not permitted"
                                  + self._writable_hint())
            if target is None or target != self.output_path:
                return self._deny(
                    f"read-only run: writes are only permitted to the answer "
                    f"file {self.output_path}" + self._writable_hint()
                )
            return {}
        # Read/Glob/Grep: the answer file is always readable back; everything
        # else keeps the execution-mode workspace confinement (widened by any
        # writable root, which a run must be able to read back).
        for key in _PATH_INPUT_KEYS:
            raw = tool_input.get(key)
            if not raw:
                continue
            target = self._resolve(str(raw))
            if target == self.output_path:
                continue
            if self.writable_roots and within_roots(target, self.writable_roots):
                continue
            if self.root is None or not target.is_relative_to(self.root):
                return self._deny(f"path escapes the workspace: {raw}")
        return {}

    def _writable_hint(self) -> str:
        if not self.writable_roots:
            return ""
        return " or inside " + ", ".join(str(r) for r in self.writable_roots)

    @staticmethod
    def _deny(reason: str) -> dict:
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            },
        }

    def _resolve(self, raw_path: str) -> Path:
        path = Path(raw_path)
        if not path.is_absolute() and self.root is not None:
            path = self.root / path
        return path.resolve()

    def _escapes_workspace(self, raw_path: str) -> bool:
        return not self._resolve(raw_path).is_relative_to(self.root)

    # --- PostToolUse ----------------------------------------------------

    async def post_bash(self, hook_input: dict, tool_use_id: str | None,
                        context: HookContext) -> dict:
        command = (hook_input.get("tool_input") or {}).get("command", "")
        if _is_check_command(command):
            if _bash_check_failed(hook_input.get("tool_response")):
                self.consecutive_check_failures += 1
            else:
                self.consecutive_check_failures = 0
        return {}

    async def post_write(self, hook_input: dict, tool_use_id: str | None,
                         context: HookContext) -> dict:
        raw = (hook_input.get("tool_input") or {}).get("file_path", "")
        if raw:
            key = self._workspace_relative(str(raw))
            self.file_edit_counts[key] = self.file_edit_counts.get(key, 0) + 1
        return {}

    def _workspace_relative(self, raw_path: str) -> str:
        path = Path(raw_path)
        target = path if path.is_absolute() else self.root / path
        try:
            return str(target.resolve().relative_to(self.root))
        except ValueError:
            return raw_path


def _trace_message(message: Any) -> None:
    """Verbose reasoning trace at the SDK's own message-per-turn boundary
    (cli-logging spec §6.2). Block shapes are duck-typed (text/thinking/
    tool-use/tool-result), so this stays best-effort across SDK versions and
    never raises into the run."""
    if not trace_log.isEnabledFor(logging.DEBUG):
        return
    content = getattr(message, "content", None)
    if not isinstance(content, list):
        return
    for block in content:
        text = getattr(block, "text", None)
        if text:
            trace_log.debug(str(text)[:TRACE_CONSOLE_CHARS],
                            extra={"trace_kind": "text"})
            continue
        thinking = getattr(block, "thinking", None)
        if thinking:
            trace_log.debug(str(thinking)[:TRACE_CONSOLE_CHARS],
                            extra={"trace_kind": "thinking"})
            continue
        name = getattr(block, "name", None)
        if name is not None and hasattr(block, "input"):
            trace_log.debug(f"{name}({getattr(block, 'input', {})})"[:TRACE_CONSOLE_CHARS],
                            extra={"trace_kind": "tool_call"})
            continue
        if hasattr(block, "tool_use_id"):
            trace_log.debug(str(getattr(block, "content", ""))[:TRACE_CONSOLE_CHARS],
                            extra={"trace_kind": "tool_result"})


def _make_finish_tool(state: _RunState):
    @_sdk().tool(FINISH_TOOL_NAME, FINISH_TOOL_DESCRIPTION, FINISH_TOOL_SCHEMA)
    async def finish(args: dict) -> dict:
        state.finish = dict(args)
        return {"content": [{"type": "text", "text": "acknowledged"}]}

    return finish


_fs_safe = fs_safe


_FINISH_COMPLETION_NOTE = (
    "Then call `finish` with a short summary and completed=true. "
    "Do not put your final answer in the finish call itself — only in the file.")


def _answer_instructions(output_path: Path, schema: dict | None,
                         completion_note: str = _FINISH_COMPLETION_NOTE) -> str:
    """The tmp-file answer-capture convention (execution-tier-agents spec
    §9.5): the final answer travels via the scratch file, never via finish.
    `completion_note` is the harness-specific "and then what" suffix — the
    default is this harness's finish-tool call; claude_code_cli (which has no
    finish tool) substitutes its own (claude-code-cli harness spec §5.2)."""
    shape = (f"as JSON matching this schema:\n{json.dumps(schema, indent=2)}"
             if schema is not None else "as plain text")
    return (f"\n\nWhen you are done, write your final answer {shape} to the file: "
            f"{output_path}\n{completion_note}")


def usage_from_dict(usage: dict | None) -> TokenUsage:
    """Reduce an Anthropic-shaped usage dict to the TokenUsage the analytics
    pipeline expects — the CLI's result JSON and the SDK's ResultMessage.usage
    share the exact same key names, so both harnesses reduce through here."""
    return usage_from_mapping(usage)


def _usage_of(result: Any) -> TokenUsage:
    """Reduce ResultMessage.usage to the TokenUsage shape the analytics
    pipeline expects (spec §7 token-accounting row)."""
    return usage_from_dict(result.usage if result else None)


# The subscription-auth env var the `claude` binary reads.
CLAUDE_OAUTH_TOKEN_VAR = "CLAUDE_CODE_OAUTH_TOKEN"

# Syntactically valid dummies of the two credential shapes (spec §8.3), in
# case the CLI validates a credential's format locally before sending it. The
# broker replaces whichever one arrives; the real value never enters the run.
PLACEHOLDER_SUBSCRIPTION_TOKEN = "sk-ant-oat01-sprintbaton-broker-placeholder-" + "A" * 40
PLACEHOLDER_API_KEY = "sk-ant-api03-sprintbaton-broker-placeholder-" + "A" * 40

# Anything that would outrank CLAUDE_CODE_OAUTH_TOKEN in Claude Code's
# documented credential precedence (cloud-provider selection, then
# ANTHROPIC_AUTH_TOKEN, then ANTHROPIC_API_KEY, then apiKeyHelper). Never in a
# subscription run's environment (spec §8.3; asserted by tests).
_OUTRANKS_OAUTH_TOKEN = (
    "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
)


def _auth_env(spec: HarnessTaskSpec, model: ModelSpec) -> dict[str, str]:
    """The per-call env overlay for a *locally* spawned `claude` (tool mode).

    The SDK's own transport overlays this onto the inherited environment and
    can never delete a key, so each credential variable a different auth mode
    would use is blanked explicitly rather than left to inheritance:

    - subscription with the owner's token: CLAUDE_CODE_OAUTH_TOKEN = token and
      every variable that outranks it blanked;
    - subscription with no token: nothing — the host's `claude login` session
      is the credential (tool mode only, spec §9.2);
    - metered: ANTHROPIC_API_KEY = key, and an ambient subscription token
      blanked, so a key that resolved empty can never fall through onto
      someone's subscription.
    """
    env: dict[str, str] = {}
    if model.base_url:
        env["ANTHROPIC_BASE_URL"] = model.base_url
    if spec.subscription_auth:
        if model.auth_token:
            env[CLAUDE_OAUTH_TOKEN_VAR] = model.auth_token
            for var in _OUTRANKS_OAUTH_TOKEN:
                env[var] = ""
        return env
    if model.api_key:
        env["ANTHROPIC_API_KEY"] = model.api_key
    if os.environ.get(CLAUDE_OAUTH_TOKEN_VAR):
        env[CLAUDE_OAUTH_TOKEN_VAR] = ""
    return env


def sandbox_run_env(bound: BoundWorkspace, spec: HarnessTaskSpec, model: ModelSpec,
                    delivery: str) -> tuple[dict[str, str], UpstreamCredential | None,
                                            tuple[str, ...]]:
    """The COMPLETE environment of a sandboxed `claude` run (spec §8.3), the
    broker credential to bind into its run token, and any extra egress hosts.

    Built from nothing but the session's base tool environment — never from
    this worker's own — so no deployment secret or other owner's credential
    can be inherited by construction.

    `delivery` is the harness's `credential_delivery`:
    - "broker": the run holds a placeholder shaped like the owner's credential
      and ANTHROPIC_BASE_URL points at the run's loopback bridge; the broker
      injects the real one;
    - "env": the owner's real credential in this run's environment only, the
      upstream reachable through the allow-list, and the value registered so
      the change set is scanned for it before it is applied.
    """
    env = dict(bound.session.tool_env())
    for var in _OUTRANKS_OAUTH_TOKEN + (CLAUDE_OAUTH_TOKEN_VAR, "ANTHROPIC_BASE_URL"):
        env.pop(var, None)
    env.update({
        "CLAUDE_CODE_ENTRYPOINT": "sdk-py",
        "CLAUDE_CONFIG_DIR": bound.session.state_dir("claude"),
        # Telemetry and error reporting bypass ANTHROPIC_BASE_URL; with no
        # network they would only fail, so they are not attempted.
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    })
    subscription = spec.subscription_auth
    real = model.auth_token if subscription else model.api_key
    upstream = model.base_url or DEFAULT_ANTHROPIC_UPSTREAM
    if delivery == "broker":
        env["ANTHROPIC_BASE_URL"] = BRIDGE_URL
        if subscription:
            env[CLAUDE_OAUTH_TOKEN_VAR] = PLACEHOLDER_SUBSCRIPTION_TOKEN
        else:
            env["ANTHROPIC_API_KEY"] = PLACEHOLDER_API_KEY
        credential = UpstreamCredential(
            kind="subscription" if subscription else "metered",
            value=real, upstream=upstream, provider=model.provider)
        return env, credential, ()
    # "env" delivery
    if subscription:
        env[CLAUDE_OAUTH_TOKEN_VAR] = real
    else:
        env["ANTHROPIC_API_KEY"] = real
    if model.base_url:
        env["ANTHROPIC_BASE_URL"] = model.base_url
    bound.register_secret(real)
    host = upstream.split("://", 1)[-1].split("/", 1)[0].rsplit("@", 1)[-1].split(":")[0]
    return env, None, (host,)


def _warn_gateway(spec: HarnessTaskSpec, model: ModelSpec) -> None:
    """The run-time surface of the base-URL advisory (hosted-sandbox-isolation
    spec §9.3): repeated per run, like every advisory's run-time surface, so it
    reaches whoever reads the logs after a task behaved oddly."""
    from sprintbaton.harness.advisories import CLAUDE_SDK_GATEWAY, conditional_advisory

    advisory = conditional_advisory(CLAUDE_SDK_GATEWAY)
    log.warning("claude_agent_sdk: %s", advisory.summary, extra={
        "event": "harness_advisory", "task_id": spec.task_id,
        "harness": ClaudeAgentSdkHarness.name, "base_url": model.base_url,
        "reference_url": advisory.reference_url,
        "verified_on": advisory.verified_on,
    })


class ClaudeAgentSdkHarness:
    name = "claude_agent_sdk"
    # Spawns the same `claude` binary claude_code_cli does, so it understands
    # CLAUDE_CODE_OAUTH_TOKEN; and it takes a metered key too (auth-mode-
    # resolution spec §3). The only dual-capable harness in the system.
    supports_subscription_auth = True
    supports_metered_auth = True
    requires_explicit_key_when_metered = True
    # The CLI reads a subscription token from its environment, so an owner's
    # own stored subscription Credential can drive it (hosted-sandbox-
    # isolation spec §9.2).
    subscription_token_var = CLAUDE_OAUTH_TOKEN_VAR
    # Read-only runs honor writable_paths (project-initialization-task §7.1).
    supports_writable_paths = True
    # The whole `claude` process crosses the seam (hosted-sandbox-isolation
    # spec §7.2); hooks, the finish tool and the SDK stay in the worker.
    sandbox_mode = "process"
    # The run holds a placeholder; the broker injects the real credential
    # (spec §8.3). Flip to "env" if the CLI ever rejects the placeholder
    # (§15 q1) — nothing else depends on it.
    credential_delivery = "broker"

    # Credential-agnostic singleton (per-user-provider-credentials spec §4.6):
    # the credential rides the per-call ModelSpec.
    def __init__(self, sandbox: SandboxRuntime | None = None,
                 cli_path: str = "claude"):
        self._sandbox = sandbox or LOCAL_RUNTIME
        # The binary's name inside the sandbox image (spec §10.1). Unused under
        # the passthrough, where the SDK finds its own bundled binary.
        self._cli_path = cli_path

    def execute(self, spec: HarnessTaskSpec, model: ModelSpec) -> HarnessResult:
        if model.provider != "anthropic":
            raise ValueError(
                f"claude_agent_sdk only supports provider 'anthropic', got {model.provider!r}"
            )
        if model.base_url:
            _warn_gateway(spec, model)
        # Bridge locally; async is not propagated up through the agent,
        # service, orchestrator, or polling layers (spec §8).
        return asyncio.run(self._run(spec, model))

    async def _run(self, spec: HarnessTaskSpec, model: ModelSpec) -> HarnessResult:
        if spec.read_only:
            return await self._run_read_only(spec, model)
        return await self._run_execution(spec, model)

    def _build_options(self, spec: HarnessTaskSpec, model: ModelSpec,
                       state: _RunState) -> ClaudeAgentOptions:
        # The overlay applies only to a locally spawned binary; a sandboxed
        # one gets a complete environment from sandbox_run_env instead.
        env = {} if self._sandbox.isolated else _auth_env(spec, model)
        sdk = _sdk()
        return sdk.ClaudeAgentOptions(
            system_prompt=spec.system_prompt,
            cwd=spec.workspace_path,
            model=model.model_id,
            tools=list(CORE_TOOLS),
            allowed_tools=[*CORE_TOOLS, FINISH_MCP_TOOL],
            # Headless service: never block on an interactive permission
            # prompt. Irreversibility is enforced by the PreToolUse hooks,
            # which run regardless of permission mode.
            permission_mode="bypassPermissions",
            max_turns=spec.max_iterations,
            resume=spec.conversation_id,
            # Never load user/project/local Claude Code settings inside the
            # cluster service — the harness must be fully self-configured.
            setting_sources=[],
            mcp_servers={
                MCP_SERVER_NAME: sdk.create_sdk_mcp_server(
                    name=MCP_SERVER_NAME, tools=[_make_finish_tool(state)],
                ),
            },
            hooks={
                "PreToolUse": [
                    sdk.HookMatcher(matcher="Bash", hooks=[state.pre_bash]),
                    sdk.HookMatcher(matcher=FILE_PATH_MATCHER, hooks=[state.pre_file]),
                ],
                "PostToolUse": [
                    sdk.HookMatcher(matcher="Bash", hooks=[state.post_bash]),
                    sdk.HookMatcher(matcher=FILE_WRITE_MATCHER, hooks=[state.post_write]),
                ],
            },
            env=env,
        )

    def _build_read_only_options(self, spec: HarnessTaskSpec, model: ModelSpec,
                                 state: _RunState, scratch_dir: str) -> ClaudeAgentOptions:
        env = {} if self._sandbox.isolated else _auth_env(spec, model)
        # No workspace -> nothing to browse: only the scratch-file Write and
        # finish are offered (spec §9.6 — e.g. passing_criteria).
        tools = list(READ_ONLY_TOOLS) if spec.workspace_path else ["Write"]
        if spec.workspace_path and spec.writable_paths:
            tools += WRITABLE_ROOT_TOOLS
        sdk = _sdk()
        return sdk.ClaudeAgentOptions(
            system_prompt=spec.system_prompt,
            cwd=spec.workspace_path or scratch_dir,
            model=model.model_id,
            tools=tools,
            allowed_tools=[*tools, FINISH_MCP_TOOL],
            permission_mode="bypassPermissions",
            max_turns=spec.max_iterations,
            resume=spec.conversation_id,
            setting_sources=[],
            mcp_servers={
                MCP_SERVER_NAME: sdk.create_sdk_mcp_server(
                    name=MCP_SERVER_NAME, tools=[_make_finish_tool(state)],
                ),
            },
            hooks={
                "PreToolUse": [
                    sdk.HookMatcher(matcher="Bash", hooks=[state.pre_bash_read_only]),
                    sdk.HookMatcher(matcher=FILE_PATH_MATCHER, hooks=[state.pre_file_read_only]),
                ],
            },
            env=env,
        )

    def _transport(self, bound: BoundWorkspace, spec: HarnessTaskSpec,
                   model: ModelSpec, prompt: str, options: ClaudeAgentOptions):
        """None under the passthrough (the SDK spawns its bundled binary
        locally); a SandboxTransport when the sandbox is isolated (§7.2)."""
        if not bound.isolated:
            return None
        from sprintbaton.sandbox.transport import SandboxTransport

        env, credential, extra_hosts = sandbox_run_env(
            bound, spec, model, self.credential_delivery)
        env["PWD"] = str(options.cwd or "/")
        grant = bound.mint_egress(credential=credential, extra_hosts=extra_hosts)
        return SandboxTransport(prompt, options, session=bound.session, env=env,
                                egress=grant, cli_path=self._cli_path,
                                on_close=lambda: bound.revoke(grant))

    async def _stream(self, bound: BoundWorkspace, spec: HarnessTaskSpec,
                      model: ModelSpec, prompt: str, options: ClaudeAgentOptions,
                      state: _RunState) -> None:
        transport = self._transport(bound, spec, model, prompt, options)
        extra = {"transport": transport} if transport is not None else {}
        async for message in query(prompt=prompt, options=options, **extra):
            _trace_message(message)
            if isinstance(message, _result_message_type()):
                state.result_message = message

    async def _run_read_only(self, spec: HarnessTaskSpec, model: ModelSpec) -> HarnessResult:
        bound = open_binding(self._sandbox, spec)
        # One private scratch directory per run, in the session (the binary
        # that writes the answer file may run in the sandbox): uniqueness means
        # concurrent tasks can never pick up each other's answer file, and the
        # task id in the name keeps any leaked directory attributable.
        prefix = _fs_safe(spec.task_id) if spec.task_id else "run"
        with bound.scratch(prefix) as tmp_dir:
            output_path = Path(tmp_dir) / "answer.txt"
            root = Path(spec.workspace_path).resolve() if spec.workspace_path else None
            state = _RunState(root, spec.guardrails, output_path=output_path,
                              writable_roots=tuple(Path(p) for p in spec.writable_paths))
            options = self._build_read_only_options(spec, model, state, tmp_dir)
            user_message = spec.user_message + _answer_instructions(
                output_path, spec.output_schema)

            try:
                await self._stream(bound, spec, model, user_message, options, state)
            except Exception as e:
                bound.sync_back_quietly()
                if state.blocked is not None:
                    raise state.blocked from e
                if looks_transient(str(e)):
                    raise TransientHarnessError(
                        f"claude_agent_sdk run failed on a transient error: {e}") from e
                raise
            # writable_paths edits (the init pass) reach the worker's clone
            # before anything reads them (§6.4).
            bound.sync_back()
            if state.blocked is not None:
                raise state.blocked

            finish = state.finish
            result = state.result_message
            answer = bound.read_text(str(output_path)) or ""
            stop_reason = "completed"
            if result is not None and result.is_error:
                log.warning("claude_agent_sdk read-only run ended in error", extra={
                    "task_id": spec.task_id, "subtype": result.subtype,
                })
                if result.subtype == MAX_TURNS_SUBTYPE:
                    stop_reason = "turn_limit"
                else:
                    stop_reason = "error"
                    error_text = " ".join(
                        str(part) for part in (getattr(result, "errors", None) or [],
                                               getattr(result, "result", "") or ""))
                    if not answer and looks_transient(error_text):
                        raise TransientHarnessError(
                            f"claude_agent_sdk run ended in a transient error: "
                            f"{error_text[:500]}")
            if not answer and stop_reason == "completed":
                # Same-shape failure to malformed single_shot output: the
                # calling agent's parse_json_output / plan handling errors
                # loudly on the empty output_text.
                log.warning("read-only run finished with no answer file", extra={
                    "task_id": spec.task_id, "workspace": spec.workspace_path,
                })
            return HarnessResult(
                summary=finish.get("summary", ""),
                completed=bool(finish.get("completed", False)),
                usage=_usage_of(result),
                conversation_id=result.session_id if result else None,
                output_text=answer,
                stop_reason=stop_reason,
            )

    async def _run_execution(self, spec: HarnessTaskSpec, model: ModelSpec) -> HarnessResult:
        bound = open_binding(self._sandbox, spec)
        state = _RunState(Path(spec.workspace_path).resolve(), spec.guardrails)
        options = self._build_options(spec, model, state)

        try:
            await self._stream(bound, spec, model, spec.user_message, options, state)
        except BaseException:
            bound.sync_back_quietly()
            raise
        # The run's changes reach the worker's clone before the diff (§6.4).
        bound.sync_back()

        if state.blocked is not None:
            # Same semantics as the raw loop's uncaught exception: the task
            # hard-stops to EH, unattended, no retry (spec §6).
            raise state.blocked

        finish = state.finish
        result = state.result_message
        if result is not None and result.is_error:
            log.warning("claude_agent_sdk run ended in error", extra={
                "subtype": result.subtype, "errors": result.errors,
            })
        stop_reason = "completed"
        if result is not None and result.is_error:
            stop_reason = ("turn_limit" if result.subtype == MAX_TURNS_SUBTYPE
                           else "error")
        return HarnessResult(
            summary=finish.get("summary", ""),
            completed=bool(finish.get("completed", False)),
            asked_question=finish.get("question") or None,
            clarification_question=finish.get("clarification_question") or None,
            clarification_options=finish.get("clarification_options") or None,
            plan_broken=bool(finish.get("plan_broken", False)),
            importance_flags=list(finish.get("importance_flags", [])),
            files_edited=dict(state.file_edit_counts),
            consecutive_check_failures=state.consecutive_check_failures,
            diff=workspace_diff(spec.workspace_path),
            usage=_usage_of(result),
            conversation_id=result.session_id if result else None,
            output_text=finish.get("summary", ""),
            stop_reason=stop_reason,
        )

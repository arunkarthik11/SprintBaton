"""The raw_tool_loop harness — SprintBaton's hand-rolled tool loop, relocated
from sprintbaton/models behind the Harness interface (migration spec §3, §11
phase 1). This is a permanent, legitimate member of the harness family, not
scaffolding: fully in-house, no external dependency, and the tightest possible
guardrail control (enforcement is inline in the bash dispatch).

Behavior-preserving relocation of the former WorkspaceToolExecutor +
TaskExecutionAgent loop: tools operate strictly inside the task workspace,
every bash command passes through the irreversibility guard, and file paths
are confined to the workspace root.
"""

import logging
from pathlib import Path
from typing import Callable

from sprintbaton.entities.usage import TokenUsage
from sprintbaton.harness.base import (
    DEFAULT_GUARDRAIL_POLICY,
    FINISH_TOOL_DESCRIPTION,
    FINISH_TOOL_NAME,
    FINISH_TOOL_SCHEMA,
    GuardrailPolicy,
    HarnessResult,
    HarnessTaskSpec,
    ModelSpec,
    workspace_diff,
)
from sprintbaton.models.provider import AnthropicModelProvider
from sprintbaton.sandbox.binding import (
    LOCAL_RUNTIME,
    BoundWorkspace,
    SandboxRuntime,
    open_binding,
)
from sprintbaton.observer.verbosity import TRACE_CONSOLE_CHARS, TRACE_LOGGER_NAME

log = logging.getLogger(__name__)
trace_log = logging.getLogger(TRACE_LOGGER_NAME)

BASH_TIMEOUT_SECONDS = 300
MAX_OUTPUT_CHARS = 20_000

EXECUTION_TOOLS: list[dict] = [
    {
        "name": "bash",
        "description": (
            "Run a shell command inside the repository workspace. Use for inspecting "
            "the repo, running tests/builds, and git status/diff. Destructive or "
            "irreversible commands are blocked; git push is handled by the orchestrator."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": "Read a file from the repository workspace.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Path relative to the repo root"}},
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Create or overwrite a file in the repository workspace.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relative to the repo root"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": FINISH_TOOL_NAME,
        "description": FINISH_TOOL_DESCRIPTION,
        "input_schema": FINISH_TOOL_SCHEMA,
    },
]

# Heuristic for correctness checks: these bash commands count toward the
# consecutive-failure signal when they exit non-zero.
CHECK_COMMAND_MARKERS = ("pytest", "npm test", "yarn test", "go test", "make test",
                         "tsc", "mypy", "build", "cargo test")


class WorkspaceToolExecutor:
    """The three tools, dispatched through the task's sandbox session
    (hosted-sandbox-isolation spec §7.1). The guard runs here, in the worker,
    *before* a call crosses the seam — enforcement stays single-sourced in
    models/guard.py and the sandbox is the second layer, not the policy."""

    def __init__(self, workspace: str | Path,
                 guardrails: GuardrailPolicy = DEFAULT_GUARDRAIL_POLICY,
                 bound: BoundWorkspace | None = None):
        self.root = Path(workspace).resolve()
        self._guardrails = guardrails
        # A direct construction (tests, ad-hoc) runs under the passthrough.
        self._ws = bound or open_binding(LOCAL_RUNTIME, HarnessTaskSpec(
            system_prompt="", workspace_path=str(self.root), read_only=False))
        # Signals consumed by the escalation trigger evaluation
        self.file_edit_counts: dict[str, int] = {}
        self.consecutive_check_failures = 0
        self.last_error: str | None = None

    def _resolve(self, rel_path: str) -> Path:
        target = (self.root / rel_path).resolve()
        if not target.is_relative_to(self.root):
            raise ValueError(f"path escapes the workspace: {rel_path}")
        return target

    def run(self, tool_name: str, tool_input: dict) -> str:
        if tool_name == "bash":
            return self._bash(tool_input["command"])
        if tool_name == "read_file":
            return self._read(tool_input["path"])
        if tool_name == "write_file":
            return self._write(tool_input["path"], tool_input["content"])
        return f"unknown tool: {tool_name}"

    def _bash(self, command: str) -> str:
        self._guardrails.check_command(command)  # raises IrreversibleOperationError -> EH hard stop
        result = self._ws.shell(command, cwd=str(self.root),
                                timeout_seconds=BASH_TIMEOUT_SECONDS)
        output = (result.stdout + result.stderr)[-MAX_OUTPUT_CHARS:]
        if result.timed_out:
            output += f"\n(timed out after {BASH_TIMEOUT_SECONDS}s)"
        if self._is_check_command(command):
            if result.exit_code != 0:
                self.consecutive_check_failures += 1
                self.last_error = output[-2000:]
            else:
                self.consecutive_check_failures = 0
        return f"exit code: {result.exit_code}\n{output}"

    def _read(self, rel_path: str) -> str:
        target = self._resolve(rel_path)
        text = self._ws.read_text(str(target))
        if text is None:
            return f"error: not a file: {rel_path}"
        return text[:MAX_OUTPUT_CHARS]

    def _write(self, rel_path: str, content: str) -> str:
        target = self._resolve(rel_path)
        self._ws.session.write_file(str(target), content.encode())
        self.file_edit_counts[rel_path] = self.file_edit_counts.get(rel_path, 0) + 1
        return f"wrote {len(content)} chars to {rel_path}"

    @staticmethod
    def _is_check_command(command: str) -> bool:
        return any(marker in command for marker in CHECK_COMMAND_MARKERS)

    def diff(self) -> str:
        # Worker-side, on the worker's own clone, after sync_back (§6.4).
        return workspace_diff(self.root)


class RawToolLoopHarness:
    name = "raw_tool_loop"

    # Tool calls cross the sandbox seam; the model loop stays in the worker
    # (hosted-sandbox-isolation spec §7).
    sandbox_mode = "tools"

    def __init__(self, provider_factory: Callable[..., AnthropicModelProvider]
                 = AnthropicModelProvider,
                 sandbox: SandboxRuntime | None = None):
        # Credential-agnostic singleton (per-user-provider-credentials spec
        # §4.6/§4.7): the client is built per execute() from the per-call
        # model.api_key ("" falls back to ambient credentials). The factory
        # parameter exists for tests (ScriptedProvider et al.).
        self._provider_factory = provider_factory
        self._sandbox = sandbox or LOCAL_RUNTIME

    def execute(self, spec: HarnessTaskSpec, model: ModelSpec) -> HarnessResult:
        if model.provider != "anthropic":
            raise ValueError(
                f"raw_tool_loop only supports provider 'anthropic', got {model.provider!r}"
            )
        provider = self._make_provider(model)
        bound = open_binding(self._sandbox, spec)
        executor = WorkspaceToolExecutor(spec.workspace_path, guardrails=spec.guardrails,
                                         bound=bound)
        try:
            usage, finish = self._loop(spec, model, provider, executor)
        except BaseException:
            # A hard stop still leaves the worker holding whatever partial
            # work exists, exactly as when tools ran on its own disk.
            bound.sync_back_quietly()
            raise
        # Apply what the run changed to the worker's clone before the diff.
        bound.sync_back()

        return HarnessResult(
            summary=finish.get("summary", ""),
            completed=bool(finish.get("completed", False)),
            asked_question=finish.get("question") or None,
            clarification_question=finish.get("clarification_question") or None,
            clarification_options=finish.get("clarification_options") or None,
            plan_broken=bool(finish.get("plan_broken", False)),
            importance_flags=list(finish.get("importance_flags", [])),
            files_edited=dict(executor.file_edit_counts),
            consecutive_check_failures=executor.consecutive_check_failures,
            diff=executor.diff(),
            usage=usage,
            output_text=finish.get("summary", ""),
        )

    def _make_provider(self, model: ModelSpec) -> AnthropicModelProvider:
        if model.base_url:
            return self._provider_factory(model.api_key, base_url=model.base_url)
        return self._provider_factory(model.api_key)

    def _loop(self, spec: HarnessTaskSpec, model: ModelSpec,
              provider: AnthropicModelProvider,
              executor: WorkspaceToolExecutor) -> tuple[TokenUsage, dict]:
        messages: list[dict] = [{"role": "user", "content": spec.user_message}]
        usage = TokenUsage()
        finish: dict = {}

        for _ in range(spec.max_iterations):
            response = provider.complete(
                model=model.model_id, system=spec.system_prompt, messages=messages,
                tools=EXECUTION_TOOLS, adaptive_thinking=True,
            )
            usage = usage + provider.usage_of(response)

            tool_uses = [b for b in response.content if b.type == "tool_use"]
            messages.append({"role": "assistant", "content": response.content})

            # Verbose reasoning trace at turn/tool-call boundaries (cli-logging
            # spec §6.2) — isEnabledFor short-circuits when not verbose.
            if trace_log.isEnabledFor(logging.DEBUG):
                for block in response.content:
                    if block.type == "text" and block.text:
                        trace_log.debug(block.text[:TRACE_CONSOLE_CHARS],
                                        extra={"trace_kind": "text"})
                    elif block.type == "thinking" and block.thinking:
                        trace_log.debug(block.thinking[:TRACE_CONSOLE_CHARS],
                                        extra={"trace_kind": "thinking"})
                for block in tool_uses:
                    trace_log.debug(f"{block.name}({block.input})"[:TRACE_CONSOLE_CHARS],
                                    extra={"trace_kind": "tool_call"})

            if not tool_uses:
                # No tool call means the turn is over; re-sending would leave a
                # trailing assistant message (rejected as a prefill on 4.6+ models)
                break

            done = False
            results = []
            for block in tool_uses:
                if block.name == FINISH_TOOL_NAME:
                    finish = dict(block.input)
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": "acknowledged"})
                    done = True
                    continue
                try:
                    output = executor.run(block.name, dict(block.input))
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": output})
                    trace_log.debug(output[:TRACE_CONSOLE_CHARS],
                                    extra={"trace_kind": "tool_result"})
                except ValueError as e:
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": f"error: {e}", "is_error": True})
                    trace_log.debug(f"error: {e}"[:TRACE_CONSOLE_CHARS],
                                    extra={"trace_kind": "tool_result"})
                # IrreversibleOperationError deliberately propagates: hard stop -> EH
            messages.append({"role": "user", "content": results})
            if done:
                break

        return usage, finish

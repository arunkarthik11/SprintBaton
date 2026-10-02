"""The openai_agent_sdk harness — the Coding Model / advisory tool loop on the
OpenAI Agents SDK (multi-provider-parity spec §4.1), the OpenAI analogue of
`claude_agent_sdk`. Read-only **and** write-capable execution modes; full
capability parity with the Anthropic path.

Verified against the OpenAI Agents SDK docs (2026-07):

- `Agent(name, model, instructions, tools=[...])` + `Runner.run_sync(agent,
  input, max_turns=...) -> RunResult`; `RunResult.final_output`,
  `.last_response_id` (the resume handle, passed back as `previous_response_id`),
  `.context_wrapper.usage` (`input_tokens`/`output_tokens`).
- Tools are `FunctionTool(name, description, params_json_schema, on_invoke_tool)`
  where `on_invoke_tool(ctx, args_json: str)` — SprintBaton owns the tool body,
  which is where the guardrail lives (RunHooks are observe-only, so a hook can
  *not* deny a call; the tool itself must — see agent_framework.AgentToolbox).

`openai-agents` is an OPTIONAL dependency (pip extra "openai-agents"); the import
is lazy inside the invoker so HarnessRegistry construction never needs it (§6).
Unit tests inject a fake invoker; the live SDK path is conformance-gated behind
SPRINTBATON_CONFORMANCE_LIVE=1 (§8).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from sprintbaton.entities.usage import TokenUsage
from sprintbaton.harness.agent_framework import (
    AgentToolbox,
    build_execution_result,
    build_read_only_result,
)
from sprintbaton.harness.base import (
    FINISH_TOOL_DESCRIPTION,
    FINISH_TOOL_NAME,
    FINISH_TOOL_SCHEMA,
    HarnessResult,
    HarnessTaskSpec,
    ModelSpec,
)
from sprintbaton.harness.claude_agent_sdk import _answer_instructions, _fs_safe
from sprintbaton.sandbox.binding import LOCAL_RUNTIME, SandboxRuntime, open_binding
from sprintbaton.dependencies import require_module

log = logging.getLogger(__name__)


# Tool JSON schemas (the OpenAI Agents SDK builds a FunctionTool from an explicit
# params_json_schema). Object shapes mirror raw_tool_loop's EXECUTION_TOOLS.
_BASH_SCHEMA = {"type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"], "additionalProperties": False}
_READ_SCHEMA = {"type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"], "additionalProperties": False}
_WRITE_SCHEMA = {"type": "object",
                 "properties": {"path": {"type": "string"},
                                "content": {"type": "string"}},
                 "required": ["path", "content"], "additionalProperties": False}
_EDIT_SCHEMA = {"type": "object",
                "properties": {"path": {"type": "string"},
                               "old": {"type": "string"},
                               "new": {"type": "string"}},
                "required": ["path", "old", "new"], "additionalProperties": False}


@dataclass
class _Outcome:
    usage: TokenUsage
    conversation_id: str | None
    # "turn_limit" when Runner.run_sync raised MaxTurnsExceeded
    # (project-initialization-task spec §7.2).
    stop_reason: str = "completed"


def _usage_of(run_result: Any) -> TokenUsage:
    """Reduce RunResult.context_wrapper.usage to TokenUsage — best-effort over
    the documented field names."""
    ctx = getattr(run_result, "context_wrapper", None)
    usage = getattr(ctx, "usage", None) if ctx is not None else None
    if usage is None:
        return TokenUsage()
    return TokenUsage(
        inputTokens=int(getattr(usage, "input_tokens", 0) or 0),
        outputTokens=int(getattr(usage, "output_tokens", 0) or 0),
    )


class OpenAiAgentSdkHarness:
    name = "openai_agent_sdk"
    provider = "openai"
    # Read-only runs honor writable_paths (project-initialization-task §7.1).
    supports_writable_paths = True
    # Tool bodies cross the sandbox seam; the SDK and its model calls stay in
    # the worker (hosted-sandbox-isolation spec §7).
    sandbox_mode = "tools"

    def __init__(self, invoker: Callable[..., _Outcome] | None = None,
                 sandbox: SandboxRuntime | None = None):
        self._sandbox = sandbox or LOCAL_RUNTIME
        # The invoker is the whole SDK boundary in one seam: it builds the Agent
        # from the toolbox's tools + instructions, runs it, drives the toolbox
        # tools, and returns usage + resume id. Default lazily imports `agents`;
        # tests inject a fake that exercises the toolbox without the SDK (§8).
        self._invoke = invoker or self._default_invoke

    def execute(self, spec: HarnessTaskSpec, model: ModelSpec) -> HarnessResult:
        if model.provider != "openai":
            raise ValueError(
                f"openai_agent_sdk only supports provider 'openai', "
                f"got {model.provider!r}")
        if spec.read_only:
            return self._run_read_only(spec, model)
        return self._run_execution(spec, model)

    # --------------------------------------------------------------- run modes

    def _run_execution(self, spec: HarnessTaskSpec, model: ModelSpec) -> HarnessResult:
        bound = open_binding(self._sandbox, spec)
        toolbox = AgentToolbox(spec, read_only=False, bound=bound)
        try:
            outcome = self._invoke(spec, model, toolbox, spec.user_message)
        except BaseException:
            bound.sync_back_quietly()
            raise
        # The run's changes reach the worker's clone before the diff (§6.4).
        bound.sync_back()
        if toolbox.blocked is not None:
            # Same semantics as claude_agent_sdk: the task hard-stops to EH,
            # unattended, no retry — even if the framework swallowed the raise.
            raise toolbox.blocked
        return build_execution_result(
            toolbox, usage=outcome.usage, conversation_id=outcome.conversation_id)

    def _run_read_only(self, spec: HarnessTaskSpec, model: ModelSpec) -> HarnessResult:
        bound = open_binding(self._sandbox, spec)
        prefix = _fs_safe(spec.task_id) if spec.task_id else "run"
        # One private scratch directory per run, inside the session: the
        # answer file is written by a tool body that runs in the sandbox.
        with bound.scratch(prefix) as scratch:
            output_path = Path(scratch) / "answer.txt"
            toolbox = AgentToolbox(spec, read_only=True, output_path=output_path,
                                   bound=bound)
            user_message = spec.user_message + _answer_instructions(
                output_path, spec.output_schema)
            try:
                outcome = self._invoke(spec, model, toolbox, user_message)
            except BaseException:
                bound.sync_back_quietly()
                raise
            # writable_paths edits (the init pass) reach the worker first.
            bound.sync_back()
            if toolbox.blocked is not None:
                raise toolbox.blocked
            answer = bound.read_text(str(output_path)) or ""
            if not answer:
                log.warning("openai_agent_sdk read-only run produced no answer file",
                            extra={"task_id": spec.task_id})
            return build_read_only_result(
                toolbox, answer, usage=outcome.usage,
                conversation_id=outcome.conversation_id,
                stop_reason=getattr(outcome, "stop_reason", "completed"))

    # ------------------------------------------------------ real SDK invoker

    def _default_invoke(self, spec: HarnessTaskSpec, model: ModelSpec,
                        toolbox: AgentToolbox, user_message: str) -> _Outcome:
        require_module("agents", package="openai-agents", extra="openai-agents",
                       harness="openai_agent_sdk")
        from agents import Agent, FunctionTool, Runner  # optional dep, lazy (§6)
        from agents.exceptions import MaxTurnsExceeded

        def _tool(name: str, description: str, schema: dict,
                  handler: Callable[[dict], str]) -> Any:
            async def on_invoke(ctx: Any, args_json: str) -> str:
                args = json.loads(args_json) if args_json else {}
                return handler(args)
            return FunctionTool(name=name, description=description,
                                params_json_schema=schema, on_invoke_tool=on_invoke)

        tools = self._tools(spec, toolbox, _tool)
        agent = Agent(name="sprintbaton", model=model.model_id,
                      instructions=spec.system_prompt, tools=tools)
        # os.environ overlay so the SDK client authenticates with the resolved
        # per-user key (per-user-provider-credentials spec §4.9); ambient
        # OPENAI_API_KEY is the fallback when none resolved.
        with _openai_key(model.api_key):
            try:
                result = Runner.run_sync(agent, user_message,
                                         max_turns=spec.max_iterations)
            except MaxTurnsExceeded:
                # A turn cap is a stop, not a failure (project-initialization-
                # task spec §7.2) — the toolbox's on-disk edits survive.
                if toolbox.blocked is not None:
                    return _Outcome(usage=TokenUsage(), conversation_id=None)
                return _Outcome(usage=TokenUsage(), conversation_id=None,
                                stop_reason="turn_limit")
            except Exception:
                if toolbox.blocked is not None:
                    # The catastrophic-command raise; surfaced by the caller.
                    return _Outcome(usage=TokenUsage(), conversation_id=None)
                raise
        return _Outcome(usage=_usage_of(result),
                        conversation_id=getattr(result, "last_response_id", None))

    @staticmethod
    def _tools(spec: HarnessTaskSpec, toolbox: AgentToolbox,
               make: Callable[[str, str, dict, Callable[[dict], str]], Any]) -> list:
        finish = make(FINISH_TOOL_NAME, FINISH_TOOL_DESCRIPTION, FINISH_TOOL_SCHEMA,
                      toolbox.record_finish)
        if spec.read_only:
            write_help = ("Write the final answer file, or a file under a writable "
                          "directory." if spec.writable_paths
                          else "Write the final answer file.")
            tools = [make("write_file", write_help,
                          _WRITE_SCHEMA,
                          lambda a: toolbox.write_file(a["path"], a["content"])),
                     finish]
            if spec.workspace_path:
                tools[0:0] = [
                    make("bash", "Run a read-only shell command in the workspace.",
                         _BASH_SCHEMA, lambda a: toolbox.bash(a["command"])),
                    make("read_file", "Read a file from the workspace.",
                         _READ_SCHEMA, lambda a: toolbox.read_file(a["path"])),
                ]
                if spec.writable_paths:
                    # In-place editing inside the writable roots (§7.1); the
                    # toolbox denies every other target.
                    tools.insert(len(tools) - 1, make(
                        "edit_file", "Replace a string in a file under a writable directory.",
                        _EDIT_SCHEMA,
                        lambda a: toolbox.edit_file(a["path"], a["old"], a["new"])))
            return tools
        return [
            make("bash", "Run a shell command in the workspace.", _BASH_SCHEMA,
                 lambda a: toolbox.bash(a["command"])),
            make("read_file", "Read a file from the workspace.", _READ_SCHEMA,
                 lambda a: toolbox.read_file(a["path"])),
            make("write_file", "Create or overwrite a file in the workspace.",
                 _WRITE_SCHEMA, lambda a: toolbox.write_file(a["path"], a["content"])),
            make("edit_file", "Replace a string in a workspace file.", _EDIT_SCHEMA,
                 lambda a: toolbox.edit_file(a["path"], a["old"], a["new"])),
            finish,
        ]


class _openai_key:
    """Overlay the resolved key onto OPENAI_API_KEY for the run, restoring the
    prior value after — overlay, never replace, so an ambient login survives an
    empty resolved key (per-user-provider-credentials spec §4.9)."""

    def __init__(self, api_key: str):
        self._api_key = api_key
        self._prior: str | None = None

    def __enter__(self):
        import os
        if self._api_key:
            self._prior = os.environ.get("OPENAI_API_KEY")
            os.environ["OPENAI_API_KEY"] = self._api_key
        return self

    def __exit__(self, *exc):
        import os
        if self._api_key:
            if self._prior is None:
                os.environ.pop("OPENAI_API_KEY", None)
            else:
                os.environ["OPENAI_API_KEY"] = self._prior
        return False

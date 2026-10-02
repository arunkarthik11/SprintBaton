"""The gemini_agent_sdk harness — the Coding Model / advisory tool loop on the
Google Agent Development Kit (ADK) — multi-provider-parity spec §4.1, the Google
analogue of `claude_agent_sdk`. Read-only **and** write-capable execution modes.

Verified against the ADK docs (2026-07):

- `LlmAgent(name, model, instruction, tools=[...], before_tool_callback=...)`;
  tools are plain typed Python callables ADK introspects into schemas.
- `before_tool_callback(tool, args, tool_context)` — unlike OpenAI's observe-only
  hooks, this one **can deny**: returning a dict skips the real tool and uses the
  dict as the result. It is wired here as the guardrail hard-stop, with
  agent_framework.AgentToolbox's in-tool guard as defense-in-depth.
- Runtime: `Runner(agent, app_name, session_service)` with
  `InMemorySessionService`; `runner.run(user_id, session_id, new_message)` yields
  events carrying `usage_metadata` (`prompt_token_count`/`candidates_token_count`).

`google-adk` is an OPTIONAL dependency (pip extra "google-adk"); the import is
lazy inside the invoker (§6). Unit tests inject a fake invoker; the live SDK path
is conformance-gated behind SPRINTBATON_CONFORMANCE_LIVE=1 (§8).
"""

from __future__ import annotations

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
from sprintbaton.harness.base import HarnessResult, HarnessTaskSpec, ModelSpec
from sprintbaton.harness.claude_agent_sdk import _answer_instructions, _fs_safe
from sprintbaton.sandbox.binding import LOCAL_RUNTIME, SandboxRuntime, open_binding
from sprintbaton.models.guard import IrreversibleOperationError
from sprintbaton.dependencies import require_module

log = logging.getLogger(__name__)

_APP_NAME = "sprintbaton"


@dataclass
class _Outcome:
    usage: TokenUsage
    conversation_id: str | None
    # "turn_limit" when ADK's LLM-call cap stopped the run
    # (project-initialization-task spec §7.2).
    stop_reason: str = "completed"


# ADK's turn-cap exception, matched by class name so a moved import path across
# ADK versions still classifies (RunConfig.max_llm_calls raises it).
_LLM_CALLS_LIMIT_ERROR = "LlmCallsLimitExceededError"


class GeminiAgentSdkHarness:
    name = "gemini_agent_sdk"
    provider = "google"
    # Read-only runs honor writable_paths (project-initialization-task §7.1).
    supports_writable_paths = True
    # Tool bodies cross the sandbox seam; the SDK and its model calls stay in
    # the worker (hosted-sandbox-isolation spec §7).
    sandbox_mode = "tools"

    def __init__(self, invoker: Callable[..., _Outcome] | None = None,
                 sandbox: SandboxRuntime | None = None):
        self._sandbox = sandbox or LOCAL_RUNTIME
        # The invoker is the whole ADK boundary in one seam (mirrors
        # openai_agent_sdk). Default lazily imports google.adk; tests inject a
        # fake that exercises the toolbox without the SDK (§8).
        self._invoke = invoker or self._default_invoke

    def execute(self, spec: HarnessTaskSpec, model: ModelSpec) -> HarnessResult:
        if model.provider != "google":
            raise ValueError(
                f"gemini_agent_sdk only supports provider 'google', "
                f"got {model.provider!r}")
        if spec.read_only:
            return self._run_read_only(spec, model)
        return self._run_execution(spec, model)

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
                log.warning("gemini_agent_sdk read-only run produced no answer file",
                            extra={"task_id": spec.task_id})
            return build_read_only_result(
                toolbox, answer, usage=outcome.usage,
                conversation_id=outcome.conversation_id,
                stop_reason=getattr(outcome, "stop_reason", "completed"))

    # ------------------------------------------------------ real ADK invoker

    def _default_invoke(self, spec: HarnessTaskSpec, model: ModelSpec,
                        toolbox: AgentToolbox, user_message: str) -> _Outcome:
        import uuid

        require_module("google.adk", package="google-adk", extra="google-adk",
                       harness="gemini_agent_sdk")
        from google.adk.agents import LlmAgent  # optional dep, lazy (§6)
        from google.adk.agents.run_config import RunConfig
        from google.adk.runners import Runner
        from google.adk.sessions import InMemorySessionService
        from google.genai import types

        tools = _adk_tools(spec, toolbox)
        agent = LlmAgent(
            name="sprintbaton", model=model.model_id,
            instruction=spec.system_prompt, tools=tools,
            before_tool_callback=_guard_callback(toolbox),
        )
        session_service = InMemorySessionService()
        session_id = uuid.uuid4().hex
        session_service.create_session(
            app_name=_APP_NAME, user_id="sprintbaton", session_id=session_id)
        message = types.Content(role="user", parts=[types.Part(text=user_message)])
        usage = TokenUsage()
        with _gemini_key(model.api_key):
            runner = Runner(agent=agent, app_name=_APP_NAME,
                            session_service=session_service)
            try:
                for event in runner.run(user_id="sprintbaton", session_id=session_id,
                                        new_message=message,
                                        run_config=RunConfig(
                                            max_llm_calls=spec.max_iterations)):
                    usage = usage + _event_usage(event)
            except Exception as e:
                if toolbox.blocked is not None:
                    return _Outcome(usage=usage, conversation_id=None)
                if type(e).__name__ == _LLM_CALLS_LIMIT_ERROR:
                    # A turn cap is a stop, not a failure (§7.2).
                    return _Outcome(usage=usage, conversation_id=session_id,
                                    stop_reason="turn_limit")
                raise
        return _Outcome(usage=usage, conversation_id=session_id)


def _event_usage(event: Any) -> TokenUsage:
    meta = getattr(event, "usage_metadata", None)
    if meta is None:
        return TokenUsage()
    return TokenUsage(
        inputTokens=int(getattr(meta, "prompt_token_count", 0) or 0),
        outputTokens=int(getattr(meta, "candidates_token_count", 0) or 0),
        cacheReadInputTokens=int(getattr(meta, "cached_content_token_count", 0) or 0),
    )


def _guard_callback(toolbox: AgentToolbox):
    """A before_tool_callback that hard-stops a catastrophic bash command by
    returning a deny result (skipping the real tool) and recording the block so
    the harness aborts after the run — ADK's callback CAN deny, unlike OpenAI's
    observe-only hooks (§4.1)."""
    def before_tool(tool: Any, args: dict, tool_context: Any):
        if getattr(tool, "name", "") != "bash":
            return None
        command = (args or {}).get("command", "")
        try:
            toolbox.guardrails.check_command(command)
        except IrreversibleOperationError as e:
            toolbox.blocked = e
            return {"result": f"denied: irreversible operation blocked ({e.reason})"}
        return None
    return before_tool


def _adk_tools(spec: HarnessTaskSpec, toolbox: AgentToolbox) -> list:
    """ADK introspects a plain typed callable into a tool schema, so the tools
    are thin typed closures over the toolbox. Names/signatures match the
    OpenAI harness's tool set."""

    def bash(command: str) -> str:
        """Run a shell command inside the repository workspace."""
        return toolbox.bash(command)

    def read_file(path: str) -> str:
        """Read a file from the repository workspace."""
        return toolbox.read_file(path)

    def write_file(path: str, content: str) -> str:
        """Create or overwrite a file in the workspace (the answer file in
        read-only mode)."""
        return toolbox.write_file(path, content)

    def edit_file(path: str, old: str, new: str) -> str:
        """Replace the first occurrence of `old` with `new` in a workspace file."""
        return toolbox.edit_file(path, old, new)

    def finish(summary: str, completed: bool, question: str = "",
               clarification_question: str = "", plan_broken: bool = False,
               importance_flags: str = "") -> str:
        """Finish the task. Call exactly once when work is complete or blocked.
        importance_flags is a comma-separated list (auth,payments,migration)."""
        toolbox.record_finish({
            "summary": summary, "completed": completed,
            "question": question, "clarification_question": clarification_question,
            "plan_broken": plan_broken,
            "importance_flags": [f.strip() for f in importance_flags.split(",") if f.strip()],
        })
        return "acknowledged"

    if spec.read_only:
        tools = [write_file, finish]
        if spec.workspace_path:
            tools[0:0] = [bash, read_file]
            if spec.writable_paths:
                # In-place editing inside the writable roots (project-
                # initialization-task spec §7.1); the toolbox denies the rest.
                tools.insert(len(tools) - 1, edit_file)
        return tools
    return [bash, read_file, write_file, edit_file, finish]


class _gemini_key:
    """Overlay the resolved key onto GEMINI_API_KEY/GOOGLE_API_KEY for the run
    (overlay, never replace — an ambient login survives an empty key)."""

    _VARS = ("GEMINI_API_KEY", "GOOGLE_API_KEY")

    def __init__(self, api_key: str):
        self._api_key = api_key
        self._prior: dict[str, str | None] = {}

    def __enter__(self):
        import os
        if self._api_key:
            for var in self._VARS:
                self._prior[var] = os.environ.get(var)
                os.environ[var] = self._api_key
        return self

    def __exit__(self, *exc):
        import os
        for var, prior in self._prior.items():
            if prior is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = prior
        return False

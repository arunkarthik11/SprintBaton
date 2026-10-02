"""Shared tool core for the in-process agent-framework harnesses
(`openai_agent_sdk` / `gemini_agent_sdk` — multi-provider-parity spec §4.1).

The two harnesses wrap two different in-process Python frameworks (OpenAI Agents
SDK, Google ADK), but the *tools* they expose and the guardrail/escalation-signal
semantics behind those tools are identical to each other and to
`claude_agent_sdk` — so that core lives here once, exactly as `subprocess_cli.py`
factors the CLI-wrapper shape `claude_code_cli`/`codex_cli`/`gemini_cli` share.

`AgentToolbox` owns:

- **The five tools** (`bash`/`read_file`/`write_file`/`edit_file`/`finish`) as
  plain Python callables closing over one per-run state object. Each framework
  registers them in its own tool-registration mechanism; the callables never
  know which framework called them.
- **Guardrail enforcement inside the tool** (not only via a framework hook):
  OpenAI Agents SDK's `RunHooks` are observe-only (verified against the SDK
  docs — `on_tool_start` returns `None`, no denial), so `bash` checks
  `guard.check_command` itself and records/raises on a catastrophic command.
  This is *stronger* than a hook, not weaker: SprintBaton owns the tool body, so
  a blocked command never executes. ADK's `before_tool_callback` (which *can*
  deny) is wired additionally in that harness as defense-in-depth, but this
  toolbox is the single enforcement point both frameworks rely on.
- **Read-only vs execution mode** (`read_only`): read-only narrows what is
  *permitted* (Bash allowlist-gated via `check_read_only`, writes scoped to the
  one scratch answer file) without loosening what is *catastrophic*
  (`check_command` still hard-stops). Execution mode drops the narrowing.
- **The escalation signals** the raw loop gets for free by owning dispatch:
  `file_edit_counts` and `consecutive_check_failures`, reconstructed exactly as
  `WorkspaceToolExecutor`/`claude_agent_sdk._RunState` do.
- **writable_paths** (project-initialization-task spec §7.1): in read-only
  mode, `write_file`/`edit_file` are also permitted on targets inside a
  writable root (symlinks followed); nothing else loosens.
"""

from __future__ import annotations

import logging
from pathlib import Path

from sprintbaton.harness.base import (
    GuardrailPolicy,
    HarnessResult,
    HarnessTaskSpec,
    StopReason,
    within_roots,
    workspace_diff,
)
from sprintbaton.harness.raw_tool_loop import (
    BASH_TIMEOUT_SECONDS,
    CHECK_COMMAND_MARKERS,
    MAX_OUTPUT_CHARS,
)
from sprintbaton.models.guard import IrreversibleOperationError
from sprintbaton.sandbox.binding import LOCAL_RUNTIME, BoundWorkspace, open_binding
from sprintbaton.observer.verbosity import TRACE_CONSOLE_CHARS, TRACE_LOGGER_NAME

log = logging.getLogger(__name__)
trace_log = logging.getLogger(TRACE_LOGGER_NAME)


def _is_check_command(command: str) -> bool:
    return any(marker in command for marker in CHECK_COMMAND_MARKERS)


def _trace(kind: str, text: str) -> None:
    if text and trace_log.isEnabledFor(logging.DEBUG):
        trace_log.debug(str(text)[:TRACE_CONSOLE_CHARS], extra={"trace_kind": kind})


class AgentToolbox:
    """Per-run tool state + the five tool callables both agent-framework
    harnesses register. One instance per execute()."""

    def __init__(self, spec: HarnessTaskSpec, *, read_only: bool,
                 output_path: Path | None = None,
                 bound: BoundWorkspace | None = None):
        self.spec = spec
        # Every tool body crosses the sandbox seam (hosted-sandbox-isolation
        # spec §7.1); the guard below runs first, here in the worker. A direct
        # construction (tests) runs under the tool-mode passthrough.
        self.ws = bound or open_binding(LOCAL_RUNTIME, spec)
        self.read_only = read_only
        self.guardrails: GuardrailPolicy = spec.guardrails
        self.root = Path(spec.workspace_path).resolve() if spec.workspace_path else None
        # The one Write target permitted in read-only mode (None in execution).
        self.output_path = output_path
        # Extra Write/Edit roots a read-only run may edit inside (§7.1).
        self.writable_roots = (tuple(Path(p).resolve() for p in spec.writable_paths)
                               if read_only else ())
        self.finish: dict = {}
        self.file_edit_counts: dict[str, int] = {}
        self.consecutive_check_failures = 0
        self.blocked: IrreversibleOperationError | None = None

    # ----------------------------------------------------------- path helpers

    def _cwd(self) -> str:
        if self.root is not None:
            return str(self.root)
        # No workspace (e.g. passing_criteria): confine to the scratch dir.
        return str(self.output_path.parent) if self.output_path else "."

    def _resolve(self, rel_path: str) -> Path:
        base = self.root or (self.output_path.parent if self.output_path else Path.cwd())
        target = (base / rel_path).resolve() if not Path(rel_path).is_absolute() \
            else Path(rel_path).resolve()
        return target

    def _confined(self, target: Path) -> bool:
        if self.output_path is not None and target == self.output_path:
            return True
        if self._writable(target):
            return True
        return self.root is not None and target.is_relative_to(self.root)

    def _writable(self, target: Path) -> bool:
        return bool(self.writable_roots) and within_roots(target, self.writable_roots)

    # ------------------------------------------------------------------ tools

    def bash(self, command: str) -> str:
        """Run a shell command in the workspace. Catastrophic commands hard-stop
        the whole run (EH); in read-only mode a non-allowlisted command is a
        recoverable deny."""
        try:
            self.guardrails.check_command(command)
        except IrreversibleOperationError as e:
            # Record for the after-run check AND raise: the EH hard-stop must
            # end the task, not become a silently-denied call. Frameworks that
            # swallow the exception into a model message are still caught by the
            # harness's post-run `if toolbox.blocked` check.
            self.blocked = e
            raise
        if self.read_only:
            try:
                self.guardrails.check_read_only(command)
            except IrreversibleOperationError as e:
                _trace("tool_result", f"denied: {e.reason}")
                return f"error: read-only run: {e.reason}: {e.command}"
        result = self.ws.shell(command, cwd=self._cwd(),
                               timeout_seconds=BASH_TIMEOUT_SECONDS)
        output = (result.stdout + result.stderr)[-MAX_OUTPUT_CHARS:]
        if result.timed_out:
            output += f"\n(timed out after {BASH_TIMEOUT_SECONDS}s)"
        if _is_check_command(command):
            if result.exit_code != 0:
                self.consecutive_check_failures += 1
            else:
                self.consecutive_check_failures = 0
        _trace("tool_result", output)
        return f"exit code: {result.exit_code}\n{output}"

    def read_file(self, path: str) -> str:
        target = self._resolve(path)
        if not self._confined(target):
            return f"error: path escapes the workspace: {path}"
        text = self.ws.read_text(str(target))
        if text is None:
            return f"error: not a file: {path}"
        return text[:MAX_OUTPUT_CHARS]

    def write_file(self, path: str, content: str) -> str:
        target = self._resolve(path)
        if self.read_only and self._writable(target):
            self.ws.session.write_file(str(target), content.encode())
            return f"wrote {len(content)} chars to {path}"
        if self.read_only:
            if self.output_path is None or target != self.output_path:
                return ("error: read-only run: writes are only permitted to the "
                        "answer file" + self._writable_hint())
            self.ws.session.write_file(str(target), content.encode())
            return f"wrote {len(content)} chars to the answer file"
        if not self._confined(target):
            return f"error: path escapes the workspace: {path}"
        self.ws.session.write_file(str(target), content.encode())
        self._count_edit(target, path)
        return f"wrote {len(content)} chars to {path}"

    def edit_file(self, path: str, old: str, new: str) -> str:
        target = self._resolve(path)
        if self.read_only and not self._writable(target):
            return ("error: read-only run: edit_file is not permitted"
                    + self._writable_hint())
        if not self._confined(target):
            return f"error: path escapes the workspace: {path}"
        text = self.ws.read_text(str(target))
        if text is None:
            return f"error: not a file: {path}"
        if old not in text:
            return f"error: old string not found in {path}"
        self.ws.session.write_file(str(target), text.replace(old, new, 1).encode())
        self._count_edit(target, path)
        return f"edited {path}"

    def _writable_hint(self) -> str:
        if not self.writable_roots:
            return ""
        return " outside " + ", ".join(str(r) for r in self.writable_roots)

    def record_finish(self, payload: dict) -> str:
        # NB: the *attribute* self.finish holds the payload; this is the tool
        # callable — distinct names so neither shadows the other.
        self.finish = dict(payload)
        return "acknowledged"

    def _count_edit(self, target: Path, raw_path: str) -> None:
        try:
            key = str(target.relative_to(self.root)) if self.root else raw_path
        except ValueError:
            key = raw_path
        self.file_edit_counts[key] = self.file_edit_counts.get(key, 0) + 1

    # ---------------------------------------------------------------- results

    def diff(self) -> str:
        return workspace_diff(self.root) if self.root else ""


def build_execution_result(toolbox: AgentToolbox, *, usage, conversation_id):
    """Assemble the execution-mode HarnessResult from the finish payload +
    accumulated signals — the same reduction claude_agent_sdk._run_execution
    does, framework-agnostic."""
    finish = toolbox.finish
    return HarnessResult(
        summary=finish.get("summary", ""),
        completed=bool(finish.get("completed", False)),
        asked_question=finish.get("question") or None,
        clarification_question=finish.get("clarification_question") or None,
        clarification_options=finish.get("clarification_options") or None,
        plan_broken=bool(finish.get("plan_broken", False)),
        importance_flags=list(finish.get("importance_flags", [])),
        files_edited=dict(toolbox.file_edit_counts),
        consecutive_check_failures=toolbox.consecutive_check_failures,
        diff=toolbox.diff(),
        usage=usage,
        conversation_id=conversation_id,
        output_text=finish.get("summary", ""),
    )


def build_read_only_result(toolbox: AgentToolbox, answer: str, *, usage,
                           conversation_id, stop_reason: StopReason = "completed"):
    """Assemble the read-only HarnessResult: the answer travels via the scratch
    file (output_text), the finish payload carries only summary/completed."""
    finish = toolbox.finish
    return HarnessResult(
        summary=finish.get("summary", ""),
        completed=bool(finish.get("completed", False)),
        usage=usage,
        conversation_id=conversation_id,
        output_text=answer,
        stop_reason=stop_reason,
    )

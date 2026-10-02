"""The claude_code_cli harness — a subprocess wrapper around the real `claude`
CLI binary in headless mode (claude-code-cli harness spec §5).

Authenticated by whatever Claude Code login (`claude login`) already exists in
the environment the worker runs in: SprintBaton never reads, stores, or
forwards that credential — the one hard invariant here is that the subprocess
environment NEVER carries ANTHROPIC_API_KEY (spec §5.2/§9.3), so the CLI can
only ever use its own on-disk OAuth session. A drop-in alternative to
claude_agent_sdk for the five read-only advisory roles (finalization/
abstract_finalization/passing_criteria/planning/review): same scratch-answer-
file convention, same guard.py-backed guardrails, same HarnessResult shape.

Flag/JSON contracts, verified against claude CLI 2.1.212 (the spec's §12
verification requirement — it fixed the architecture, not the flag strings):

- `claude -p` with the user message on stdin; `--model <id>`.
- `--output-format json` (not the spec's suggested stream-json): one result
  object carrying `session_id`, `usage` (same key names as the Agent SDK's
  ResultMessage.usage), `is_error`, and `result` (final text) — everything the
  harness needs, with strictly simpler parsing. The answer itself travels via
  the scratch file, so turn-by-turn stream output buys nothing.
- `--resume <session_id>` for conversation resume. Sessions are persisted
  per working directory, so a resume only finds its session when re-run from
  the same cwd — the harness therefore always uses a stable cwd per task: the
  task's workspace when one exists, else a stable per-task scratch directory
  (never the per-run answer-file tempdir, which changes every run).
- `--settings <file>`: a per-run generated settings JSON whose PreToolUse
  hooks shell out to `python -m sprintbaton.cli harness-guard` (spec §5.3),
  which calls the same guard.check_command/check_read_only every other
  harness uses and prints the hook protocol's permissionDecision JSON. A
  catastrophic command additionally drops a marker file in the per-run state
  dir and stops the run (`"continue": false`); the harness raises
  IrreversibleOperationError when it finds the marker — the same EH hard-stop
  semantics as claude_agent_sdk's in-process hook. Note the CLI silently
  ignores an *invalid* settings file in -p mode, so `--tools` below is kept
  as an independent second layer.
- `--setting-sources ""` so the host's user/project/local settings (and their
  hooks) never load — only auth comes from the host login; `--tools` narrowed
  to the read-only set; `--permission-mode bypassPermissions` (irreversibility
  is enforced by the hooks, which run regardless of permission mode).
- No `--max-turns` exists in this CLI version; spec.max_iterations is
  therefore not enforced here (the run is bounded by the subprocess timeout).

writable_paths (project-initialization-task spec §7.1): `--tools` gains Edit
and each root travels to the hook subprocess as a repeated `--writable-root`,
where evaluate_hook_read_only permits Write/Edit inside it. A read-only run
that hits the subprocess timeout returns a `time_limit` stop instead of raising
(§7.2), and a failed read-only run with no answer whose output looks like a
model-API outage raises TransientHarnessError (§5.6) — checked only after
usage-limit detection, so a usage limit is never mistaken for an outage.

Write mode (write-execution spec) — read_only=False, the execution/
conflict_resolution shape — is now implemented as a sibling of the read-only
run (`_run_execution`): the full Read/Write/Edit/Bash/Glob/Grep tool set, a
write-mode guard ruleset (any file write inside the workspace permitted;
check_command's catastrophic denylist still hard-stops; check_read_only's
allowlist does not apply), PostToolUse hooks accumulating files_edited /
consecutive_check_failures into a flock-guarded per-run exec-state file (the
cross-subprocess stand-in for the SDK harness's in-process _RunState), and the
answer file parsed against FINISH_TOOL_SCHEMA for the terminal payload. It is
still gated behind SPRINTBATON_CONFORMANCE_LIVE=1 for its live scenarios (spec
§10) and is never the default coding harness (SPRINTBATON_CODING_HARNESS keeps
raw_tool_loop) — reaching it stays an explicit opt-in, exactly like open_hands.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

from sprintbaton.harness.base import (
    DEFAULT_GUARDRAIL_POLICY,
    FINISH_TOOL_SCHEMA,
    USAGE_LIMIT_SCOPE_SESSION,
    USAGE_LIMIT_SCOPE_WEEKLY,
    GuardrailPolicy,
    HarnessResult,
    HarnessTaskSpec,
    ModelSpec,
    TransientHarnessError,
    UsageLimitSignal,
    looks_transient,
    scratch_cwd_for,
    within_roots,
    workspace_diff,
)
from sprintbaton.harness.claude_agent_sdk import (
    CLAUDE_OAUTH_TOKEN_VAR,
    _PATH_INPUT_KEYS,
    _WRITE_TOOLS,
    CORE_TOOLS,
    FILE_PATH_MATCHER,
    FILE_WRITE_MATCHER,
    MAX_TURNS_SUBTYPE,
    READ_ONLY_TOOLS,
    WRITABLE_ROOT_TOOLS,
    _answer_instructions,
    _bash_check_failed,
    _fs_safe,
    _is_check_command,
    usage_from_dict,
)
from sprintbaton.models.guard import IrreversibleOperationError

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 3600
BLOCKED_MARKER = "guard-blocked.json"
# The cross-subprocess escalation-signal state file (write-execution spec §6):
# file_edit_counts / consecutive_check_failures, which the SDK harness's
# in-process _RunState accumulates for free, must instead survive across the
# separate, memory-less `sprintbaton harness-guard` PostToolUse subprocesses of
# one write-capable run — so they live in this JSON file in the per-run scratch
# dir, flock-guarded (_locked_state) against any concurrent tool call.
EXEC_STATE_FILE = "exec-state.json"
EXEC_STATE_LOCK = "exec-state.lock"
# NotebookEdit is not in FILE_PATH_MATCHER (the SDK harness never offers it)
# but the CLI's built-in set includes it — match it so the deny is explicit.
_HOOK_FILE_MATCHER = f"{FILE_PATH_MATCHER}|NotebookEdit"
_CLI_COMPLETION_NOTE = (
    "That file is your only deliverable — write it, then end your turn. "
    "Do not repeat the answer in your reply text.")
_MAX_SUMMARY_CHARS = 2000


# --- usage-limit detection (usage-limit-aware execution spec §9) -------------
#
# The first concrete UsageLimitSignal producer: this harness draws from a
# Claude Code subscription's session/weekly pools, the one place a run can
# fail for pure availability rather than capability. Pattern-based and
# best-effort — the exact headless signal shape is flagged for live
# verification (spec §12, the same posture the harness spec took for its own
# flags): an unmatched failure stays a plain failure (no signal); a matched
# one carries resets_at when the message embeds the historically observed
# "…usage limit reached|<epoch>" convention.

_USAGE_LIMIT_HINT = re.compile(
    r"usage limit|rate limit|out of (?:\w+ )?usage|limit reached", re.IGNORECASE)
_USAGE_LIMIT_RESET_EPOCH = re.compile(r"limit reached\|(\d{9,13})", re.IGNORECASE)
_WEEKLY_HINT = re.compile(r"week", re.IGNORECASE)
_MAX_DETAIL_CHARS = 500


def detect_usage_limits(result: dict, stderr: str = "") -> list[UsageLimitSignal]:
    """Recognize a usage/rate-limit condition in a *failed* run's result JSON
    or stderr. Callers must only invoke this on failure paths (is_error /
    non-zero exit / unparseable output) — a successful run's text mentioning
    "usage limit" (e.g. a review of this very feature) is not a signal."""
    for text in (str(result.get("result") or ""), stderr or ""):
        if not text or _USAGE_LIMIT_HINT.search(text) is None:
            continue
        scope = (USAGE_LIMIT_SCOPE_WEEKLY if _WEEKLY_HINT.search(text)
                 else USAGE_LIMIT_SCOPE_SESSION)
        resets_at = None
        match = _USAGE_LIMIT_RESET_EPOCH.search(text)
        if match:
            raw = int(match.group(1))
            # The embedded timestamp has been observed in epoch seconds;
            # accept millis too rather than misread one as 1970.
            resets_at = raw if raw >= 10**12 else raw * 1000
        return [UsageLimitSignal(scope=scope, resets_at=resets_at,
                                 detail=text[:_MAX_DETAIL_CHARS])]
    return []


# --- native-hook guard evaluation (spec §5.3) --------------------------------
#
# Runs inside the `sprintbaton harness-guard` hook subprocess, NOT inside the
# harness process: Claude Code's hooks mechanism pipes the PreToolUse input
# JSON to a shell command and reads the decision JSON from its stdout —
# mirroring claude_agent_sdk._RunState's read-only callbacks without the
# in-process callables the process boundary makes impossible. guard.py stays
# the single source of truth for what Bash may run.

def _deny(reason: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        },
    }


def _resolve_path(raw_path: str, root: Path | None) -> Path:
    path = Path(raw_path)
    if not path.is_absolute() and root is not None:
        path = root / path
    return path.resolve()


def _hard_stop_bash(command: str, state_dir: str,
                    guardrails: GuardrailPolicy) -> dict | None:
    """The EH hard-stop shared by both rulesets: a catastrophic Bash command
    drops the blocked marker for the parent harness AND ends the run
    (`continue: false`). Returns the decision dict, or None if the command is
    not catastrophic (callers then apply their mode-specific rules)."""
    try:
        guardrails.check_command(command)
    except IrreversibleOperationError as e:
        (Path(state_dir) / BLOCKED_MARKER).write_text(
            json.dumps({"command": e.command, "reason": e.reason}))
        decision = _deny(str(e))
        decision["continue"] = False
        decision["stopReason"] = f"irreversible operation blocked ({e.reason})"
        return decision
    return None


def evaluate_hook_read_only(hook_input: dict, *, workspace: str, answer_file: str,
                            state_dir: str,
                            guardrails: GuardrailPolicy = DEFAULT_GUARDRAIL_POLICY,
                            writable_roots: tuple[str, ...] | list[str] = ()) -> dict:
    """One PreToolUse decision for a read-only claude_code_cli run — the same
    rules as claude_agent_sdk's read-only mode: Bash gated by check_command
    (catastrophic -> stop the whole run) then check_read_only (recoverable
    deny); Write only to the answer file — or Write/Edit inside a writable root
    (project-initialization-task spec §7.1); every other file tool confined to
    the workspace (plus the always-readable answer file and writable roots)."""
    tool_name = hook_input.get("tool_name", "")
    tool_input = hook_input.get("tool_input") or {}
    root = Path(workspace).resolve() if workspace else None
    answer_path = Path(answer_file).resolve()
    roots = tuple(Path(r) for r in writable_roots if r)

    if tool_name == "Bash":
        command = tool_input.get("command", "")
        # The EH hard-stop: a catastrophic command marks the run blocked (the
        # parent harness raises IrreversibleOperationError on the marker) AND
        # ends it — a silently-denied call the loop continues past is not a stop.
        hard_stop = _hard_stop_bash(command, state_dir, guardrails)
        if hard_stop is not None:
            return hard_stop
        try:
            guardrails.check_read_only(command)
        except IrreversibleOperationError as e:
            # Recoverable: deny this one call, the agent may retry compliantly.
            return _deny(f"read-only run: {e.reason}: {e.command}")
        return {}

    if tool_name in _WRITE_TOOLS:
        raw = tool_input.get("file_path", "")
        target = _resolve_path(str(raw), root) if raw else None
        if (target is not None and tool_name in ("Write", "Edit") and roots
                and within_roots(target, roots)):
            return {}
        if tool_name != "Write":
            return _deny(f"read-only run: {tool_name} is not permitted")
        if target is None or target != answer_path:
            return _deny(f"read-only run: writes are only permitted to the "
                         f"answer file {answer_path}")
        return {}

    # Read/Glob/Grep: workspace confinement, answer file and writable roots
    # always readable.
    for key in _PATH_INPUT_KEYS:
        raw = tool_input.get(key)
        if not raw:
            continue
        target = _resolve_path(str(raw), root)
        if target == answer_path:
            continue
        if roots and within_roots(target, roots):
            continue
        if root is None or not target.is_relative_to(root):
            return _deny(f"path escapes the workspace: {raw}")
    return {}


# --- write-mode guard evaluation (write-execution spec §5) -------------------
#
# The Coding Model's own write-capable role: mirrors _RunState.pre_bash /
# pre_file (execution mode) — Bash gated by check_command only (catastrophic
# hard-stops, everything else runs; check_read_only's narrower allowlist does
# NOT apply), and any file write inside the workspace permitted; only escaping
# the workspace root is denied.

def evaluate_hook_execution(hook_input: dict, *, workspace: str, state_dir: str,
                            guardrails: GuardrailPolicy = DEFAULT_GUARDRAIL_POLICY) -> dict:
    """One PreToolUse decision for a write-capable claude_code_cli run."""
    tool_name = hook_input.get("tool_name", "")
    tool_input = hook_input.get("tool_input") or {}
    root = Path(workspace).resolve() if workspace else None

    if tool_name == "Bash":
        command = tool_input.get("command", "")
        # The one guardrail write mode does NOT relax — read-only mode narrows
        # what's permitted, it does not loosen what's catastrophic.
        hard_stop = _hard_stop_bash(command, state_dir, guardrails)
        if hard_stop is not None:
            return hard_stop
        return {}

    # Write/Edit/NotebookEdit and Read/Glob/Grep alike: any path inside the
    # workspace is fair game in write mode; only escaping the root is denied.
    for key in _PATH_INPUT_KEYS:
        raw = tool_input.get(key)
        if not raw:
            continue
        target = _resolve_path(str(raw), root)
        if root is None or not target.is_relative_to(root):
            return _deny(f"path escapes the workspace: {raw}")
    return {}


@contextmanager
def _locked_state(state_dir: str):
    """Read-modify-write the per-run exec-state file under an advisory flock
    (write-execution spec §6) — the same OS primitive storage/lock_fs.py uses,
    narrowed to guard against a genuinely concurrent tool call within one turn
    (unconfirmed for this CLI version, §14; free when calls are sequential)."""
    sdir = Path(state_dir)
    sdir.mkdir(parents=True, exist_ok=True)
    state_path = sdir / EXEC_STATE_FILE
    with open(sdir / EXEC_STATE_LOCK, "w") as lock_fd:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            try:
                state = json.loads(state_path.read_text())
            except (FileNotFoundError, json.JSONDecodeError):
                state = {}
            yield state
            state_path.write_text(json.dumps(state))
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)


def _workspace_relative(raw_path: str, workspace: str) -> str:
    root = Path(workspace).resolve() if workspace else None
    path = Path(raw_path)
    target = path if path.is_absolute() else (root / path if root else path)
    if root is None:
        return raw_path
    try:
        return str(target.resolve().relative_to(root))
    except ValueError:
        return raw_path


def evaluate_hook_post(hook_input: dict, *, state_dir: str, workspace: str) -> dict:
    """PostToolUse for a write-capable run: accumulate the escalation signals
    (consecutive_check_failures on Bash, file_edit_counts on Write/Edit) into
    the flock-guarded exec-state file, the cross-subprocess stand-in for the
    SDK harness's in-process _RunState. Decisions are advisory-only — always
    allow — so the return is empty regardless."""
    tool_name = hook_input.get("tool_name", "")
    with _locked_state(state_dir) as state:
        if tool_name == "Bash":
            command = (hook_input.get("tool_input") or {}).get("command", "")
            if _is_check_command(command):
                failed = _bash_check_failed(hook_input.get("tool_response"))
                state["consecutive_check_failures"] = (
                    state.get("consecutive_check_failures", 0) + 1 if failed else 0)
        elif tool_name in ("Write", "Edit"):
            raw = (hook_input.get("tool_input") or {}).get("file_path", "")
            if raw:
                key = _workspace_relative(str(raw), workspace)
                edits = state.setdefault("file_edit_counts", {})
                edits[key] = edits.get(key, 0) + 1
    return {}


# --- the harness --------------------------------------------------------------


class ClaudeCodeCliHarness:
    name = "claude_code_cli"
    # Subscription-only by construction: _env() strips ANTHROPIC_API_KEY
    # unconditionally (the compliance invariant), so a metered key resolved
    # for this harness would be silently discarded. Declaring it
    # metered-incapable makes an ineligible owner a loud failure instead
    # (auth-mode-resolution spec §3).
    supports_subscription_auth = True
    supports_metered_auth = False
    # Unlike codex_cli/gemini_cli the CLI accepts a token from its env, so an
    # owner's stored subscription Credential can drive it in tool mode
    # (hosted-sandbox-isolation spec §9.2).
    subscription_token_var = CLAUDE_OAUTH_TOKEN_VAR
    # Read-only runs honor writable_paths via the hook subprocess
    # (project-initialization-task spec §7.1).
    supports_writable_paths = True
    # The CLI runs its own tools as local subprocesses, so it can only ever
    # run on the tool-mode passthrough and is refused wherever runs must be
    # isolated (hosted-sandbox-isolation spec §7.3).
    sandbox_mode = "unsupported"

    def __init__(self, binary: str = "claude", config_dir: str = "",
                 timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
                 scratch_root: str = ""):
        self._binary = binary
        # Optional isolation of *which* `claude login` session the subprocess
        # uses, via CLAUDE_CONFIG_DIR. Per-user threading of this is the
        # spec's flagged-unsolved multi-admin gap (§12) — the plumbing exists,
        # nothing populates it per user yet.
        self._config_dir = config_dir
        self._timeout = timeout_seconds
        # Where the stable per-task cwd for workspace-less runs lives — a
        # session can only be resumed from the directory it started in, so
        # the cwd must survive across runs of the same task.
        self._scratch_root = Path(scratch_root or tempfile.gettempdir())

    def execute(self, spec: HarnessTaskSpec, model: ModelSpec) -> HarnessResult:
        if model.provider != "anthropic":
            raise ValueError(
                f"claude_code_cli only supports provider 'anthropic', got {model.provider!r}")
        # Branch on read_only exactly as ClaudeAgentSdkHarness does — write mode
        # (execution/conflict_resolution) is the write-execution spec's one
        # change; still conformance-gated behind SPRINTBATON_CONFORMANCE_LIVE=1
        # for its live scenarios (spec §10), never the default coding harness.
        return (self._run_read_only(spec, model) if spec.read_only
                else self._run_execution(spec, model))

    def _run_read_only(self, spec: HarnessTaskSpec, model: ModelSpec) -> HarnessResult:
        prefix = (f"sprintbaton-out-{_fs_safe(spec.task_id)}-" if spec.task_id
                  else "sprintbaton-out-")
        with tempfile.TemporaryDirectory(prefix=prefix) as tmp_dir:
            output_path = (Path(tmp_dir) / "answer.txt").resolve()
            settings_path = Path(tmp_dir) / "settings.json"
            settings_path.write_text(json.dumps(
                self._hook_settings(spec.workspace_path, output_path, tmp_dir,
                                    writable_paths=spec.writable_paths)))
            user_message = spec.user_message + _answer_instructions(
                output_path, spec.output_schema, completion_note=_CLI_COMPLETION_NOTE)

            try:
                completed = subprocess.run(
                    self._command(spec, model, settings_path),
                    input=user_message,
                    cwd=self._cwd(spec),
                    env=self._env(spec, model),
                    capture_output=True, text=True, timeout=self._timeout,
                )
            except subprocess.TimeoutExpired:
                self._raise_if_blocked(tmp_dir)
                # A wall-clock cut-off is a stop, not a failure (project-
                # initialization-task spec §7.2): an in-place-editing run's
                # partial on-disk work is kept for a continuation.
                log.warning("claude_code_cli read-only run hit its timeout",
                            extra={"task_id": spec.task_id,
                                   "timeout_seconds": self._timeout})
                return HarnessResult(
                    output_text=(output_path.read_text()
                                 if output_path.exists() else ""),
                    stop_reason="time_limit",
                )

            self._raise_if_blocked(tmp_dir)

            result = self._parse_result(completed, spec)
            answer = output_path.read_text() if output_path.exists() else ""
            usage_limits = self._usage_limits(result, completed, spec)
            failed = bool(result.get("is_error") or completed.returncode != 0
                          or not result)
            stop_reason = "completed"
            if result.get("subtype") == MAX_TURNS_SUBTYPE:
                stop_reason = "turn_limit"
            elif failed and not usage_limits:
                stop_reason = "error"
                if not answer and looks_transient(
                        str(result.get("result") or ""), completed.stderr or ""):
                    raise TransientHarnessError(
                        "claude_code_cli run failed on a transient error: "
                        f"{(str(result.get('result') or '') or completed.stderr or '')[:500]}")
            if not answer and not usage_limits:
                # Same-shape failure to malformed single_shot output: the
                # calling agent's parse_json_output errors loudly on it.
                log.warning("claude_code_cli run finished with no answer file",
                            extra={"task_id": spec.task_id,
                                   "workspace": spec.workspace_path})
            return HarnessResult(
                summary=str(result.get("result") or "")[:_MAX_SUMMARY_CHARS],
                completed=bool(answer) and not result.get("is_error", False),
                usage=usage_from_dict(result.get("usage")),
                conversation_id=result.get("session_id"),
                output_text=answer,
                usage_limits=usage_limits,
                stop_reason=stop_reason,
            )

    def _raise_if_blocked(self, state_dir: str) -> None:
        """Same semantics as the SDK harness's aborted stream: the EH hard-stop
        propagates as the exception, unattended, no retry."""
        blocked = Path(state_dir) / BLOCKED_MARKER
        if blocked.exists():
            data = json.loads(blocked.read_text())
            raise IrreversibleOperationError(data["command"], data["reason"])

    def _run_execution(self, spec: HarnessTaskSpec, model: ModelSpec) -> HarnessResult:
        """The write-capable sibling of _run_read_only (write-execution spec
        §4): same scratch-tempdir/subprocess/blocked-marker/usage-limit shape,
        differing only in the wider tool set + write-mode hooks (§5, §8), the
        cross-subprocess exec-state file the PostToolUse hooks accumulate into
        (§6), and always parsing the answer file against FINISH_TOOL_SCHEMA (§7)
        — execution never sets spec.output_schema, so nothing is lost."""
        prefix = (f"sprintbaton-out-{_fs_safe(spec.task_id)}-" if spec.task_id
                  else "sprintbaton-out-")
        with tempfile.TemporaryDirectory(prefix=prefix) as tmp_dir:
            output_path = (Path(tmp_dir) / "answer.txt").resolve()
            settings_path = Path(tmp_dir) / "settings.json"
            settings_path.write_text(json.dumps(
                self._hook_settings_execution(spec.workspace_path, tmp_dir)))
            user_message = spec.user_message + _answer_instructions(
                output_path, FINISH_TOOL_SCHEMA, completion_note=_CLI_COMPLETION_NOTE)

            completed = subprocess.run(
                self._command_execution(spec, model, settings_path),
                # Execution/conflict_resolution always have a workspace, so the
                # cwd is never the scratch-dir fallback — the stable-per-task-cwd
                # requirement for --resume is met with no extra logic.
                input=user_message,
                cwd=spec.workspace_path,
                env=self._env(spec, model),
                capture_output=True, text=True, timeout=self._timeout,
            )

            blocked = Path(tmp_dir) / BLOCKED_MARKER
            if blocked.exists():
                data = json.loads(blocked.read_text())
                raise IrreversibleOperationError(data["command"], data["reason"])

            result = self._parse_result(completed, spec)
            finish = self._parse_finish_payload(output_path, spec)
            state = self._read_exec_state(tmp_dir)
            usage_limits = self._usage_limits(result, completed, spec)
            return HarnessResult(
                summary=finish.get("summary", ""),
                completed=bool(finish.get("completed", False)),
                asked_question=finish.get("question") or None,
                clarification_question=finish.get("clarification_question") or None,
                clarification_options=finish.get("clarification_options") or None,
                plan_broken=bool(finish.get("plan_broken", False)),
                importance_flags=list(finish.get("importance_flags", [])),
                files_edited=dict(state.get("file_edit_counts", {})),
                consecutive_check_failures=int(
                    state.get("consecutive_check_failures", 0) or 0),
                diff=workspace_diff(spec.workspace_path),
                usage=usage_from_dict(result.get("usage")),
                conversation_id=result.get("session_id"),
                output_text=finish.get("summary", ""),
                usage_limits=usage_limits,
            )

    def _usage_limits(self, result: dict, completed: subprocess.CompletedProcess,
                      spec: HarnessTaskSpec) -> list[UsageLimitSignal]:
        """Failure-path usage-limit detection, shared by both run modes — the
        single choke point every subprocess invocation flows through (usage-
        limit spec §9.2). A successful run is never inspected."""
        if not (result.get("is_error") or completed.returncode != 0 or not result):
            return []
        signals = detect_usage_limits(result, completed.stderr)
        if signals:
            log.info("claude_code_cli hit a usage limit", extra={
                "task_id": spec.task_id,
                "scope": signals[0].scope,
                "resets_at": signals[0].resets_at,
            })
        return signals

    def _parse_finish_payload(self, output_path: Path,
                              spec: HarnessTaskSpec) -> dict:
        """The write-mode answer file is a FINISH_TOOL_SCHEMA-shaped JSON
        payload (§7). A missing/unparseable file degrades to an incomplete
        run — the same shape a stalled read-only run's empty answer takes."""
        if not output_path.exists():
            log.warning("claude_code_cli execution run produced no finish payload",
                        extra={"task_id": spec.task_id,
                               "workspace": spec.workspace_path})
            return {}
        try:
            payload = json.loads(output_path.read_text())
        except (json.JSONDecodeError, TypeError):
            log.warning("claude_code_cli execution finish payload was not JSON",
                        extra={"task_id": spec.task_id})
            return {}
        return payload if isinstance(payload, dict) else {}

    def _read_exec_state(self, state_dir: str) -> dict:
        try:
            return json.loads((Path(state_dir) / EXEC_STATE_FILE).read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _command(self, spec: HarnessTaskSpec, model: ModelSpec,
                 settings_path: Path) -> list[str]:
        # No workspace -> nothing to browse: only the scratch-file Write is
        # offered, mirroring claude_agent_sdk's degradation (spec §5.2).
        tools = list(READ_ONLY_TOOLS) if spec.workspace_path else ["Write"]
        if spec.workspace_path and spec.writable_paths:
            tools += WRITABLE_ROOT_TOOLS  # gated to the roots by the hook (§7.1)
        cmd = [
            self._binary, "-p",
            "--model", model.model_id,
            "--output-format", "json",
            # Irreversibility is enforced by the PreToolUse hooks, which run
            # regardless of permission mode — never block on a prompt.
            "--permission-mode", "bypassPermissions",
            "--settings", str(settings_path),
            # Never the host's user/project settings (or their hooks) — only
            # the login session is inherited, everything else is per-run.
            "--setting-sources", "",
            "--tools", ",".join(tools),
            "--system-prompt", spec.system_prompt,
        ]
        if spec.conversation_id:
            cmd += ["--resume", spec.conversation_id]
        return cmd

    def _command_execution(self, spec: HarnessTaskSpec, model: ModelSpec,
                           settings_path: Path) -> list[str]:
        # The full write-capable tool set (write-execution spec §8) — mirrors
        # claude_agent_sdk's CORE_TOOLS (NotebookEdit deliberately omitted, §14).
        cmd = [
            self._binary, "-p",
            "--model", model.model_id,
            "--output-format", "json",
            "--permission-mode", "bypassPermissions",
            "--settings", str(settings_path),
            "--setting-sources", "",
            "--tools", ",".join(CORE_TOOLS),
            "--system-prompt", spec.system_prompt,
        ]
        if spec.conversation_id:
            cmd += ["--resume", spec.conversation_id]
        return cmd

    def _cwd(self, spec: HarnessTaskSpec) -> str:
        """A cwd that is stable across every run of the same task — the CLI
        persists sessions per working directory, so --resume only works when
        re-run from where the session started."""
        if spec.workspace_path:
            return spec.workspace_path
        cwd = scratch_cwd_for(self._scratch_root, self.name, spec.task_id)
        cwd.mkdir(parents=True, exist_ok=True)
        return str(cwd)

    def _env(self, spec: HarnessTaskSpec | None = None,
             model: ModelSpec | None = None) -> dict[str, str]:
        """The subprocess environment: the host's, minus ANTHROPIC_API_KEY —
        the hard invariant (spec §9.3). An API key in the env would flip the
        CLI to metered billing, and forwarding one is precisely the
        credential-routing this harness exists not to be.

        A subscription token is passed only for a subscription-authed call:
        the owner's own stored token (ModelSpec.auth_token, hosted-sandbox-
        isolation spec §9.2) when one resolved, else whatever the host's login
        session provides. This dict is passed to subprocess.run(env=...),
        which *replaces* the child environment, so removal genuinely removes.
        """
        env = {k: v for k, v in os.environ.items()
               if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
        if spec is not None and not spec.subscription_auth:
            env.pop(CLAUDE_OAUTH_TOKEN_VAR, None)
        elif model is not None and model.auth_token:
            env[CLAUDE_OAUTH_TOKEN_VAR] = model.auth_token
        if self._config_dir:
            env["CLAUDE_CONFIG_DIR"] = self._config_dir
        return env

    def _hook_settings(self, workspace_path: str, output_path: Path,
                       state_dir: str,
                       writable_paths: tuple[str, ...] = ()) -> dict:
        """The per-run settings JSON wiring PreToolUse into `sprintbaton
        harness-guard` (spec §5.3) — the hook subprocess imports guard.py
        directly, so both harnesses' guardrail behavior stays backed by the
        one module. Each writable root rides as a repeated --writable-root
        (project-initialization-task spec §7.1)."""
        parts = [
            sys.executable, "-m", "sprintbaton.cli", "harness-guard",
            "--workspace", workspace_path,
            "--answer-file", str(output_path),
            "--state-dir", state_dir,
        ]
        for root in writable_paths:
            parts += ["--writable-root", str(root)]
        guard_cmd = " ".join(shlex.quote(part) for part in parts)
        hook = [{"type": "command", "command": guard_cmd}]
        return {"hooks": {"PreToolUse": [
            {"matcher": "Bash", "hooks": hook},
            {"matcher": _HOOK_FILE_MATCHER, "hooks": hook},
        ]}}

    def _hook_settings_execution(self, workspace_path: str,
                                 state_dir: str) -> dict:
        """The write-mode settings JSON (write-execution spec §5/§6): PreToolUse
        for the guard ruleset (via --mode execution) AND PostToolUse for the
        escalation-signal accumulation. No --answer-file — write mode does not
        confine writes to it; the hook subprocess routes on the CLI's own
        hook_event_name field, so one guard command serves both events."""
        guard_cmd = " ".join(shlex.quote(part) for part in [
            sys.executable, "-m", "sprintbaton.cli", "harness-guard",
            "--workspace", workspace_path,
            "--state-dir", state_dir,
            "--mode", "execution",
        ])
        hook = [{"type": "command", "command": guard_cmd}]
        return {"hooks": {
            "PreToolUse": [
                {"matcher": "Bash", "hooks": hook},
                {"matcher": _HOOK_FILE_MATCHER, "hooks": hook},
            ],
            "PostToolUse": [
                {"matcher": "Bash", "hooks": hook},
                {"matcher": FILE_WRITE_MATCHER, "hooks": hook},
            ],
        }}

    def _parse_result(self, completed: subprocess.CompletedProcess,
                      spec: HarnessTaskSpec) -> dict:
        if completed.returncode != 0:
            log.warning("claude_code_cli exited non-zero", extra={
                "task_id": spec.task_id, "returncode": completed.returncode,
                "stderr": completed.stderr[-2000:],
            })
        try:
            result = json.loads(completed.stdout)
        except (json.JSONDecodeError, TypeError):
            log.warning("claude_code_cli produced no parseable result JSON",
                        extra={"task_id": spec.task_id,
                               "stdout": completed.stdout[-2000:]})
            return {}
        return result if isinstance(result, dict) else {}

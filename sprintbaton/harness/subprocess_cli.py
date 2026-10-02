"""Shared base for subprocess-wrapper harnesses around an already-installed,
already-authenticated coding CLI (provider-registration spec §6.1).

`claude_code_cli` established the shape; `codex_cli` and `gemini_cli` are the
second and third copies of it, differing only in the binary, the non-interactive
invocation flags, and the result parsing.

Guardrail posture (subprocess-cli-write-parity-and-advisories spec §4): both
CLIs now expose native pre-tool hooks, so guardrail enforcement is no longer
prompt-advisory — a write-capable run wires the CLI's own hook mechanism into
`sprintbaton harness-guard`, which applies the same `models/guard.py` checks
every other write-capable role runs. The per-CLI differences (event names,
tool-name vocabulary, decision shape, where the config lives) are captured once
in `HookDialect` rather than copied per subclass.

Two properties of the hook config are load-bearing and must not be "simplified"
away (spec §4.3):

  * **It is byte-identical across runs.** Run-specific state (workspace, state
    dir, mode) travels in the subprocess environment, never baked into the
    command string. Codex records hook trust against a hash of the definition,
    so a config that varied per run would be re-flagged for review — and
    silently skipped — every single time.
  * **It is inert outside a SprintBaton run.** With no SPRINTBATON_GUARD_MODE
    in the environment the guard allows immediately. Codex's hook must live at
    the user layer (see `HookDialect.config_scope`), where it would otherwise
    fire during the user's own interactive sessions.

This module never passes `--dangerously-bypass-hook-trust` (spec §2.3). The flag
is per-invocation and unscopable, so it would also activate user-level hooks the
user reviewed and deliberately declined to trust. A test asserts it appears
nowhere in the codebase.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from sprintbaton.harness.base import (
    FINISH_TOOL_SCHEMA,
    HarnessResult,
    HarnessTaskSpec,
    ModelSpec,
    UsageLimitSignal,
    scratch_cwd_for,
)
from sprintbaton.harness.advisories import EXECUTION, READ_ONLY
from sprintbaton.harness.claude_agent_sdk import _answer_instructions, _fs_safe
from sprintbaton.harness.claude_code_cli import (
    BLOCKED_MARKER,
    EXEC_STATE_FILE,
    _locked_state,
    detect_usage_limits,
    evaluate_hook_execution,
    evaluate_hook_post,
    evaluate_hook_read_only,
)
from sprintbaton.models.guard import IrreversibleOperationError

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 3600
_MAX_SUMMARY_CHARS = 2000

_ADVISORY_GUARD_NOTE = (
    "You are running read-only: do not modify, create, move, or delete any "
    "file except the single answer file named below, and never run a "
    "destructive or irreversible command.")

# --- the guard environment channel (spec §4.3) -------------------------------
#
# Run state reaches the hook subprocess through the environment rather than the
# hook command string, so the config file's content — and therefore its trust
# hash — never varies. The hook process is a grandchild of ours (we spawn the
# CLI, the CLI spawns the hook), so it inherits this.

GUARD_MODE_VAR = "SPRINTBATON_GUARD_MODE"
GUARD_WORKSPACE_VAR = "SPRINTBATON_GUARD_WORKSPACE"
GUARD_STATE_DIR_VAR = "SPRINTBATON_GUARD_STATE_DIR"
GUARD_ANSWER_FILE_VAR = "SPRINTBATON_GUARD_ANSWER_FILE"
GUARD_WRITABLE_ROOTS_VAR = "SPRINTBATON_GUARD_WRITABLE_ROOTS"
# Set only by the capability probe (spec §7.2): the hook writes a breadcrumb
# here to prove it executed at all.
GUARD_PROBE_FILE_VAR = "SPRINTBATON_GUARD_PROBE_FILE"


@dataclass(frozen=True)
class HookDialect:
    """One CLI's native hook mechanism (spec §4.1).

    `claude_code_cli` is deliberately NOT refactored onto this table — rewriting
    a conformance-passed guardrail path to prove a shared abstraction is exactly
    the churn the write-execution spec avoided.
    """

    name: str                       # --dialect value
    pre_event: str
    post_event: str
    # Where the hook config lives, and who writes it:
    #   "workspace_per_run" — written into the run's own clone, torn down after.
    #   "user_once"         — merged into the user's own config by
    #                         `sprintbaton providers add`, trusted once, then
    #                         left alone. Forced for Codex: trust is keyed on the
    #                         config file's ABSOLUTE PATH, and a per-task clone
    #                         path can never accumulate a trust record (§2.2).
    config_scope: str
    config_relpath: str             # relative to the workspace, or to ~
    # Matchers in this CLI's own syntax.
    bash_matcher: str
    file_matcher: str
    write_matcher: str
    # CLI tool name -> the canonical name guard evaluation speaks.
    tool_name_map: dict[str, str] = field(default_factory=dict)
    # CLI tool_input key -> canonical key (guard reads `command`, `file_path`,
    # `path`, `notebook_path`).
    input_key_map: dict[str, str] = field(default_factory=dict)
    # Deny is rendered in this CLI's shape; see `render_decision`.
    deny_style: str = "permission_decision"   # | "decision_reason"
    # Upstream enforces a deny on file-write tools. False => the run needs the
    # detect-and-abort backstop (spec §5), which is not optional.
    enforces_write_deny: bool = True
    # Upstream is known to sometimes not execute hooks at all, so this install
    # must be measured before write mode is trusted (spec §7).
    needs_capability_probe: bool = False


CODEX_DIALECT = HookDialect(
    name="codex",
    pre_event="PreToolUse",
    post_event="PostToolUse",
    # openai/codex#32491: `codex exec` skips hooks recorded as trusted, so trust
    # must be established once against a stable path — the user layer.
    config_scope="user_once",
    config_relpath=".codex/hooks.json",
    bash_matcher="Bash",
    file_matcher="apply_patch|Edit|Write|Read|Glob|Grep",
    write_matcher="apply_patch|Edit|Write",
    tool_name_map={"apply_patch": "Edit"},
    deny_style="permission_decision",
    # openai/codex#27833: deny is not enforced for apply_patch.
    enforces_write_deny=False,
    needs_capability_probe=True,
)

GEMINI_DIALECT = HookDialect(
    name="gemini",
    pre_event="BeforeTool",
    post_event="AfterTool",
    # No per-hook hash trust, so a per-task path costs nothing and nothing
    # outside the disposable clone is ever touched.
    config_scope="workspace_per_run",
    config_relpath=".gemini/settings.json",
    bash_matcher="run_shell_command",
    file_matcher="read_file|write_file|replace|glob|search_file_content|list_directory",
    write_matcher="write_file|replace",
    tool_name_map={
        "run_shell_command": "Bash",
        "read_file": "Read",
        "write_file": "Write",
        "replace": "Edit",
        "glob": "Glob",
        "search_file_content": "Grep",
        "list_directory": "Read",
    },
    input_key_map={"absolute_path": "path", "file": "file_path"},
    deny_style="decision_reason",
    enforces_write_deny=True,
    needs_capability_probe=False,
)

DIALECTS: dict[str, HookDialect] = {
    CODEX_DIALECT.name: CODEX_DIALECT,
    GEMINI_DIALECT.name: GEMINI_DIALECT,
}


# --- dialect translation (spec §4.2) -----------------------------------------

def normalize_hook_input(raw: dict, dialect: HookDialect) -> dict:
    """A CLI's hook payload -> the internal shape guard evaluation speaks.

    One guard implementation, three wire formats — this is the whole reason the
    dialect table is worth having. Field names are read defensively (snake_case
    and camelCase both accepted) because only Claude Code's payload shape is
    confirmed against a live binary (spec §10).
    """
    def pick(*names: str):
        for name in names:
            if name in raw:
                return raw[name]
        return None

    tool_name = pick("tool_name", "toolName") or ""
    tool_input = pick("tool_input", "toolInput") or {}
    event = pick("hook_event_name", "hookEventName", "event_name", "event") or ""

    tool_name = dialect.tool_name_map.get(tool_name, tool_name)
    if dialect.input_key_map and isinstance(tool_input, dict):
        tool_input = {dialect.input_key_map.get(k, k): v
                      for k, v in tool_input.items()}

    # Canonicalize the event to Claude Code's vocabulary, which the guard
    # functions and the CLI dispatch already route on.
    if event == dialect.post_event:
        event = "PostToolUse"
    elif event == dialect.pre_event:
        event = "PreToolUse"

    return {
        "tool_name": tool_name,
        "tool_input": tool_input,
        "hook_event_name": event,
        "tool_response": pick("tool_response", "toolResponse"),
    }


def render_decision(decision: dict, dialect: HookDialect) -> tuple[dict, int]:
    """An internal guard decision -> (dialect-shaped payload, exit code).

    Exit code 2 accompanies every deny so both documented channels are used —
    Gemini honors either; Codex honors neither for writes (#27833), which is
    what the §5 backstop is for.
    """
    inner = decision.get("hookSpecificOutput") or {}
    denied = inner.get("permissionDecision") == "deny"
    if not denied:
        return {}, 0

    reason = inner.get("permissionDecisionReason", "denied by SprintBaton guard")
    if dialect.deny_style == "decision_reason":
        payload: dict = {"decision": "deny", "reason": reason}
    else:
        payload = {
            "hookSpecificOutput": {
                "hookEventName": dialect.pre_event,
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            },
        }
    # A catastrophic command ends the run, not just the call.
    if decision.get("continue") is False:
        payload["continue"] = False
        payload["stopReason"] = decision.get("stopReason", reason)
    return payload, 2


_WRITE_TOOL_NAMES = frozenset({"Write", "Edit", "NotebookEdit"})


def _target_path(tool_input: dict) -> str:
    for key in ("file_path", "path", "notebook_path"):
        raw = tool_input.get(key)
        if raw:
            return str(raw)
    return ""


def evaluate_dialect_hook(raw: dict, dialect: HookDialect, *, mode: str,
                          workspace: str, state_dir: str, answer_file: str = "",
                          writable_roots: tuple[str, ...] = ()) -> tuple[dict, int]:
    """The whole hook path for a non-Claude CLI: normalize, apply the shared
    guard, render. Called by `sprintbaton harness-guard --dialect`.

    On a dialect that does not enforce write denies, this also drives the
    detect-and-abort backstop (spec §5.1): a denied write path is recorded on
    the pre-hook, and a post-hook for that same path means the write happened
    anyway — which drops the violation marker the harness raises on.
    """
    hook_input = normalize_hook_input(raw, dialect)
    tool_name = hook_input["tool_name"]
    tool_input = hook_input["tool_input"] if isinstance(
        hook_input["tool_input"], dict) else {}

    if hook_input["hook_event_name"] == "PostToolUse":
        if (not dialect.enforces_write_deny and tool_name in _WRITE_TOOL_NAMES):
            target = _target_path(tool_input)
            if target and target in _denied_writes(state_dir):
                (Path(state_dir) / DENY_VIOLATION_MARKER).write_text(
                    json.dumps({"path": target, "tool": tool_name}))
        evaluate_hook_post(hook_input, state_dir=state_dir, workspace=workspace)
        return {}, 0

    if mode == "execution":
        decision = evaluate_hook_execution(
            hook_input, workspace=workspace, state_dir=state_dir)
    else:
        decision = evaluate_hook_read_only(
            hook_input, workspace=workspace, answer_file=answer_file,
            state_dir=state_dir, writable_roots=writable_roots)

    payload, exit_code = render_decision(decision, dialect)
    if (exit_code and not dialect.enforces_write_deny
            and tool_name in _WRITE_TOOL_NAMES):
        target = _target_path(tool_input)
        if target:
            _record_denied_write(state_dir, target)
    return payload, exit_code


def _denied_writes(state_dir: str) -> set[str]:
    try:
        state = json.loads((Path(state_dir) / EXEC_STATE_FILE).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return set()
    return set(state.get("denied_writes") or [])


def _record_denied_write(state_dir: str, target: str) -> None:
    with _locked_state(state_dir) as state:
        denied = state.setdefault("denied_writes", [])
        if target not in denied:
            denied.append(target)


# --- hook config construction ------------------------------------------------

def guard_command() -> str:
    """The hook command — static by construction (spec §4.3). `sys.executable`
    is an absolute path, but it is constant for an install, so the content (and
    Codex's trust hash) is stable. Moving the venv means re-running
    `sprintbaton providers add`."""
    return " ".join(shlex.quote(p) for p in
                    [sys.executable, "-m", "sprintbaton.cli", "harness-guard"])


def build_hook_config(dialect: HookDialect) -> dict:
    """The hook config for a dialect. Takes no run-specific argument — that is
    the point (spec §4.3)."""
    hook = [{"type": "command",
             "command": f"{guard_command()} --dialect {dialect.name}"}]
    return {"hooks": {
        dialect.pre_event: [
            {"matcher": dialect.bash_matcher, "hooks": hook},
            {"matcher": dialect.file_matcher, "hooks": hook},
        ],
        dialect.post_event: [
            {"matcher": dialect.bash_matcher, "hooks": hook},
            {"matcher": dialect.write_matcher, "hooks": hook},
        ],
    }}


def is_sprintbaton_hook(entry: dict) -> bool:
    """Whether a hook entry in a user's own config is one of ours — the key to
    an idempotent merge that never touches the user's entries."""
    for h in entry.get("hooks") or []:
        if "sprintbaton.cli" in str(h.get("command", "")):
            return True
    return False


def merge_hook_config(existing: dict, dialect: HookDialect) -> dict:
    """Additively merge our hook into a user's own config (spec §4.4).

    The user's entries are preserved untouched; only SprintBaton's own previous
    entry is replaced, so re-running `providers add` is idempotent rather than
    accumulating duplicates.
    """
    merged = json.loads(json.dumps(existing)) if existing else {}
    hooks = merged.setdefault("hooks", {})
    ours = build_hook_config(dialect)["hooks"]
    for event, entries in ours.items():
        current = [e for e in hooks.get(event, []) if not is_sprintbaton_hook(e)]
        hooks[event] = current + entries
    return merged


@contextmanager
def _workspace_hook_config(dialect: HookDialect, workspace: str):
    """Write the per-run hook config into the workspace, restoring whatever was
    there before (spec §4.4). Clobbering a developer's own settings file would
    be a serious regression, so the restore is unconditional."""
    if not workspace:
        yield None
        return
    path = Path(workspace) / dialect.config_relpath
    had_before = path.exists()
    previous = path.read_bytes() if had_before else None
    path.parent.mkdir(parents=True, exist_ok=True)
    _exclude_from_git(workspace, dialect.config_relpath)
    try:
        path.write_text(json.dumps(build_hook_config(dialect), indent=2))
        yield path
    finally:
        if previous is not None:
            path.write_bytes(previous)
        elif path.exists():
            path.unlink()


def _exclude_from_git(workspace: str, relpath: str) -> None:
    """Keep the generated config out of every SprintBaton commit via the
    workspace-local .git/info/exclude — never a tracked .gitignore edit, the
    same rule GitService._ensure_local_exclude follows for .sprintbaton/."""
    exclude = Path(workspace) / ".git" / "info" / "exclude"
    if not exclude.parent.is_dir():
        return
    entry = f"/{relpath.split('/')[0]}/"
    try:
        current = exclude.read_text() if exclude.exists() else ""
        if entry not in current:
            exclude.write_text(current + ("" if current.endswith("\n") or not current
                                          else "\n") + entry + "\n")
    except OSError:  # never fail a run over a housekeeping write
        log.debug("could not update .git/info/exclude", exc_info=True)


class SubprocessCliHarness:
    """Common execute()/run loop; subclasses set `name`, `provider`, the
    env var their CLI reads an API key from (`api_key_env_var`), the default
    binary and `hook_dialect`, and implement `_command`."""

    name: str = ""
    provider: str = ""
    # The env var this CLI would read an API key from. No longer an overlay
    # target — it names what _env() STRIPS, so the CLI can only ever use its
    # own login (cli-subscription-auth-parity spec §4.3).
    api_key_env_var: str = ""
    # Extra key vars the CLI also honors and that must be stripped with it.
    # Gemini reads GOOGLE_API_KEY as well as GEMINI_API_KEY, which
    # gemini_agent_sdk already treats as a pair.
    extra_key_env_vars: tuple[str, ...] = ()
    hook_dialect: HookDialect | None = None
    # Read-only by default; a subclass opts in once its dialect is wired. The
    # safe direction for any future subprocess harness (spec §4.6).
    supports_write_execution: bool = False
    # The CLI runs its own tools as local subprocesses — tool-mode
    # passthrough only, refused wherever runs must be isolated
    # (hosted-sandbox-isolation spec §7.3).
    sandbox_mode = "unsupported"

    def __init__(self, binary: str, *, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
                 scratch_root: str = ""):
        self._binary = binary
        self._timeout = timeout_seconds
        self._scratch_root = Path(scratch_root or tempfile.gettempdir())

    def execute(self, spec: HarnessTaskSpec, model: ModelSpec) -> HarnessResult:
        if model.provider != self.provider:
            raise ValueError(
                f"{self.name} only supports provider {self.provider!r}, "
                f"got {model.provider!r}")
        if not spec.read_only:
            if not self.supports_write_execution:
                raise NotImplementedError(
                    f"{self.name} may not back a write-capable role")
            return self._run_execution(spec, model)
        return self._run_read_only(spec, model)

    # ------------------------------------------------------------- read-only

    def _run_read_only(self, spec: HarnessTaskSpec, model: ModelSpec) -> HarnessResult:
        # A plain read-only run may write only its own answer file, so an
        # unexecuted hook costs little there. writable_paths changes that: the
        # run is licensed to edit a real directory in the clone, so the same
        # measurement and backstop the write path uses apply
        # (project-initialization-task spec §7.1, write-parity spec §5/§7).
        # The probe is install+version-cached, so this is not a per-run cost.
        guardrail_enforced = True
        if spec.writable_paths:
            self._warn_unguarded_writes(spec, READ_ONLY)
            guardrail_enforced = self._check_hook_capability(spec)
        prefix = (f"sprintbaton-out-{_fs_safe(spec.task_id)}-" if spec.task_id
                  else "sprintbaton-out-")
        with tempfile.TemporaryDirectory(prefix=prefix) as tmp_dir:
            output_path = (Path(tmp_dir) / "answer.txt").resolve()
            user_message = (
                spec.user_message + "\n\n" + _ADVISORY_GUARD_NOTE
                + _answer_instructions(output_path, spec.output_schema))
            cwd = self._cwd(spec)
            with self._hook_config(cwd):
                completed = subprocess.run(
                    self._command(spec, model),
                    input=user_message,
                    cwd=cwd,
                    env=self._env(model, spec, mode="read_only",
                                  workspace=cwd, state_dir=tmp_dir,
                                  answer_file=str(output_path)),
                    capture_output=True, text=True, timeout=self._timeout,
                )
            self._raise_if_blocked(tmp_dir)
            if spec.writable_paths:
                # The detect-and-abort backstop, for the same reason the probe
                # runs above: on a dialect that does not enforce write denies,
                # a write outside the writable roots that proceeded anyway
                # aborts the run rather than being published as metadata.
                self._raise_if_deny_violated(tmp_dir, self._dialect())
            answer = output_path.read_text() if output_path.exists() else ""
            usage_limits: list[UsageLimitSignal] = []
            if completed.returncode != 0 or not answer:
                usage_limits = detect_usage_limits(
                    {"result": completed.stdout}, completed.stderr)
                if usage_limits:
                    log.info("%s hit a usage limit", self.name, extra={
                        "task_id": spec.task_id, "scope": usage_limits[0].scope})
                elif not answer:
                    log.warning("%s run finished with no answer file", self.name,
                                extra={"task_id": spec.task_id,
                                       "returncode": completed.returncode,
                                       "stderr": completed.stderr[-2000:]})
            return HarnessResult(
                summary=completed.stdout[:_MAX_SUMMARY_CHARS],
                completed=bool(answer) and completed.returncode == 0,
                output_text=answer,
                usage_limits=usage_limits,
                guardrail_enforced=guardrail_enforced,
            )

    # ------------------------------------------------------------- execution

    def _run_execution(self, spec: HarnessTaskSpec, model: ModelSpec) -> HarnessResult:
        """The write-capable sibling of _run_read_only (spec §4.5) — same
        subprocess/blocked-marker/usage-limit shape, differing in the write-mode
        guard ruleset, the cross-subprocess exec-state file the post-tool hooks
        accumulate into, and the answer file always being parsed against
        FINISH_TOOL_SCHEMA."""
        dialect = self._dialect()
        self._warn_unguarded_writes(spec, EXECUTION)
        guardrail_enforced = self._check_hook_capability(spec)
        prefix = (f"sprintbaton-out-{_fs_safe(spec.task_id)}-" if spec.task_id
                  else "sprintbaton-out-")
        with tempfile.TemporaryDirectory(prefix=prefix) as tmp_dir:
            output_path = (Path(tmp_dir) / "answer.txt").resolve()
            user_message = spec.user_message + _answer_instructions(
                output_path, FINISH_TOOL_SCHEMA)
            with self._hook_config(spec.workspace_path):
                completed = subprocess.run(
                    self._command(spec, model),
                    input=user_message,
                    cwd=spec.workspace_path,
                    env=self._env(model, spec, mode="execution",
                                  workspace=spec.workspace_path,
                                  state_dir=tmp_dir),
                    capture_output=True, text=True, timeout=self._timeout,
                )
            self._raise_if_blocked(tmp_dir)
            # The detect-and-abort backstop (spec §5.1): on a CLI that does not
            # enforce a write deny, a write observed after a deny aborts the run
            # and discards its diff. The disposable clone is what makes this a
            # complete remedy rather than a partial one.
            self._raise_if_deny_violated(tmp_dir, dialect)

            finish = self._finish_payload(output_path, spec)
            state = self._exec_state(tmp_dir)
            usage_limits = detect_usage_limits(
                {"result": completed.stdout}, completed.stderr) \
                if completed.returncode != 0 else []
            return HarnessResult(
                summary=finish.get("summary", "")
                or completed.stdout[:_MAX_SUMMARY_CHARS],
                completed=bool(finish.get("completed", False)),
                asked_question=finish.get("question") or None,
                clarification_question=finish.get("clarification_question") or None,
                clarification_options=finish.get("clarification_options") or None,
                plan_broken=bool(finish.get("plan_broken", False)),
                importance_flags=finish.get("importance_flags") or [],
                files_edited=state.get("file_edit_counts", {}),
                consecutive_check_failures=state.get("consecutive_check_failures", 0),
                usage_limits=usage_limits,
                guardrail_enforced=guardrail_enforced,
            )

    # --------------------------------------------------------- hook plumbing

    def _dialect(self) -> HookDialect:
        if self.hook_dialect is None:
            raise NotImplementedError(
                f"{self.name} declares no hook_dialect but was asked to run "
                f"with guardrails")
        return self.hook_dialect

    def _hook_config(self, workspace: str):
        """Per-run workspace config, or a no-op for a user-layer dialect whose
        config `providers add` installed once."""
        dialect = self.hook_dialect
        if dialect is None or dialect.config_scope != "workspace_per_run":
            return _nullcontext()
        return _workspace_hook_config(dialect, workspace)

    def _raise_if_blocked(self, state_dir: str) -> None:
        blocked = Path(state_dir) / BLOCKED_MARKER
        if blocked.exists():
            data = json.loads(blocked.read_text())
            raise IrreversibleOperationError(data["command"], data["reason"])

    def _raise_if_deny_violated(self, state_dir: str,
                                dialect: HookDialect) -> None:
        marker = Path(state_dir) / DENY_VIOLATION_MARKER
        if not marker.exists():
            return
        data = json.loads(marker.read_text())
        raise IrreversibleOperationError(
            data.get("path", "<unknown>"),
            f"{self.name}: a write proceeded after the guard denied it "
            f"({dialect.name} does not enforce write denies) — the run is "
            f"aborted and its diff discarded")

    def _finish_payload(self, output_path: Path, spec: HarnessTaskSpec) -> dict:
        if not output_path.exists():
            log.warning("%s execution run produced no finish payload", self.name,
                        extra={"task_id": spec.task_id,
                               "workspace": spec.workspace_path})
            return {}
        try:
            payload = json.loads(output_path.read_text())
        except (json.JSONDecodeError, TypeError):
            log.warning("%s execution finish payload was not JSON", self.name,
                        extra={"task_id": spec.task_id})
            return {}
        return payload if isinstance(payload, dict) else {}

    def _exec_state(self, state_dir: str) -> dict:
        try:
            return json.loads((Path(state_dir) / EXEC_STATE_FILE).read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    # ----------------------------------------------------- runtime advisories

    def _warn_unguarded_writes(self, spec: HarnessTaskSpec, mode: str) -> None:
        """Warn, on every run that can write, about each known upstream defect
        that leaves writes unguarded on this harness (spec §6.3's run-time
        surface).

        Config-time surfaces (`agents create`, `POST /agents`) are seen once, by
        whoever wired the agent — not by whoever reads the logs after a task
        behaved oddly, and not at all when the agent was wired months earlier.
        A guarantee the system normally makes and does not make here is worth
        repeating per run, so this deliberately does not deduplicate.
        """
        from sprintbaton.harness.advisories import WARNING, advisories_for

        for advisory in advisories_for(self.name, mode=mode):
            if advisory.severity != WARNING:
                continue
            log.warning("%s: %s", self.name, advisory.summary, extra={
                "event": "harness_advisory",
                "task_id": spec.task_id,
                "harness": self.name,
                "mode": mode,
                "reference_url": advisory.reference_url,
                "verified_on": advisory.verified_on,
            })

    # -------------------------------------------------------- the probe (§7)

    def _check_hook_capability(self, spec: HarnessTaskSpec) -> bool:
        """Measure, don't assume (spec §7.1). Returns whether guardrails are
        actually enforced on this install.

        A failure does NOT block the run (spec §7.3) — it warns on EVERY
        write-mode run and stamps the result, deliberately, because the
        alternative is refusing to support the second-largest coding-agent CLI.
        The cost is real: guard.check_command does not execute, so a
        catastrophic command is not hard-stopped.
        """
        dialect = self._dialect()
        if not dialect.needs_capability_probe:
            return True
        from sprintbaton.harness.probe import probe_hook_capability

        if probe_hook_capability(self._binary, dialect, timeout_seconds=120):
            return True
        advisory = _probe_failure_advisory(self.name)
        log.warning("%s: %s", self.name, advisory.summary, extra={
            "event": "harness_advisory",
            "task_id": spec.task_id,
            "harness": self.name,
            "reference_url": advisory.reference_url,
            "guardrail_enforced": False,
        })
        return False

    # --------------------------------------------------------------- process

    def _cwd(self, spec: HarnessTaskSpec) -> str:
        if spec.workspace_path:
            return spec.workspace_path
        cwd = scratch_cwd_for(self._scratch_root, self.name, spec.task_id)
        cwd.mkdir(parents=True, exist_ok=True)
        return str(cwd)

    def stripped_key_vars(self) -> frozenset[str]:
        """Every API-key env var removed from the subprocess environment."""
        names = {self.api_key_env_var, *self.extra_key_env_vars}
        return frozenset(n for n in names if n)

    def _env(self, model: ModelSpec, spec: HarnessTaskSpec | None = None, *,
             mode: str = "", workspace: str = "", state_dir: str = "",
             answer_file: str = "") -> dict[str, str]:
        """The host environment minus this provider's API keys, plus the guard
        channel (spec §4.3).

        These CLIs authenticate from their own login session and nothing else
        (cli-subscription-auth-parity spec §4.3), so the key is stripped rather
        than overlaid — the same shape claude_code_cli has always had for
        ANTHROPIC_API_KEY. Without the strip, a user who ran `codex login` and
        happens to export OPENAI_API_KEY in their shell is billed to their
        platform account, silently, on every task: Settings.openai_api_key
        binds to that bare var, so it reaches ModelSpec.api_key through
        _resolve_api_key's deployment-fallback tier.

        The strip is unconditional rather than gated on spec.subscription_auth.
        Under §4.1's supports_metered_auth = False the two are equivalent
        (these harnesses only ever run subscription-authed, model.api_key is
        always ""), and unconditional is the form that stays correct if a
        future subclass reaches this base with different flags.
        """
        env = {k: v for k, v in os.environ.items()
               if k not in self.stripped_key_vars()}
        del model  # the key is never forwarded; see the docstring
        if mode:
            env[GUARD_MODE_VAR] = mode
            env[GUARD_WORKSPACE_VAR] = workspace or ""
            env[GUARD_STATE_DIR_VAR] = state_dir or ""
            env[GUARD_ANSWER_FILE_VAR] = answer_file or ""
            if spec is not None and spec.writable_paths:
                env[GUARD_WRITABLE_ROOTS_VAR] = os.pathsep.join(
                    str(p) for p in spec.writable_paths)
        return env

    def _command(self, spec: HarnessTaskSpec, model: ModelSpec) -> list[str]:
        raise NotImplementedError


DENY_VIOLATION_MARKER = "deny-violation.json"


@contextmanager
def _nullcontext():
    yield None


def _probe_failure_advisory(harness: str):
    from sprintbaton.harness.advisories import conditional_advisory
    return conditional_advisory(f"{harness}:hooks-not-executed")

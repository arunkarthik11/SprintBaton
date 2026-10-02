"""Human-readable console renderer for tool-mode interactive runs
(cli-logging spec §8). Installed instead of (never alongside) the JSON stdout
handler — setup_logging skips its JSON handler when is_installed() is True,
so two formats never interleave on one terminal. Hosted mode never gets one.
"""

import logging
import sys
import time

from sprintbaton.observer import context as log_ctx
from sprintbaton.observer.verbosity import TRACE_LOGGER_NAME, Verbosity

_installed = False
_color = False
_live_echoed = False

_RESET = "\x1b[0m"
_GREEN = "\x1b[32m"
_YELLOW = "\x1b[33m"
_RED = "\x1b[31m"
_EVENT_COLORS = {
    "pr_opened": _GREEN,
    "shipped": _GREEN,
    "blocked": _YELLOW,
    "clarification_paused": _YELLOW,
    "human_handoff": _YELLOW,
    "usage_limit_paused": _YELLOW,
    "error": _RED,
    # project-initialization-task spec §11
    "metadata_revision_published": _GREEN,
    "initialization_retry_scheduled": _YELLOW,
    "awaiting_project_initialization": _YELLOW,
    "metadata_gate_overridden": _YELLOW,
    "initialization_failed": _RED,
}

_TRACE_PREFIXES = {
    "tool_call": "→ tool_call: ",
    "tool_result": "← tool_result: ",
    "thinking": "· thinking: ",
    "text": "",
}


def _short_task_id(record: logging.LogRecord) -> str:
    task_id = getattr(record, "task_id", "") or log_ctx.current()["task_id"]
    # Entity ids are "<prefix>_<hex>"; the hex tail is the distinctive part.
    return (task_id.rsplit("_", 1)[-1][:6] or "-").ljust(6)


# --- per-event one-line messages (spec §5's table, third column) -------------

def _f(record: logging.LogRecord, key: str) -> str:
    return str(getattr(record, key, "") or "")


def _event_message(event: str, record: logging.LogRecord) -> str:
    match event:
        case "task_started":
            return f"processing task (status={_f(record, 'status')})"
        case "agent_assigned":
            tier = _f(record, "tier")
            suffix = f" (tier {tier})" if tier else ""
            action = _f(record, "action") or log_ctx.current()["action"]
            return (f"{action} → {_f(record, 'harness')} / "
                    f"{_f(record, 'model')}{suffix}")
        case "state_transition":
            return f"{_f(record, 'from_status')} → {_f(record, 'to_status')}"
        case "escalation":
            return (f"escalation {_f(record, 'from_tier')} → {_f(record, 'to_tier')} "
                    f"(trigger={_f(record, 'trigger')})")
        case "clarification_paused":
            action = _f(record, "action")
            return (f"paused — clarification question posted ({action}), "
                    f"reassigned to human")
        case "clarification_resumed":
            return (f"resumed — human replied, resuming {_f(record, 'action')} "
                    f"({_f(record, 'branch')} branch)")
        case "human_handoff":
            return f"handed to human (EH) — trigger={_f(record, 'trigger')}"
        case "usage_limit_paused":
            resume_at = _f(record, "resume_at")
            when = ""
            if resume_at:
                try:
                    when = " until " + time.strftime(
                        "%H:%M:%S", time.localtime(int(resume_at) / 1000))
                except ValueError:
                    pass
            return (f"paused — usage limit hit ({_f(record, 'scope')}),"
                    f" waiting{when} ({_f(record, 'action')})")
        case "usage_limit_resumed":
            branch = _f(record, "branch")
            suffix = f" ({branch} branch)" if branch else ""
            return f"resumed — usage limit cleared{suffix}"
        case "pr_opened":
            return f"PR opened: {_f(record, 'pr_url')}"
        case "shipped":
            release = _f(record, "release_id")
            return f"shipped ({release})" if release else "shipped"
        case "blocked":
            return f"blocked — {_f(record, 'summary') or record.getMessage()}"
        case "error":
            return f"ERROR {record.getMessage()}"
        # --- project initialization (project-initialization-task spec §11) ---
        case "initialization_queued":
            return (f"metadata initialization queued ({_f(record, 'trigger')}, "
                    f"scope={_f(record, 'scope')})")
        case "awaiting_project_initialization":
            error = _f(record, "error")
            suffix = f" — latest run failed: {error}" if error else ""
            return (f"waiting for project metadata initialization "
                    f"({_f(record, 'initialization_status')}){suffix}")
        case "initialization_retry_scheduled":
            return (f"initialization retry #{_f(record, 'attempt')} in "
                    f"{_f(record, 'delay_seconds')}s — {_f(record, 'error')}")
        case "initialization_continued":
            return (f"{_f(record, 'action')} cut off ({_f(record, 'stop_reason')}), "
                    f"continuing (round {_f(record, 'round')})")
        case "metadata_revision_published":
            target = _f(record, "repo_id") or _f(record, "project_id")
            collected = getattr(record, "collected_revisions", None) or []
            gc = f", collected {len(collected)} old revision(s)" if collected else ""
            return (f"published {_f(record, 'scope')} metadata revision for {target} "
                    f"({_f(record, 'files')} files{gc})")
        case "initialization_failed":
            return f"metadata initialization FAILED — {_f(record, 'error')}"
        case "metadata_gate_overridden":
            state = "opened" if getattr(record, "open", False) else "enforced"
            return f"metadata gate {state} for project {_f(record, 'project_id')}"
    return record.getMessage()


def render_line(record: logging.LogRecord) -> str:
    ts = time.strftime("%H:%M:%S", time.localtime(record.created))
    if record.name == TRACE_LOGGER_NAME:
        # Indented under the current task's key-transition lines so a human
        # can visually follow one task's live activity (spec §8.2).
        prefix = _TRACE_PREFIXES.get(getattr(record, "trace_kind", "text"), "")
        message = record.getMessage()
        return "\n".join(f"    {prefix if i == 0 else '    '}{line}"
                         for i, line in enumerate(message.splitlines() or [""]))
    event = getattr(record, "event", None)
    if not event:
        # Every other pre-existing log call, unchanged from today: plain
        # rendering, no per-call-site auditing needed (spec §8.1).
        level = f" {record.levelname}" if record.levelno >= logging.WARNING else ""
        line = f"{ts}  {record.name}{level}  {record.getMessage()}"
        if record.exc_info and record.exc_info[1] is not None:
            line += f": {record.exc_info[1]}"
        return line
    line = f"{ts}  {_short_task_id(record)}  {_event_message(event, record)}"
    color = _EVENT_COLORS.get(event)
    if color and _color:
        line = f"{color}{line}{_RESET}"
    return line


class CliConsoleHandler(logging.Handler):
    """Human-readable renderer for tool-mode interactive runs."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = render_line(record)
        except Exception:  # never let rendering take the worker down
            line = record.getMessage()
        if line:
            print(line, file=sys.stdout, flush=True)


def install_console_handler(verbosity: Verbosity, *, mode: str,
                            force_json: bool = False) -> bool:
    """Install the console renderer as the root handler for a tool-mode
    interactive run. Returns whether it actually installed one (False when
    hosted mode, no TTY — a piped `sprintbaton serve > out.log` still gets
    JSON — or --json was passed); the caller only wires the JSON handler when
    this returned False (spec §8)."""
    global _installed, _color
    if mode != "tool" or force_json or not sys.stdout.isatty():
        return False
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(CliConsoleHandler())
    _installed = True
    _color = sys.stdout.isatty()
    return True


def is_installed() -> bool:
    return _installed


def uninstall_console_handler() -> None:
    """Test hook — module state would otherwise leak across tests."""
    global _installed, _color
    root = logging.getLogger()
    root.handlers[:] = [h for h in root.handlers
                        if not isinstance(h, CliConsoleHandler)]
    _installed = False
    _color = False


# --- raw live echo (spec §8.2) ----------------------------------------------
# single_shot's token-level streaming callback bypasses logging entirely and
# writes straight to the same stdout stream — genuinely live per-character
# echo, gated by the caller on verbosity == VERBOSE and is_installed().

def live_echo(text: str) -> None:
    global _live_echoed
    _live_echoed = True
    sys.stdout.write(text)
    sys.stdout.flush()


def end_live_echo() -> None:
    """Terminate the raw echo with a newline so the next rendered log line
    starts on its own line."""
    global _live_echoed
    if _live_echoed:
        sys.stdout.write("\n")
        sys.stdout.flush()
        _live_echoed = False

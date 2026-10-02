"""Process-wide log verbosity (cli-logging spec §3).

Verbosity controls *volume* (which records get emitted at all); the output
surface (human-readable console vs. JSON) is the orthogonal axis derived from
SPRINTBATON_MODE in console.py. The resolved level is a module-level variable,
not a contextvar — verbosity is a process-wide setting for the whole worker
run, never scoped to one task.
"""

import logging
from enum import StrEnum

# The dedicated reasoning-trace logger (spec §6.1): separate from every
# module's own __name__ logger so a hosted operator can filter the (much
# larger) trace volume independently via standard logging configuration.
TRACE_LOGGER_NAME = "sprintbaton.trace"

# Per-record cap for reasoning-trace content — well under raw_tool_loop's
# MAX_OUTPUT_CHARS so one runaway tool output can't flood the terminal or
# blow a hosted log-line size limit (spec §6.1).
TRACE_CONSOLE_CHARS = 2_000


class Verbosity(StrEnum):
    SILENT = "silent"
    NORMAL = "normal"
    VERBOSE = "verbose"


_LEVEL_FLOOR = {
    Verbosity.SILENT: logging.WARNING,
    Verbosity.NORMAL: logging.INFO,
    Verbosity.VERBOSE: logging.DEBUG,
}

# Third-party loggers whose DEBUG output would drown the reasoning-trace
# stream at verbose — capped to INFO so --verbose stays about SprintBaton's
# own activity, not HTTP wire chatter.
_NOISY_LOGGERS = ("httpx", "httpcore", "urllib3", "botocore", "anthropic", "asyncio")

_current = Verbosity.NORMAL


def resolve_verbosity(env_value: str, *, verbose: bool = False,
                      quiet: bool = False) -> Verbosity:
    """Resolution order (spec §3): explicit CLI flag > SPRINTBATON_LOG_VERBOSITY
    > normal. Fails loudly on an unknown env value, never defaulting silently."""
    if verbose:
        return Verbosity.VERBOSE
    if quiet:
        return Verbosity.SILENT
    try:
        return Verbosity(env_value or Verbosity.NORMAL)
    except ValueError:
        raise ValueError(
            f"unknown SPRINTBATON_LOG_VERBOSITY {env_value!r} "
            f"(valid: silent, normal, verbose)"
        ) from None


def set_verbosity(verbosity: Verbosity, *, base_level: str = "info") -> None:
    """Store the process-wide verbosity and apply its level floor to the root
    logger, so `silent` genuinely costs nothing below WARNING (Python's
    logging short-circuits disabled levels before any formatter runs).
    `base_level` is the pre-existing LOG_LEVEL knob, still authoritative at
    NORMAL (see effective_root_level)."""
    global _current
    _current = verbosity
    logging.getLogger().setLevel(effective_root_level(base_level))
    # Cheap early-out (spec §6.1): non-verbose runs never even format a trace
    # record, let alone render one.
    logging.getLogger(TRACE_LOGGER_NAME).disabled = verbosity != Verbosity.VERBOSE
    if verbosity == Verbosity.VERBOSE:
        for name in _NOISY_LOGGERS:
            logging.getLogger(name).setLevel(logging.INFO)


def get_verbosity() -> Verbosity:
    return _current


def effective_root_level(base_level_name: str = "info") -> int:
    """NORMAL keeps LOG_LEVEL (the coarser pre-existing knob) authoritative —
    today's behavior unchanged; silent/verbose apply their own floor. This is
    also how `--verbose --json` and LOG_LEVEL agree rather than the stricter
    of the two winning unexpectedly (spec §14 resolution)."""
    if _current == Verbosity.NORMAL:
        return getattr(logging, base_level_name.upper(), logging.INFO)
    return _LEVEL_FLOOR[_current]

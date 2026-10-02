"""Failure classification and backoff for project initialization runs
(docs/project-initialization-task-spec.md §5.6).

Every exception escaping an init pass is classified **once**, here:

- **transient** — a dependency was briefly unreachable (model API, git remote,
  blob store): retried with exponential backoff via Task.retryAfter;
- **permanent** — a configuration error, an exhausted budget, or *anything
  unclassified*: the run fails loudly. Retrying an unknown error spends tokens
  looping on a defect and delays the surfaced error.

Usage-limit signals and turn/time cut-offs never reach this module: they are
not errors (a pause, a continuation), handled before any exception is raised.

Provider SDK and botocore classes are matched lazily *by import path*, through
sys.modules: an exception of a class can only exist once its module is
imported, so an uninstalled or never-used SDK is simply not matched — and this
module never imports one.
"""

import random
import socket
import sys

from sprintbaton.harness.base import (
    AuthResolutionError,
    HarnessCapabilityError,
    TransientHarnessError,
)
from sprintbaton.models.guard import IrreversibleOperationError
from sprintbaton.vcs.git_service import GitAuthenticationError, GitTransientError

TRANSIENT = "transient"
PERMANENT = "permanent"

# Checked first: some of these subclass a transient base (every Git* error is
# a RuntimeError) or would otherwise be swallowed by the unclassified default.
_PERMANENT_TYPES: tuple[type[BaseException], ...] = (
    AuthResolutionError,
    HarnessCapabilityError,
    GitAuthenticationError,
    IrreversibleOperationError,
    KeyError,  # resolution: unknown AgentDefinition / provider
)

_TRANSIENT_TYPES: tuple[type[BaseException], ...] = (
    TransientHarnessError,
    GitTransientError,
    ConnectionError,
    TimeoutError,     # also socket.timeout
    socket.gaierror,  # name resolution
    socket.herror,
)

# (module, class name) pairs matched only if the module is already imported.
_LAZY_TRANSIENT_CLASSES: tuple[tuple[str, str], ...] = (
    ("httpx", "TransportError"),
    ("anthropic", "APIConnectionError"),   # APITimeoutError subclasses it
    ("anthropic", "APITimeoutError"),
    ("anthropic", "InternalServerError"),
    ("openai", "APIConnectionError"),
    ("openai", "APITimeoutError"),
    ("openai", "InternalServerError"),
    ("google.genai.errors", "ServerError"),
    ("botocore.exceptions", "EndpointConnectionError"),
    ("botocore.exceptions", "ConnectionClosedError"),
    ("botocore.exceptions", "ReadTimeoutError"),
    ("botocore.exceptions", "ConnectTimeoutError"),
)

# Provider SDK status errors carrying a >= 500 status (e.g. Anthropic's 529
# "overloaded") are transient even when their class is a generic APIStatusError.
_STATUS_ERROR_MODULE_ROOTS = ("anthropic", "openai", "google.genai")


def _lazy_match(exc: BaseException) -> bool:
    for module_name, class_name in _LAZY_TRANSIENT_CLASSES:
        module = sys.modules.get(module_name)
        cls = getattr(module, class_name, None) if module is not None else None
        if isinstance(cls, type) and isinstance(exc, cls):
            return True
    return False


def _server_status_error(exc: BaseException) -> bool:
    module = type(exc).__module__ or ""
    if not module.startswith(_STATUS_ERROR_MODULE_ROOTS):
        return False
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(exc, "code", None)
    return isinstance(status, int) and status >= 500


def _classify_one(exc: BaseException) -> str | None:
    if isinstance(exc, _PERMANENT_TYPES):
        return PERMANENT
    if isinstance(exc, _TRANSIENT_TYPES) or _lazy_match(exc) or _server_status_error(exc):
        return TRANSIENT
    return None


def classify_initialization_error(exc: BaseException) -> str:
    """TRANSIENT or PERMANENT for an exception escaping an init pass. Walks an
    explicit `raise … from` chain, so a wrapper around a connection error is
    still recognized; anything unmatched is PERMANENT (spec §5.6)."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        verdict = _classify_one(current)
        if verdict is not None:
            return verdict
        current = current.__cause__
    return PERMANENT


def backoff_millis(attempt: int, *, base_seconds: int, max_seconds: int,
                   rng: random.Random | None = None) -> int:
    """Delay before retry `attempt` (1-based): base * 2**(attempt-1), capped at
    max_seconds, with ±10% jitter so a fleet of runs failing on one outage does
    not retry in lockstep (spec §5.6)."""
    delay = min(base_seconds * 2 ** max(attempt - 1, 0), max_seconds)
    jitter = (rng or random).uniform(-0.1, 0.1)
    return max(int(delay * (1 + jitter) * 1000), 0)

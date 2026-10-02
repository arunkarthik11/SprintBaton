"""Request-scoped log-identifier layer (cli-logging spec §4).

contextvars (not a thread-local or an explicit parameter) is deliberate for
the same reason the existing trace.get_current_span() mechanism uses it:
process() calls straight down into services, agents, and harnesses several
frames deep, and none of that call chain should need a request_id parameter
added to its signature just so a log.info four layers down can tag its line.

request_id is a SprintBaton-minted id (new_id("req")), not the OTel trace_id,
on purpose — the two coexist as independent fields on every log line (§4).
"""

import contextvars
from contextlib import contextmanager

_request_id: contextvars.ContextVar[str] = contextvars.ContextVar("sb_request_id", default="")
_task_id: contextvars.ContextVar[str] = contextvars.ContextVar("sb_task_id", default="")
_user_id: contextvars.ContextVar[str] = contextvars.ContextVar("sb_user_id", default="")
_repo_id: contextvars.ContextVar[str] = contextvars.ContextVar("sb_repo_id", default="")
_action: contextvars.ContextVar[str] = contextvars.ContextVar("sb_action", default="")


@contextmanager
def log_context(*, request_id: str | None = None, task_id: str | None = None,
                user_id: str | None = None, repo_id: str | None = None,
                action: str | None = None):
    """Push whichever fields are given; unset ones inherit the enclosing
    context unchanged (so nested `with log_context(action=...)` calls don't
    have to re-state task_id/request_id/user_id)."""
    tokens = []
    for var, value in ((_request_id, request_id), (_task_id, task_id),
                       (_user_id, user_id), (_repo_id, repo_id), (_action, action)):
        if value is not None:
            tokens.append((var, var.set(value)))
    try:
        yield
    finally:
        for var, tok in reversed(tokens):
            var.reset(tok)


def current() -> dict[str, str]:
    return {"request_id": _request_id.get(), "task_id": _task_id.get(),
            "user_id": _user_id.get(), "repo_id": _repo_id.get(),
            "action": _action.get()}

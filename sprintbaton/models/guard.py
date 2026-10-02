"""Irreversibility guardrails (escalation spec §6).

An unattended agent never performs an irreversible operation autonomously,
regardless of tier. Matching commands hard-stop execution and jump the task
straight to EH (human handoff); the user approves via comment before the action
may proceed.
"""

import re

DESTRUCTIVE_PATTERNS: list[tuple[str, str]] = [
    (r"\bdrop\s+(table|database|schema|collection)\b", "SQL/NoSQL drop"),
    (r"\btruncate\s+table\b", "SQL truncate"),
    (r"\bdelete\s+from\b(?!.*\bwhere\b)", "unbounded SQL delete"),
    (r"\bgit\s+push\b.*(--force|-f\b|\+[^\s]+:)", "git force-push"),
    (r"\bgit\s+push\b", "git push (orchestrator-only operation)"),
    (r"\brm\s+(-[a-zA-Z]*r[a-zA-Z]*f|-[a-zA-Z]*f[a-zA-Z]*r)\b", "recursive force delete"),
    (r"\brm\b.*\s/(?:\s|$)", "delete at filesystem root"),
    (r"\b(alembic|flyway|liquibase)\b.*\b(upgrade|migrate|update)\b", "database migration apply"),
    (r"\bmanage\.py\s+migrate\b", "django migration apply"),
    (r"\bkubectl\s+(delete|apply|scale)\b", "cluster mutation"),
    (r"\bhelm\s+(install|upgrade|uninstall|delete)\b", "cluster mutation"),
    (r"\bterraform\s+(apply|destroy)\b(?!.*-target=plan)", "infrastructure mutation"),
    (r"\bdocker\s+(system\s+prune|rm|rmi)\b", "docker resource deletion"),
    (r"\bmkfs\b|\bdd\s+if=", "disk-level operation"),
]

_COMPILED = [(re.compile(p, re.IGNORECASE), label) for p, label in DESTRUCTIVE_PATTERNS]


class IrreversibleOperationError(Exception):
    def __init__(self, command: str, reason: str):
        self.command = command
        self.reason = reason
        super().__init__(f"irreversible operation blocked ({reason}): {command}")


def check_command(command: str) -> None:
    """Raise IrreversibleOperationError if the command matches a destructive pattern."""
    for pattern, label in _COMPILED:
        if pattern.search(command):
            raise IrreversibleOperationError(command, label)


# --- Read-only allowlist (execution-tier-agents spec §9.4) -------------------
#
# check_command above is a denylist tuned for the Coding Model's blast-radius
# concern — it happily lets merely-mutating commands through (sed -i, git
# commit, pip install). A read-only guarantee needs the opposite shape: an
# allowlist of known-safe read patterns, reject everything else, fail closed.
# Best-effort by design (no full shell parsing — quoting/subshells can fool the
# segment split), consistent with the heuristics elsewhere in the harness.

_READ_ONLY_ALLOWLIST = re.compile(
    r"^(ls|cat|head|tail|wc|find|grep|rg|tree|file|stat|du|pwd|echo|which|"
    r"diff|sort|uniq|cut|"
    r"git\s+(log|diff|show|status|blame|ls-files|rev-parse|branch))\b"
)
# Any `>` redirection except a pure fd dup (2>&1, 1>&2) is write-capable;
# tee/dd write files; find's -delete/-exec/-ok escape the allowlist's intent.
_WRITE_CAPABLE = re.compile(
    r">(?!&[12])|\btee\b|\bdd\b|\bfind\b.*\s-(delete|exec|execdir|ok|okdir)\b"
)
_SEGMENT_SPLIT = re.compile(r"&&|\|\||;|\|")


def check_read_only(command: str) -> None:
    """Raise IrreversibleOperationError if `command` is not a recognized
    read-only pattern, or contains write-capable redirection. Each
    ;/&&/||/|-separated segment is checked independently so a benign prefix
    can't smuggle a write later in the pipeline."""
    check_command(command)  # defense in depth — catastrophic patterns still apply
    if _WRITE_CAPABLE.search(command):
        raise IrreversibleOperationError(command, "write-capable redirection or command")
    for segment in _SEGMENT_SPLIT.split(command):
        segment = segment.strip()
        if not segment:
            continue
        if not _READ_ONLY_ALLOWLIST.match(segment):
            raise IrreversibleOperationError(segment, "not on the read-only command allowlist")

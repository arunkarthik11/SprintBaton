"""Harness abstraction — the neutral contract between the task-action layer
and any execution runtime (docs/agent-sdk-migration-spec.md §4).

An agent is defined by three independent choices: a harness (which runtime
executes the task), a model (ModelSpec), and a prompt — persisted together as
an AgentDefinition (entities/agent_definition.py). Every TaskActionAgent runs
through a Harness: single-shot roles (classification/finalization/planning/
review) default to the `single_shot` harness, the Coding Model to a tool-loop
harness (`raw_tool_loop`, `claude_agent_sdk`, `open_hands`). The value objects
here mediate between SprintBaton and every harness; nothing in this module may
assume a specific runtime shape.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from sprintbaton.entities.usage import TokenUsage
from sprintbaton.models import guard

MAX_EXECUTION_ITERATIONS = 60
DIFF_TIMEOUT_SECONDS = 60
MAX_DIFF_CHARS = 20_000
DEFAULT_MAX_OUTPUT_TOKENS = 16_000
DEFAULT_USER_MESSAGE = "Implement the task now."

# The structured-options payload a role may attach alongside its
# clarification_question (clarification-options spec §7) — one fragment,
# reused by FINISH_TOOL_SCHEMA here and by every advisory OUTPUT_SCHEMA
# (models/agents.py). Mirrors Answer/ClarificationOptions
# (entities/clarification.py), snake_case as model output.
CLARIFICATION_OPTIONS_SCHEMA: dict = {
    "type": ["object", "null"],
    "properties": {
        "header": {"type": "string", "description": "Short chip label, <=12 chars"},
        "answers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "option": {"type": "string"},
                    "description": {"type": "string"},
                    "is_recommended": {"type": "boolean"},
                    "additional_notes": {"type": "string"},
                },
                "required": ["option"],
                # The Anthropic structured-output API (output_config json_schema)
                # rejects any object that does not explicitly disallow extra
                # keys — required recursively on every nested object, not just
                # the top level. This fragment is embedded in the advisory
                # OUTPUT_SCHEMAs that run on single_shot, so it must comply too.
                "additionalProperties": False,
            },
        },
        "multi_select": {"type": "boolean"},
    },
    "additionalProperties": False,
}

# The terminal-payload contract shared by every harness: the Coding Model ends
# a run by calling `finish` with this schema, and the payload maps 1:1 onto
# HarnessResult's escalation-signal fields (spec §7).
FINISH_TOOL_NAME = "finish"
FINISH_TOOL_DESCRIPTION = (
    "Finish the task. Call exactly once, when the work is complete, blocked "
    "on a human question, or the plan no longer fits reality."
)
FINISH_TOOL_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "What was done, or why you stopped"},
        "completed": {"type": "boolean"},
        "question": {
            "type": "string",
            "description": "A clarifying question for the human, if you are blocked on a product decision",
        },
        "clarification_question": {
            "type": "string",
            "description": (
                "One narrow decision the human must make for you to continue as-is "
                "(e.g. a structural preference between two reasonable options). The task "
                "pauses and resumes with the answer. Use `question` instead when the task "
                "itself is mis-specified and needs re-specification."
            ),
        },
        "clarification_options": {
            **CLARIFICATION_OPTIONS_SCHEMA,
            "description": (
                "Optional structured options for clarification_question, when you have "
                "2-4 genuinely differentiated choices to offer (mark at most one "
                "is_recommended). Only meaningful alongside clarification_question."
            ),
        },
        "plan_broken": {
            "type": "boolean",
            "description": "True if runtime discoveries invalidated the plan's structure",
        },
        "importance_flags": {
            "type": "array",
            "items": {"type": "string", "enum": ["auth", "payments", "migration"]},
            "description": "Importance-gated surfaces this change unexpectedly touches",
        },
    },
    "required": ["summary", "completed"],
}


# The answer-file schema of the two metadata init-pass actions
# (project-initialization-task spec §8.3). The files the agent edits in place
# ARE the output; the answer carries only run metadata. Removals travel here
# because no delete tool is ever offered to a read-only run (§7.1).
METADATA_RUN_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "What was created, changed, verified, or deleted — recorded in notes.md",
        },
        "removedFiles": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Paths, relative to the metadata directory, of files to delete",
        },
    },
    "required": ["summary", "removedFiles"],
    "additionalProperties": False,
}

# Why a harness run ended (project-initialization-task spec §7.2). A run cut
# off by a turn cap or wall-clock timeout returns normally — never raised — so
# the partial on-disk work of an in-place-editing run survives for a
# continuation.
StopReason = Literal["completed", "turn_limit", "time_limit", "error"]


# Usage-limit scopes (usage-limit-aware execution spec §4.1). Deliberately
# not a closed enum: a harness or a future budget policy may report an
# arbitrary scope string (e.g. "budget:daily") without a change here — every
# consumer treats `scope` as opaque. The two constants exist only because
# they are the two Claude Code concretely has today.
USAGE_LIMIT_SCOPE_SESSION = "session"   # e.g. Claude Code's rolling multi-hour window
USAGE_LIMIT_SCOPE_WEEKLY = "weekly"     # e.g. Claude Code's weekly cap


@dataclass(frozen=True)
class UsageLimitSignal:
    """One cascading constraint a harness detected — reactively (the call was
    rejected/throttled) or proactively (SprintBaton's own usage tracking
    crossed a configured warning threshold before the hard limit would hit).
    Harness-neutral, mirroring ModelSpec/GuardrailPolicy's role in this module:
    nothing here may assume a specific provider's error shape."""

    scope: str                # USAGE_LIMIT_SCOPE_SESSION | _WEEKLY | "budget:<name>" | ...
    resets_at: int | None     # epoch millis this scope is believed to clear; None = unknown/indefinite
    proactive: bool = False   # True = threshold-crossed pre-emptive pause; False = the call was rejected
    detail: str = ""          # raw provider/tracker message — logged and, optionally, shown to the human


@dataclass(frozen=True)
class ModelSpec:
    """Which model a harness should run internally. `provider` is "anthropic"
    for both built-in harnesses; carried as a field so a multi-provider
    harness (e.g. OpenHands) has somewhere to route from.

    `api_key` is the per-call credential resolved by AgentDefinitionResolver
    (per-user-provider-credentials spec §4.2) — it rides the same per-call
    channel model_id already travels on, so harnesses stay credential-agnostic
    singletons. Empty means "no resolved credential": the harness falls back to
    its own ambient/login auth, exactly the pre-spec zero-config behavior.
    Never populated for a claude_code_cli-bound spec (no-forwarded-key
    invariant). repr=False keeps the plaintext out of any logged/formatted
    ModelSpec."""

    provider: str = "anthropic"
    model_id: str = ""
    api_key: str = field(default="", repr=False)
    # The owner's subscription token (CredentialProvider.ANTHROPIC_SUBSCRIPTION)
    # on a subscription-authed call (hosted-sandbox-isolation spec §9.2). Empty
    # on every metered call, and on a subscription call that authenticates from
    # an ambient on-disk login instead (tool mode only).
    auth_token: str = field(default="", repr=False)
    # Provider.baseUrl (spec §9.3): an Anthropic-compatible endpoint other than
    # the provider type's default. Empty means the default.
    base_url: str = ""


@runtime_checkable
class GuardrailPolicy(Protocol):
    def check_command(self, command: str) -> None:
        """Raise IrreversibleOperationError if `command` is destructive.
        Harness-agnostic in definition; each harness enforces it via its own
        native interception point (spec §6)."""

    def check_read_only(self, command: str) -> None:
        """Raise IrreversibleOperationError if `command` is not a recognized
        read-only pattern (execution-tier-agents spec §9.4). Fail-closed
        allowlist — strictly narrower than check_command's denylist."""


class DefaultGuardrailPolicy:
    """Wraps sprintbaton.models.guard unchanged — guard.py stays the single
    source of truth for what counts as irreversible / write-capable."""

    def check_command(self, command: str) -> None:
        guard.check_command(command)

    def check_read_only(self, command: str) -> None:
        guard.check_read_only(command)


DEFAULT_GUARDRAIL_POLICY = DefaultGuardrailPolicy()


@dataclass
class HarnessTaskSpec:
    """Everything a harness needs to execute one task action turn. Tool-loop
    fields (workspace, guardrails, iterations) are meaningless to single-shot
    harnesses and vice versa (output_schema); each harness reads what applies."""

    system_prompt: str
    workspace_path: str = ""
    user_message: str = DEFAULT_USER_MESSAGE
    guardrails: GuardrailPolicy = DEFAULT_GUARDRAIL_POLICY
    max_iterations: int = MAX_EXECUTION_ITERATIONS
    situation_report: str | None = None
    conversation_id: str | None = None  # same-role/tier resume only (spec §8)
    # Single-shot structured output: a JSON schema the final text must satisfy.
    output_schema: dict | None = None
    max_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    adaptive_thinking: bool = False
    # Read-only mode (execution-tier-agents spec §9.4): inert for single_shot;
    # for tool-loop harnesses it gates the tool set (no Edit, allowlisted Bash,
    # Write scoped to one scratch answer file). Execution is the only role that
    # sets this to False.
    read_only: bool = True
    # Subscription (OAuth) auth mode (hosted-sandbox-isolation spec §9.2):
    # the run authenticates from the owner's subscription — ModelSpec.
    # auth_token when the owner stored one, else (tool mode only) the host's
    # on-disk `claude login` session. Always paired with an empty
    # ModelSpec.api_key. Honored by the harnesses that spawn a `claude` binary.
    subscription_auth: bool = False
    # Task attribution for concurrent runs: stamped into the per-run scratch
    # output directory name and logs so an answer file is always traceable to
    # the task that produced it (the directory itself is unique per run).
    task_id: str = ""
    # The task's owner (Task.userId). Keys the task's sandbox session and its
    # per-tenant dependency cache (hosted-sandbox-isolation spec §6.1) and is
    # bound into every run token the broker mints (§8.2). Empty only for
    # direct harness calls, which run under the tool-mode passthrough.
    owner_id: str = ""
    # Extra roots a read-only run may Write/Edit inside (project-
    # initialization-task spec §7.1). Meaningful only when read_only=True, and
    # it widens only *what may be written*: Bash stays allowlist-gated,
    # check_command hard-stops still apply, and no delete tool is added. Only
    # harnesses declaring supports_writable_paths honor it — the resolver
    # refuses to hand one to any other (§6.3).
    writable_paths: tuple[str, ...] = ()


@dataclass
class HarnessResult:
    """The harness-neutral shape of what came out of a run — a same-shape
    sibling of TaskExecutionResponse minus the request-owned fields
    (taskId, promptId), which the TaskActionAgent fills in."""

    summary: str = ""
    completed: bool = False
    asked_question: str | None = None
    # The pause-in-place signal (conversation-lifecycle spec §3) — narrower
    # than asked_question, which escalates to the Spec role instead.
    clarification_question: str | None = None
    # Raw structured options from the finish payload (clarification-options
    # spec §7) — kept harness-neutral as a dict; the TaskActionAgent validates
    # it into a ClarificationOptions (or drops it) via
    # parse_clarification_options.
    clarification_options: dict | None = None
    plan_broken: bool = False
    importance_flags: list[str] = field(default_factory=list)
    files_edited: dict[str, int] = field(default_factory=dict)
    consecutive_check_failures: int = 0
    diff: str = ""
    usage: TokenUsage = field(default_factory=TokenUsage)
    conversation_id: str | None = None
    # The run's final text output — the finish summary for tool-loop
    # harnesses, the raw completion (JSON when output_schema was set) for
    # single-shot harnesses.
    output_text: str = ""
    # Usage/rate/budget constraints detected this run (usage-limit-aware
    # execution spec §4.2). Empty (the default on every existing harness,
    # unchanged) means "no constraint hit" — additive, same pattern
    # clarification_question/clarification_options were added with.
    usage_limits: list[UsageLimitSignal] = field(default_factory=list)
    # Why the run ended (project-initialization-task spec §7.2). Harness-
    # neutral; only the metadata init pass consumes it in v1.
    stop_reason: StopReason = "completed"
    # Whether guard.py's checks actually executed for this run (subprocess-cli-
    # write-parity spec §7.3). True on every harness that enforces in-process
    # or through a verified hook — the default, so no existing harness changes.
    # False only when a hook-capability probe established that the CLI does not
    # run our hook: the run proceeded unguarded, by explicit product decision,
    # and this flag is what makes "was this diff produced under a guard?"
    # answerable after the fact rather than inferred from logs.
    guardrail_enforced: bool = True


class Harness(Protocol):
    """A named execution runtime. AgentDefinitions reference harnesses by
    `name`; `execute` runs the given task spec with the given model."""

    name: str  # "single_shot" | "raw_tool_loop" | "claude_agent_sdk" | "open_hands" | ...

    # --- auth capabilities (auth-mode-resolution spec §3) -------------------
    # AgentDefinitionResolver._auth_mode derives HarnessTaskSpec.subscription_auth
    # from (owner eligibility, these flags, credential availability). The
    # defaults below describe the common case — a metered API harness with no
    # subscription channel — so a newly registered harness needs no edit and
    # can never silently become subscription-capable.

    # Can authenticate from a provider subscription/login session instead of a
    # metered key. True for every harness that spawns a logged-in CLI —
    # claude_code_cli, claude_agent_sdk (which spawns `claude` too), codex_cli
    # and gemini_cli. False for the in-process HTTP clients, which have no CLI
    # underneath to inherit a session from and whose vendors offer no
    # subscription auth for the SDK itself (cli-subscription-auth-parity §3).
    supports_subscription_auth: bool = False

    # The env var the harness's CLI reads a subscription token from, or ""
    # when its session exists only as an on-disk login. A non-empty value is
    # what lets an owner's own stored subscription Credential
    # (CredentialProvider.ANTHROPIC_SUBSCRIPTION) satisfy the harness
    # (hosted-sandbox-isolation spec §9.2); "" means "tool mode only" — an
    # on-disk session is never probed (auth-mode-resolution §9 q1).
    subscription_token_var: str = ""

    # Can authenticate from a metered API key at all. False for the three
    # coding CLIs, whose no-forwarded-key invariant means a resolved key would
    # be silently discarded — declaring it here turns that into a loud failure
    # instead (spec §3, cli-subscription-auth-parity §4.1).
    supports_metered_auth: bool = True

    # When running metered, does this harness require an explicitly resolved
    # key? True for claude_agent_sdk: an ambient ~/.claude session there is
    # exactly the fall-through _auth_env's token blanking exists to prevent.
    #
    # No harness overrides this to False any more — codex_cli/gemini_cli did,
    # to legalise "metered with no key" as a stand-in for their CLI login, and
    # cli-subscription-auth-parity §4.1 replaced that with the honest
    # supports_metered_auth = False. The flag is still load-bearing: its
    # default governs every metered harness, and _resolve_auth reads it on the
    # branch a future ambient-auth harness would take. Do not delete as unused.
    requires_explicit_key_when_metered: bool = True

    # Honors HarnessTaskSpec.writable_paths in read-only mode (project-
    # initialization-task spec §7.1). False by default so a newly registered
    # harness can never silently be handed an in-place-editing role.
    supports_writable_paths: bool = False

    # How this harness uses the sandbox seam (hosted-sandbox-isolation spec
    # §7): "tools" — each tool call crosses it, the model loop stays in the
    # worker; "process" — the whole CLI process runs in the sandbox; "none" —
    # no tools at all; "unsupported" — runs tools on the host and so is refused
    # wherever runs must be isolated. Read through `sandbox_mode_of`, whose
    # default is "unsupported": a new harness that forgets to declare is
    # refused in hosted mode rather than silently running tenant code in the
    # worker.
    sandbox_mode: str = "unsupported"

    # How a "process" harness receives its model credential (spec §8.3):
    # "broker" — a placeholder in the run, the real one injected by the
    # worker's egress broker; "env" — the owner's real credential in that
    # run's environment only. Meaningless for "tools"/"none" harnesses, whose
    # model calls happen in the worker.
    credential_delivery: str = "env"

    def execute(self, spec: HarnessTaskSpec, model: ModelSpec) -> HarnessResult: ...


SANDBOX_MODES = ("tools", "process", "none", "unsupported")


def sandbox_mode_of(harness) -> str:
    """A harness's declared sandbox mode, failing safe to "unsupported"."""
    mode = getattr(harness, "sandbox_mode", "unsupported")
    return mode if mode in SANDBOX_MODES else "unsupported"


def unsandboxable_reason(harness) -> str:
    name = getattr(harness, "name", "?")
    return (f"harness {name!r} runs its tools on the host and cannot be "
            f"sandboxed, so it is refused where runs must be isolated (hosted "
            f"mode — hosted-sandbox-isolation spec §7). Use this provider's "
            f"agent-SDK or single-shot harness instead.")


def unusable_harness_reason(harness, *, isolated: bool,
                            local_login_sessions: bool) -> str | None:
    """Why this harness could never run in this deployment, or None if it could.

    The write-time twin of AgentDefinitionResolver._resolve_auth, shared by
    `POST /agents` and `sprintbaton agents create` so one rule has one
    implementation (cli-subscription-auth-parity spec §4.5). Accepts a harness
    class or instance — only class attributes are read.

    Two structural refusals, and no longer any per-owner one: since
    hosted-sandbox-isolation spec §9 every owner brings their own credentials,
    so nothing about the *account* makes a harness unusable.

    - `isolated` (runs must go through the sandbox — hosted mode): a harness
      whose `sandbox_mode` is "unsupported" runs tenant-driven tools on the
      host and is refused outright (§7).
    - A subscription-only harness with no routable token var authenticates
      only from an on-disk login, which exists only where
      `local_login_sessions` is declared (tool mode, §9.2).
    """
    if isolated and sandbox_mode_of(harness) == "unsupported":
        return unsandboxable_reason(harness)
    if getattr(harness, "supports_metered_auth", True):
        return None
    name = getattr(harness, "name", "?")
    if not getattr(harness, "subscription_token_var", "") and not local_login_sessions:
        return (f"harness {name!r} authenticates only from a local CLI login "
                f"session, which exists only on a tool-mode install. Use this "
                f"provider's agent-SDK or single-shot harness instead.")
    return None


class AuthResolutionError(RuntimeError):
    """A task action's auth mode could not be satisfied (auth-mode-resolution
    spec §2.1).

    Raised at *resolution* time, before any workspace is prepared or any repo
    cloned, so the message names the real cause — which action, which harness,
    which of the three subscription conditions failed, which credential tier
    was empty — rather than surfacing as an opaque provider-SDK error deep
    inside a harness run.
    """


class HarnessUnavailableError(AuthResolutionError):
    """A harness cannot run in this deployment at all — today, one whose
    `sandbox_mode` is "unsupported" where runs must be isolated
    (hosted-sandbox-isolation spec §7). A subclass of AuthResolutionError on
    purpose: like an unsatisfiable auth mode it is a per-deployment condition,
    so a fallback chain steps past it (cli-subscription-auth-parity §4.4)."""


class HarnessCapabilityError(RuntimeError):
    """A task action resolved to a harness that lacks a capability the action
    requires — today, `supports_writable_paths` for the metadata init actions
    (project-initialization-task spec §6.3). A configuration error, raised at
    resolution time, never retried."""


class TransientHarnessError(RuntimeError):
    """A harness run failed because a dependency was briefly unreachable (the
    model API overloaded, a connection reset, a 5xx) rather than because of
    anything about the task (project-initialization-task spec §5.6). Raised by
    the CLI-spawning harnesses, whose outages arrive as a failed result instead
    of a Python exception; the init pass retries it with backoff."""


# Failure text the `claude` binary emits for a model-API outage (spec §5.6).
# Unverified against a real outage (spec §17 q2) — an unmatched failure stays a
# permanent one, the safe direction. Only ever consulted on a failure path.
_TRANSIENT_FAILURE_TEXT = re.compile(
    r"overloaded(?:_error)?"
    r"|API Error:?\s*5\d\d"
    r"|internal[ _]server[ _]error"
    r"|connection (?:error|reset|refused|closed)"
    r"|ECONNRESET|ECONNREFUSED|ETIMEDOUT|EAI_AGAIN|ENOTFOUND|EPIPE"
    r"|socket hang up|fetch failed|network error"
    r"|request timed out|timed out waiting",
    re.IGNORECASE)


def looks_transient(*texts: str) -> bool:
    """Does any failure text match a transient model-API/network outage?"""
    return any(t and _TRANSIENT_FAILURE_TEXT.search(t) for t in texts)


def within_roots(target: Path, roots: Iterable[Path | str]) -> bool:
    """Is `target` inside any root, symlinks followed on both sides
    (project-initialization-task spec §7.1)? The one containment check every
    writable_paths enforcement point shares."""
    resolved = Path(target).resolve()
    return any(resolved.is_relative_to(Path(root).resolve()) for root in roots)


def workspace_diff(workspace_path: str | Path) -> str:
    """The PR diff, produced against the workspace after the run — the same
    mechanism for every harness, never harness-internal (spec §7)."""
    result = subprocess.run(
        ["git", "diff"], cwd=workspace_path, capture_output=True, text=True,
        timeout=DIFF_TIMEOUT_SECONDS,
    )
    return result.stdout[:MAX_DIFF_CHARS]


def fs_safe(value: str) -> str:
    """Task ids come from providers/Mongo — sanitize before using one in a
    directory name."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", value)[:64]


def scratch_cwd_for(scratch_root: str | Path, harness_name: str,
                    task_id: str) -> Path:
    """The stable per-task working directory a subprocess-CLI harness uses for
    a workspace-less run, so `--resume` finds the session the CLI persisted
    per cwd. One naming rule for every harness (workspace-mirrors-and-cleanup
    spec §5.2): cleanup removes this path for each registered harness name and
    never needs to know how an individual harness names things."""
    return Path(scratch_root) / (
        f"sprintbaton-{harness_name}-cwd-{fs_safe(task_id) or 'shared'}")

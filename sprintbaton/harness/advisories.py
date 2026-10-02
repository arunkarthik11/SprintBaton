"""HarnessAdvisory — the code-owned register of known third-party defects that
change what a harness actually guarantees (subprocess-cli-write-parity-and-
advisories spec §6).

A harness can be wired, run and relied upon by three audiences who will never
read its module docstring: someone running `sprintbaton agents create` in a
terminal, someone POSTing to /agents from a web UI, and someone reading worker
logs after a task behaved oddly. This module is the one table all three render
from, and the one place that answers "what do we currently know to be broken
upstream, and when did we last check?".

Static and code-owned, the same posture as providers/registry.py's
BUILTIN_PROVIDERS and models/guard.py's denylist: a known-defects table is not
user data.

`reference_url` and `verified_on` are mandatory by construction (__post_init__
raises). An advisory without a citation is a rumour, and one without a date rots
silently — this module exists because a 2026-03 finding about the codex/gemini
CLIs was still being treated as current in 2026-09.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

# Run modes an advisory can be scoped to — the same vocabulary
# HarnessTaskSpec.read_only splits on.
READ_ONLY = "read_only"
EXECUTION = "execution"

# severity levels, ordered
CAUTION = "caution"    # a limitation worth knowing; nothing is unenforced
WARNING = "warning"    # a guarantee the system normally makes does not hold


@dataclass(frozen=True)
class HarnessAdvisory:
    harness: str
    severity: str
    applies_to: tuple[str, ...]
    summary: str                 # one line, user-facing
    detail: str                  # what breaks, and what we do about it
    reference_url: str           # upstream issue — never empty
    verified_on: str             # ISO date this claim was last checked
    # True for an advisory that is only emitted when some runtime condition
    # holds (the §7 hook-capability probe failing; a provider with a
    # non-default baseUrl). Never surfaced by the static config-time lookups —
    # only by the caller that established the condition, via
    # `conditional_advisory`.
    conditional: bool = False
    # The "<harness>:<slug>" id a conditional advisory is looked up by.
    slug: str = ""

    def __post_init__(self) -> None:
        if not self.reference_url:
            raise ValueError(
                f"advisory for {self.harness!r} has no reference_url — an "
                f"advisory without a citation is a rumour (spec §6.1)")
        try:
            date.fromisoformat(self.verified_on)
        except ValueError as e:
            raise ValueError(
                f"advisory for {self.harness!r} has an unparseable "
                f"verified_on {self.verified_on!r}: {e}") from None
        if self.severity not in (CAUTION, WARNING):
            raise ValueError(
                f"advisory for {self.harness!r} has unknown severity "
                f"{self.severity!r}")

    def render(self) -> str:
        """The one-block human form shared by every text surface."""
        return (f"[{self.severity}] {self.harness}: {self.summary}\n"
                f"  {self.detail}\n"
                f"  See: {self.reference_url} (verified {self.verified_on})")

    def as_dict(self) -> dict:
        """The API shape (spec §6.3) — camelCase at the HTTP boundary."""
        return {
            "harness": self.harness,
            "severity": self.severity,
            "summary": self.summary,
            "detail": self.detail,
            "referenceUrl": self.reference_url,
            "verifiedOn": self.verified_on,
        }


# The id of the probe-failure advisory, referenced by the harness that emits it.
CODEX_HOOKS_NOT_EXECUTED = "codex_cli:hooks-not-executed"
# A claude_agent_sdk agent whose provider points at a gateway other than
# Anthropic's own API (hosted-sandbox-isolation spec §9.3).
CLAUDE_SDK_GATEWAY = "claude_agent_sdk:non-default-base-url"


ADVISORIES: tuple[HarnessAdvisory, ...] = (
    HarnessAdvisory(
        harness="codex_cli",
        severity=WARNING,
        # READ_ONLY too since writable_paths: a read-only init run may edit
        # .sprintbaton/ for real, so unguarded file edits apply there as well
        # (project-initialization-task spec §7.1).
        applies_to=(READ_ONLY, EXECUTION),
        summary=("Codex fires PreToolUse for the Bash tool ONLY, so file edits "
                 "are never checked by SprintBaton's guard: an irreversible "
                 "file operation is NOT held for human approval on this "
                 "harness."),
        detail=("apply_patch/Write/Edit never reach check_command, so the "
                "EH hard-stop cannot fire for them and the detect-and-abort "
                "backstop is inert (it needs the pre-hook to record a denied "
                "write first). Catastrophic *Bash* commands are still "
                "hard-stopped. What contains the rest is scope, not "
                "enforcement: every write lands in a disposable per-task clone "
                "and reaches you only as a pull request — and, for the "
                "metadata init pass, only the contract-validated .sprintbaton/ "
                "tree is published. A second upstream defect (#27833) would "
                "independently leave apply_patch denies unenforced even if "
                "file tools were intercepted. Wire the action to "
                "openai_agent_sdk, which enforces guardrails in-process, if "
                "you need file operations genuinely guarded. Applies to a "
                "read-only run only when it carries writable_paths."),
        reference_url="https://github.com/openai/codex/blob/main/docs/hooks.md",
        verified_on="2026-09-23",
    ),
    HarnessAdvisory(
        harness="codex_cli",
        severity=WARNING,
        applies_to=(READ_ONLY, EXECUTION),
        summary=("The SprintBaton guard hook does not execute on this install, "
                 "so NO guardrail — including catastrophic-command hard-stops "
                 "— is enforced for this harness."),
        detail=("`codex exec` skips hooks recorded as trusted. Remedy: trust "
                "the SprintBaton hook via `codex` -> /hooks, or wire this "
                "action to openai_agent_sdk, which enforces guardrails "
                "in-process. Re-checked automatically when the codex binary "
                "version changes."),
        reference_url="https://github.com/openai/codex/issues/32491",
        verified_on="2026-09-23",
        conditional=True,
        slug="hooks-not-executed",
    ),
    HarnessAdvisory(
        harness="claude_agent_sdk",
        severity=CAUTION,
        applies_to=(READ_ONLY, EXECUTION),
        summary=("This agent's provider sets a baseUrl, so Claude Code talks to "
                 "a gateway rather than Anthropic's API — a non-Claude model "
                 "behind it works but is not supported by Anthropic."),
        detail=("SprintBaton routes the run's model traffic to the provider's "
                "baseUrl (hosted-sandbox-isolation spec §9.3) and forwards "
                "anthropic-beta verbatim. Claude Code features that depend on "
                "Anthropic-specific API behavior may degrade or fail against "
                "another model; SprintBaton does not block it."),
        reference_url="https://code.claude.com/docs/en/llm-gateway",
        verified_on="2026-09-29",
        conditional=True,
        slug="non-default-base-url",
    ),
    HarnessAdvisory(
        harness="codex_cli",
        severity=CAUTION,
        applies_to=(READ_ONLY, EXECUTION),
        summary="Non-interactive flags are unverified against a live codex binary.",
        detail=("The harness is wired and unit-tested but has never run a real "
                "`codex exec` subprocess; the exact flag strings are "
                "best-effort (provider-registration spec §16)."),
        reference_url="https://github.com/openai/codex/blob/main/docs/exec.md",
        verified_on="2026-09-23",
    ),
    HarnessAdvisory(
        harness="gemini_cli",
        severity=CAUTION,
        applies_to=(READ_ONLY, EXECUTION),
        summary="Non-interactive flags are unverified against a live gemini binary.",
        detail=("The harness is wired and unit-tested but has never run a real "
                "`gemini -p` subprocess; the exact flag strings are best-effort "
                "(provider-registration spec §16)."),
        reference_url="https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/index.md",
        verified_on="2026-09-23",
    ),
    HarnessAdvisory(
        harness="open_hands",
        severity=WARNING,
        applies_to=(READ_ONLY,),
        summary=("Guardrails are prompt-advisory only; no escalation-signal "
                 "reconstruction and no token accounting."),
        detail=("A prompt-forwarding placeholder. It must pass the conformance "
                "suite before backing an execution AgentDefinition, and "
                "read_only=False raises today."),
        reference_url="https://github.com/All-Hands-AI/OpenHands",
        verified_on="2026-09-23",
    ),
)


def advisories_for(harness: str,
                   mode: str | None = None) -> tuple[HarnessAdvisory, ...]:
    """Every unconditional advisory for a harness, optionally narrowed to one
    run mode. Conditional advisories (§7.3) are deliberately excluded — they
    are only meaningful once the runtime condition has been established, and
    surfacing one at config time would claim something we have not measured."""
    return tuple(
        a for a in ADVISORIES
        if a.harness == harness
        and not a.conditional
        and (mode is None or mode in a.applies_to)
    )


def conditional_advisory(advisory_id: str) -> HarnessAdvisory:
    """A conditional advisory by "<harness>:<slug>" id, for the caller that
    established its condition. Fails loudly on an unknown id — a missing
    advisory must never degrade to silence."""
    harness, _, slug = advisory_id.partition(":")
    for a in ADVISORIES:
        if a.harness == harness and a.conditional and a.slug == slug:
            return a
    raise KeyError(f"no conditional advisory registered for {advisory_id!r}")


def gateway_advisories(harness: str, base_url: str | None) -> tuple[HarnessAdvisory, ...]:
    """The base-URL advisory (spec §9.3) for a claude_agent_sdk agent whose
    provider sets one — the one config-time condition a surface can check
    without running anything."""
    if harness == "claude_agent_sdk" and base_url:
        return (conditional_advisory(CLAUDE_SDK_GATEWAY),)
    return ()


def has_advisories(harness: str) -> bool:
    """Whether to mark this harness in `sprintbaton agents harnesses` (§6.3).
    Conditional advisories do not mark a harness: they describe a
    configuration, not the harness itself."""
    return any(a.harness == harness and not a.conditional for a in ADVISORIES)

"""ChainedHarness — the extension point for composite agents.

A ChainedHarness wraps any inner harness and runs it once per ChainStep:
each step forwards its own prompt (multiple prompts), optionally validates
the step's HarnessResult with a check function, and retries with the check's
feedback appended when the check fails (checks). The chain carries the inner
harness's conversation id between steps so resume-capable harnesses keep one
session, and aggregates usage/edit-counts/flags across steps so the
escalation and analytics pipelines see the whole run.

Register a ChainedHarness under its own unique name and reference that name
from an AgentDefinition like any other harness, e.g.:

    HarnessRegistry.register(ChainedHarness(
        name="plan_then_code",
        inner=claude_agent_sdk_harness,
        steps=[
            ChainStep("plan", "Write IMPLEMENTATION.md describing your approach."),
            ChainStep("code", "Implement the plan, then call finish.",
                      check=lambda r: None if r.completed else "finish(completed=true) was never reached",
                      max_attempts=2),
        ],
    ))

For richer behavior (dynamic steps, per-step models), subclass and override
execute() or steps_for(); the Harness protocol only requires name + execute.
"""

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace

from sprintbaton.entities.usage import TokenUsage
from sprintbaton.harness.base import Harness, HarnessResult, HarnessTaskSpec, ModelSpec

log = logging.getLogger(__name__)

# Returns None when the result passes, or a human-readable failure reason that
# is fed back to the model on retry.
CheckFn = Callable[[HarnessResult], str | None]


@dataclass(frozen=True)
class ChainStep:
    name: str
    user_message: str
    check: CheckFn | None = None
    max_attempts: int = 1


class ChainedHarness:
    def __init__(self, name: str, inner: Harness, steps: Sequence[ChainStep]):
        if not steps:
            raise ValueError("a ChainedHarness needs at least one step")
        self.name = name
        self._inner = inner
        self._steps = list(steps)

    @property
    def sandbox_mode(self) -> str:
        """A chain crosses the sandbox seam exactly as its inner harness does
        (hosted-sandbox-isolation spec §7)."""
        return getattr(self._inner, "sandbox_mode", "unsupported")

    @property
    def credential_delivery(self) -> str:
        return getattr(self._inner, "credential_delivery", "env")

    @property
    def supports_writable_paths(self) -> bool:
        """A chain can honor writable_paths exactly when its inner harness can
        (project-initialization-task spec §7.1)."""
        return bool(getattr(self._inner, "supports_writable_paths", False))

    def steps_for(self, spec: HarnessTaskSpec) -> list[ChainStep]:
        """Override to derive steps from the task at execute time."""
        return self._steps

    def execute(self, spec: HarnessTaskSpec, model: ModelSpec) -> HarnessResult:
        usage = TokenUsage()
        files_edited: dict[str, int] = {}
        importance_flags: list[str] = []
        conversation_id = spec.conversation_id
        result = HarnessResult()

        for step in self.steps_for(spec):
            message = step.user_message
            failure: str | None = None
            for attempt in range(max(1, step.max_attempts)):
                step_spec = replace(spec, user_message=message,
                                    conversation_id=conversation_id)
                result = self._inner.execute(step_spec, model)

                usage = usage + result.usage
                for path, count in result.files_edited.items():
                    files_edited[path] = files_edited.get(path, 0) + count
                for flag in result.importance_flags:
                    if flag not in importance_flags:
                        importance_flags.append(flag)
                conversation_id = result.conversation_id or conversation_id

                failure = step.check(result) if step.check else None
                if failure is None:
                    break
                log.info("chain step check failed", extra={
                    "harness": self.name, "step": step.name,
                    "attempt": attempt + 1, "reason": failure,
                })
                message = (
                    f"{step.user_message}\n\nYour previous attempt failed a "
                    f"check: {failure}\nFix that and try again."
                )
            if failure is not None:
                # The step never passed its check: surface an incomplete run so
                # the orchestrator's escalation triggers take over.
                return replace(
                    result, completed=False, usage=usage,
                    files_edited=files_edited, importance_flags=importance_flags,
                    conversation_id=conversation_id,
                    summary=f"chain step {step.name!r} failed its check: {failure}",
                )

        return replace(
            result, usage=usage, files_edited=files_edited,
            importance_flags=importance_flags, conversation_id=conversation_id,
        )

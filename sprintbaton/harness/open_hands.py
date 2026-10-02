"""The open_hands harness — forwards the task to an OpenHands agent.

The simplest possible bridge: render the prompt, hand it to an OpenHands
Conversation running in the task workspace, collect the final message and the
workspace diff. Model routing uses litellm-style ids, so any provider the
OpenHands runtime supports works (e.g. ModelSpec(provider="openrouter",
model_id="z-ai/glm-5.2") -> "openrouter/z-ai/glm-5.2").

⚠️ NOT conformance-passed (tests/harness/test_conformance.py). Known gaps,
all of which must close before this harness may back a real execution
AgentDefinition:
- Guardrails: there is no native pre-tool interception wired yet, so the
  irreversibility guard (EH hard stop) is only communicated as a prompt
  instruction — advisory, not enforced.
- Escalation signals: files_edited / consecutive_check_failures are not
  reconstructed; the finish-tool contract is not implemented, so completion
  is inferred from the run ending.
- Token accounting: usage is reported as zero (PLACEHOLDER — wire
  conversation stats once the SDK surface is confirmed live).

The `openhands` SDK is an optional dependency; import happens lazily at
execute() time so the rest of the harness family works without it.
"""

import logging

from sprintbaton.entities.usage import TokenUsage
from sprintbaton.harness.base import (
    HarnessResult,
    HarnessTaskSpec,
    ModelSpec,
    workspace_diff,
)
from sprintbaton.dependencies import require_module

log = logging.getLogger(__name__)

GUARDRAIL_PROMPT_SUFFIX = (
    "\n\nHARD CONSTRAINTS: never run destructive or irreversible commands "
    "(git push, force-deletes, migrations, dropping data). If the task seems "
    "to require one, stop and report instead."
)


class OpenHandsHarness:
    name = "open_hands"
    # The OpenHands runtime executes its own tools in-process with no seam to
    # route them through, so it is refused wherever runs must be isolated
    # (hosted-sandbox-isolation spec §7) — at `agents create`, `POST /agents`
    # and resolution.
    sandbox_mode = "unsupported"

    def __init__(self, llm_api_key: str = "", llm_base_url: str = ""):
        self._llm_api_key = llm_api_key
        self._llm_base_url = llm_base_url

    def execute(self, spec: HarnessTaskSpec, model: ModelSpec) -> HarnessResult:
        model_string = f"{model.provider}/{model.model_id}"
        prompt = (
            spec.system_prompt + GUARDRAIL_PROMPT_SUFFIX + "\n\n" + spec.user_message
        )
        final_text = self._forward(prompt, model_string, spec)
        return HarnessResult(
            summary=final_text[:2000],
            completed=True,  # no finish-tool contract yet: run-ended == done
            diff=workspace_diff(spec.workspace_path) if spec.workspace_path else "",
            usage=TokenUsage(),  # PLACEHOLDER: OpenHands usage accounting not wired
            output_text=final_text,
        )

    def _forward(self, prompt: str, model_string: str, spec: HarnessTaskSpec) -> str:
        """One OpenHands conversation: send the prompt, run to completion,
        return the final agent message. Kept as a single seam so tests can
        stub the SDK interaction."""
        require_module("openhands.sdk", package="openhands-ai", extra="openhands",
                       harness="open_hands")
        from openhands.sdk import LLM, Agent, Conversation  # noqa: PLC0415

        llm_kwargs: dict = {"model": model_string}
        if self._llm_api_key:
            llm_kwargs["api_key"] = self._llm_api_key
        if self._llm_base_url:
            llm_kwargs["base_url"] = self._llm_base_url

        agent = Agent(llm=LLM(**llm_kwargs))
        conversation = Conversation(agent=agent, workspace=spec.workspace_path or ".")
        conversation.send_message(prompt)
        conversation.run()

        # Best-effort extraction of the last agent message; the SDK event
        # shapes are not contractual, so fall back to empty.
        for event in reversed(list(getattr(conversation.state, "events", []))):
            text = getattr(event, "message", None) or getattr(event, "content", None)
            if text and getattr(event, "source", "") == "agent":
                return str(text)
        return ""

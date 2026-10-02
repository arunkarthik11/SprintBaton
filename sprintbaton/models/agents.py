"""TaskActionAgents — one per action role (docs/entities.md).

Each agent binds a Harness + a ModelSpec + a prompt (the triple an
AgentDefinition persists) to one TaskActionRequest/Response pair. The
single-shot roles default to the `single_shot` harness; the Coding (execution)
agent's model id is additionally a request parameter so escalation can move it
between Sonnet (E0) and Opus (E3) without changing the agent.
"""

import dataclasses
import json
import logging
import time

from pydantic import ValidationError

from sprintbaton.entities.actions import (
    MetadataGenerationRequest,
    MetadataGenerationResponse,
    ProjectMetadataGenerationRequest,
    ProjectMetadataGenerationResponse,
    TaskClassificationRequest,
    TaskClassificationResponse,
    TaskConflictResolutionRequest,
    TaskConflictResolutionResponse,
    TaskExecutionRequest,
    TaskExecutionResponse,
    TaskFinalizationRequest,
    TaskFinalizationResponse,
    TaskPassingCriteriaRequest,
    TaskPassingCriteriaResponse,
    TaskPlanClassificationRequest,
    TaskPlanClassificationResponse,
    TaskPlanningRequest,
    TaskPlanningResponse,
    TaskRepoScopingRequest,
    TaskRepoScopingResponse,
    TaskRevisionClassificationRequest,
    TaskRevisionClassificationResponse,
    TaskReviewRequest,
    TaskReviewResponse,
    TaskSpecClassificationRequest,
    TaskSpecClassificationResponse,
)
from sprintbaton.entities.clarification import MIN_ANSWERS, ClarificationOptions
from sprintbaton.entities.enums import (
    PlanClassificationVerdict,
    RewindPoint,
    SpecClassificationVerdict,
    TaskType,
)
from sprintbaton.harness.base import (
    CLARIFICATION_OPTIONS_SCHEMA,
    DEFAULT_GUARDRAIL_POLICY,
    DEFAULT_USER_MESSAGE,
    MAX_EXECUTION_ITERATIONS,
    METADATA_RUN_SCHEMA,
    Harness,
    HarnessResult,
    HarnessTaskSpec,
    ModelSpec,
)
from sprintbaton.entities.task_revision import render_amendment, render_prior_round
from sprintbaton.prompts.registry import AgentPrompt

log = logging.getLogger(__name__)


def _conversation_kwargs(request) -> dict:
    """The uniform request -> _agentic threading of the conversation-lifecycle
    fields (spec §7.3) — no agent hand-writes the resume/restart branch."""
    return {
        "conversation_id": request.conversationId,
        "reply_text": request.replyText,
        "clarification_context": request.clarificationContext,
    }


def _with_revision_context(system: str, request) -> str:
    """Append the revisions context to a rendered prompt (task-revisions spec
    §7.6/§9.3): the "this card was shipped before" block and the "Amending a
    previous version" block, each only when set — so a first pass renders
    byte-identically to before. Appended rather than templated so no
    template (or test rendering one) grows a placeholder it must fill."""
    blocks = [render_prior_round(request.priorRound),
              render_amendment(request.amendment)]
    extra = "\n\n".join(b for b in blocks if b)
    return f"{system}\n\n{extra}" if extra else system


def _importance_context(important: bool) -> str:
    """Importance context for the two classification checkpoints
    (classification-taxonomy spec §11): the orchestrator floors a Simple
    verdict to Compound regardless, but a model that knows the task is
    important may independently reach Complex (plan first), which the floor
    cannot produce on its own."""
    if important:
        return ("This task IS flagged important — it touches an "
                "importance-gated surface (auth/payments/migration/core). A "
                "`Simple` verdict will be floored to `Compound` downstream; "
                "weigh the elevated cost of a mistake in your own judgment.")
    return "This task is not flagged important."


def parse_clarification_options(raw: object,
                                question: str | None) -> ClarificationOptions | None:
    """Validate a model's clarification_options payload (clarification-options
    spec §7), degrading to None — never failing the turn — when it is absent,
    malformed, orphaned from a question, or offers fewer than 2 answers.
    Count/recommendation excesses degrade inside ClarificationOptions itself."""
    if not question or not isinstance(raw, dict):
        return None
    try:
        options = ClarificationOptions.model_validate(raw)
    except ValidationError as e:
        log.warning("clarification_options payload malformed; dropped",
                    extra={"error": str(e)})
        return None
    if len(options.answers) < MIN_ANSWERS:
        return None
    return options


def parse_json_output(result: HarnessResult) -> dict:
    """Parse a structured-output run. Fails loudly when the configured harness
    did not honor output_schema (e.g. a tool-loop harness wired onto a
    single-shot action by a mis-built AgentDefinition)."""
    try:
        return json.loads(result.output_text)
    except (json.JSONDecodeError, TypeError) as e:
        raise ValueError(
            f"harness output is not the expected JSON: {result.output_text[:200]!r}"
        ) from e


class TaskActionAgent:
    def __init__(self, harness: Harness, model: ModelSpec, prompt: AgentPrompt,
                 subscription_auth: bool = False):
        self.harness = harness
        self.model = model
        self.prompt = prompt
        # Auth mode resolved alongside (harness, model) by
        # AgentDefinitionResolver (agent-sdk-subscription-auth spec §2) and
        # threaded onto every HarnessTaskSpec this agent builds. Inert for
        # every harness except claude_agent_sdk.
        self.subscription_auth = subscription_auth

    def _usage_limited(self, response_cls, request, result: HarnessResult):
        """The run was cut short by a usage/rate/budget constraint before it
        produced usable output (usage-limit-aware execution spec §5.1): a
        signal-only response, skipping output parsing — every other field
        keeps its (safe-branch) default and must be treated as ignorable,
        exactly like a clarificationQuestion pause."""
        return response_cls(
            taskId=request.taskId,
            usageLimitSignals=result.usage_limits,
            usage=result.usage,
            modelId=self.model.model_id,
            promptId=self.prompt.prompt_id,
            conversationId=result.conversation_id,
        )

    def _agentic(self, system: str, user_message: str, *,
                 output_schema: dict | None = None, workspace_path: str = "",
                 max_tokens: int = 8192, adaptive_thinking: bool = False,
                 task_id: str = "", conversation_id: str | None = None,
                 reply_text: str | None = None,
                 clarification_context: str | None = None,
                 owner_id: str = "") -> HarnessResult:
        """A read-only harness run (execution-tier-agents spec §9.6): on
        single_shot the workspace/read_only/task_id fields are inert; on a
        tool-loop harness they gate the tool set and scope the scratch answer
        file the final output travels through.

        The resume-vs-restart branch of the conversation lifecycle
        (conversation-lifecycle spec §7.3) lives here exactly once: a resumed
        session already has the full history, so only the human's reply is
        sent; a fresh/restarted run gets the prior Q&A appended to the system
        prompt instead."""
        if conversation_id and reply_text is not None:
            effective_user_message = reply_text
        else:
            effective_user_message = user_message
            if clarification_context:
                system = f"{system}\n\n## Prior human clarification\n{clarification_context}"
        return self.harness.execute(
            HarnessTaskSpec(
                system_prompt=system,
                user_message=effective_user_message,
                output_schema=output_schema,
                workspace_path=workspace_path,
                read_only=True,
                subscription_auth=self.subscription_auth,
                task_id=task_id,
                owner_id=owner_id,
                max_tokens=max_tokens,
                adaptive_thinking=adaptive_thinking,
                conversation_id=conversation_id,
            ),
            self.model,
        )

    def _single_shot(self, system: str, user_message: str, *,
                     output_schema: dict | None = None,
                     max_tokens: int = 8192,
                     adaptive_thinking: bool = False,
                     task_id: str = "", conversation_id: str | None = None,
                     reply_text: str | None = None,
                     clarification_context: str | None = None,
                     owner_id: str = "") -> HarnessResult:
        return self._agentic(
            system, user_message, output_schema=output_schema,
            max_tokens=max_tokens, adaptive_thinking=adaptive_thinking,
            task_id=task_id, conversation_id=conversation_id,
            reply_text=reply_text, clarification_context=clarification_context,
            owner_id=owner_id,
        )


class TaskClassificationAgent(TaskActionAgent):
    """The Router. Cheap classifier; sets category + importance flags."""

    OUTPUT_SCHEMA = {
        "type": "object",
        "properties": {
            "category": {"type": "string", "enum": ["Simple", "Ambiguous", "Complex", "Abstract"]},
            "rationale": {"type": "string"},
            "importance_flags": {
                "type": "array",
                # auth/payments/migration are the always-important surfaces;
                # "core" is the general bucket for an application-specific
                # critical surface identified from the repository metadata
                # (classification prompt: Importance section).
                "items": {"type": "string",
                          "enum": ["auth", "payments", "migration", "core"]},
            },
        },
        "required": ["category", "rationale", "importance_flags"],
        "additionalProperties": False,
    }

    def execute(self, request: TaskClassificationRequest) -> TaskClassificationResponse:
        system = self.prompt.render(
            metadata_summary=request.metadataSummary,
            task_title=request.taskTitle,
            task_description=request.taskDescription,
        )
        system = _with_revision_context(system, request)
        result = self._single_shot(
            system, "Classify this task.",
            output_schema=self.OUTPUT_SCHEMA, max_tokens=1024,
            task_id=request.taskId, owner_id=request.userId,
        )
        data = parse_json_output(result)
        return TaskClassificationResponse(
            taskId=request.taskId,
            category=TaskType(data["category"]),
            rationale=data["rationale"],
            importanceFlags=data.get("importance_flags", []),
            usage=result.usage,
            modelId=self.model.model_id,
            promptId=self.prompt.prompt_id,
            usageLimitSignals=result.usage_limits,
        )


class TaskSpecClassificationAgent(TaskActionAgent):
    """The Spec Classification checkpoint (classification-taxonomy spec §4):
    given a finalized spec, a 3-way verdict — Simple (Sonnet direct), Compound
    (Opus direct), or Complex (needs a plan first). Read-only, single-shot."""

    OUTPUT_SCHEMA = {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": ["Simple", "Compound", "Complex"]},
            "rationale": {"type": "string"},
            # The pause-and-ask signal (conversation-lifecycle spec §4.4):
            # when non-null, every other field may be absent and is ignored.
            "clarification_question": {"type": ["string", "null"]},
            # Optional 2-4 structured choices alongside the question
            # (clarification-options spec §7)
            "clarification_options": CLARIFICATION_OPTIONS_SCHEMA,
        },
        "required": ["clarification_question"],
        "additionalProperties": False,
    }

    def execute(self, request: TaskSpecClassificationRequest) -> TaskSpecClassificationResponse:
        system = self.prompt.render(
            metadata_summary=request.metadataSummary,
            task_title=request.taskTitle,
            task_description=request.taskDescription,
            finalized_spec=request.finalizedSpec,
            importance_context=_importance_context(request.important),
        )
        result = self._single_shot(
            system, "Classify this finalized spec.",
            output_schema=self.OUTPUT_SCHEMA, max_tokens=1024,
            task_id=request.taskId, owner_id=request.userId, **_conversation_kwargs(request),
        )
        if result.usage_limits and not result.output_text:
            return self._usage_limited(TaskSpecClassificationResponse, request, result)
        data = parse_json_output(result)
        # An absent/invalid verdict defaults to the safer branch, same as the
        # service's malformed-output fallback (implementability spec §12)
        try:
            verdict = SpecClassificationVerdict(data.get("verdict"))
        except ValueError:
            verdict = SpecClassificationVerdict.Complex
        return TaskSpecClassificationResponse(
            taskId=request.taskId,
            verdict=verdict,
            rationale=data.get("rationale", ""),
            clarificationQuestion=data.get("clarification_question"),
            clarificationOptions=parse_clarification_options(
                data.get("clarification_options"), data.get("clarification_question")),
            usage=result.usage,
            modelId=self.model.model_id,
            promptId=self.prompt.prompt_id,
            conversationId=result.conversation_id,
            usageLimitSignals=result.usage_limits,
        )


class TaskPlanClassificationAgent(TaskActionAgent):
    """The Plan Classification checkpoint (classification-taxonomy spec §5):
    given a finalized spec + plan, a 2-way verdict — Simple (Sonnet) or
    Compound (Opus) execution. Read-only, single-shot."""

    OUTPUT_SCHEMA = {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": ["Simple", "Compound"]},
            "rationale": {"type": "string"},
            "clarification_question": {"type": ["string", "null"]},
            "clarification_options": CLARIFICATION_OPTIONS_SCHEMA,
        },
        "required": ["clarification_question"],
        "additionalProperties": False,
    }

    def execute(self, request: TaskPlanClassificationRequest) -> TaskPlanClassificationResponse:
        system = self.prompt.render(
            metadata_summary=request.metadataSummary,
            task_title=request.taskTitle,
            task_description=request.taskDescription,
            finalized_spec=request.finalizedSpec or "(none — the task skipped clarification)",
            plan=request.plan,
            importance_context=_importance_context(request.important),
        )
        result = self._single_shot(
            system, "Classify this finalized plan.",
            output_schema=self.OUTPUT_SCHEMA, max_tokens=1024,
            task_id=request.taskId, owner_id=request.userId, **_conversation_kwargs(request),
        )
        if result.usage_limits and not result.output_text:
            return self._usage_limited(TaskPlanClassificationResponse, request, result)
        data = parse_json_output(result)
        try:
            verdict = PlanClassificationVerdict(data.get("verdict"))
        except ValueError:
            verdict = PlanClassificationVerdict.Compound
        return TaskPlanClassificationResponse(
            taskId=request.taskId,
            verdict=verdict,
            rationale=data.get("rationale", ""),
            clarificationQuestion=data.get("clarification_question"),
            clarificationOptions=parse_clarification_options(
                data.get("clarification_options"), data.get("clarification_question")),
            usage=result.usage,
            modelId=self.model.model_id,
            promptId=self.prompt.prompt_id,
            conversationId=result.conversation_id,
            usageLimitSignals=result.usage_limits,
        )


class TaskRevisionClassificationAgent(TaskActionAgent):
    """The Revision Classification role (task-revisions spec §7): given the old
    and new card content and the artifacts derived from the old one, name the
    rewind point — the earliest stage the edit invalidates. Router tier,
    read-only, single-shot."""

    OUTPUT_SCHEMA = {
        "type": "object",
        "properties": {
            "rewind_to": {"type": "string", "enum": [p.value for p in RewindPoint]},
            "change_summary": {"type": "string"},
            "rationale": {"type": "string"},
            "clarification_question": {"type": ["string", "null"]},
            "clarification_options": CLARIFICATION_OPTIONS_SCHEMA,
        },
        "required": ["clarification_question"],
        "additionalProperties": False,
    }

    def execute(self, request: TaskRevisionClassificationRequest
                ) -> TaskRevisionClassificationResponse:
        system = self.prompt.render(
            metadata_summary=request.metadataSummary,
            previous_title=request.previousTitle,
            previous_description=request.previousDescription or "(empty)",
            new_title=request.taskTitle,
            new_description=request.taskDescription or "(empty)",
            diff=request.diff or "(no textual difference)",
            current_status=request.currentStatus,
            task_type=request.taskType or "(not yet classified)",
            importance_context=_importance_context(request.important),
            finalized_spec=request.finalizedSpec or "(none)",
            passing_criteria=request.passingCriteria or "(none)",
            plan=request.plan or "(none)",
            repo_summary=request.repoSummary or "(no code yet)",
            open_question=request.openQuestion or "(none)",
            guidance=request.guidance or "(none)",
        )
        result = self._single_shot(
            system, "Where does this edit rewind the task to?",
            output_schema=self.OUTPUT_SCHEMA, max_tokens=1024,
            task_id=request.taskId, owner_id=request.userId,
            **_conversation_kwargs(request),
        )
        if result.usage_limits and not result.output_text:
            return self._usage_limited(TaskRevisionClassificationResponse, request, result)
        data = parse_json_output(result)
        try:
            rewind = RewindPoint(data.get("rewind_to"))
        except ValueError:
            rewind = RewindPoint.Classification
        return TaskRevisionClassificationResponse(
            taskId=request.taskId,
            rewindTo=rewind,
            changeSummary=data.get("change_summary", ""),
            rationale=data.get("rationale", ""),
            clarificationQuestion=data.get("clarification_question"),
            clarificationOptions=parse_clarification_options(
                data.get("clarification_options"), data.get("clarification_question")),
            usage=result.usage,
            modelId=self.model.model_id,
            promptId=self.prompt.prompt_id,
            conversationId=result.conversation_id,
            usageLimitSignals=result.usage_limits,
        )


class TaskRepoScopingAgent(TaskActionAgent):
    """Repo scoping (multi-repo-project spec §7.3): which candidate member
    repos a task's spec/plan actually touches. Read-only, single-shot."""

    OUTPUT_SCHEMA = {
        "type": "object",
        "properties": {
            "affected_repo_ids": {"type": "array", "items": {"type": "string"}},
            "rationale": {"type": "string"},
        },
        "required": ["affected_repo_ids"],
        "additionalProperties": False,
    }

    def execute(self, request: TaskRepoScopingRequest) -> TaskRepoScopingResponse:
        repo_lines = "\n".join(
            f"- {r.get('repoId')}: {r.get('title')} — {r.get('role') or 'unspecified role'}"
            for r in request.candidateRepos
        )
        system = self.prompt.render(
            metadata_summary=request.metadataSummary,
            task_title=request.taskTitle,
            task_description=request.taskDescription,
            spec_text=request.specText or "(no finalized spec — use the description)",
            plan=request.plan or "(no plan)",
            candidate_repos=repo_lines,
        )
        result = self._single_shot(
            system, "Which repositories does this task touch?",
            output_schema=self.OUTPUT_SCHEMA, max_tokens=1024,
            task_id=request.taskId, owner_id=request.userId,
        )
        if result.usage_limits and not result.output_text:
            return self._usage_limited(TaskRepoScopingResponse, request, result)
        data = parse_json_output(result)
        valid = {r.get("repoId") for r in request.candidateRepos}
        affected = [rid for rid in data.get("affected_repo_ids", []) if rid in valid]
        return TaskRepoScopingResponse(
            taskId=request.taskId,
            affectedRepoIds=affected,
            rationale=data.get("rationale", ""),
            usage=result.usage,
            modelId=self.model.model_id,
            promptId=self.prompt.prompt_id,
            usageLimitSignals=result.usage_limits,
        )


class TaskPassingCriteriaAgent(TaskActionAgent):
    """The Passing Criteria checkpoint (finalization-passing-criteria spec §6):
    from the task's specification alone (no plan, no code), enumerate an
    exhaustive list of acceptance criteria. Read-only, single-shot."""

    OUTPUT_SCHEMA = {
        "type": "object",
        "properties": {
            "criteria": {"type": "array", "items": {"type": "string"}},
            "rationale": {"type": "string"},
            "clarification_question": {"type": ["string", "null"]},
            "clarification_options": CLARIFICATION_OPTIONS_SCHEMA,
        },
        "required": ["clarification_question"],
        "additionalProperties": False,
    }

    def execute(self, request: TaskPassingCriteriaRequest) -> TaskPassingCriteriaResponse:
        system = self.prompt.render(
            metadata_summary=request.metadataSummary,
            task_title=request.taskTitle,
            spec_text=request.specText,
        )
        system = _with_revision_context(system, request)
        # Deliberately no workspace: criteria derive from the spec alone
        # (finalization-passing-criteria spec §2, execution-tier-agents §9.6)
        # — on claude_agent_sdk this degrades to the tmp-file-answer path.
        result = self._single_shot(
            system, "Enumerate the passing criteria.",
            output_schema=self.OUTPUT_SCHEMA, max_tokens=8192,
            task_id=request.taskId, owner_id=request.userId, **_conversation_kwargs(request),
        )
        if result.usage_limits and not result.output_text:
            return self._usage_limited(TaskPassingCriteriaResponse, request, result)
        data = parse_json_output(result)
        return TaskPassingCriteriaResponse(
            taskId=request.taskId,
            criteria=list(data.get("criteria") or []),
            rationale=data.get("rationale", ""),
            clarificationQuestion=data.get("clarification_question"),
            clarificationOptions=parse_clarification_options(
                data.get("clarification_options"), data.get("clarification_question")),
            usage=result.usage,
            modelId=self.model.model_id,
            promptId=self.prompt.prompt_id,
            conversationId=result.conversation_id,
            usageLimitSignals=result.usage_limits,
        )


class TaskFinalizationAgent(TaskActionAgent):
    """The Spec Model. Generates clarifying questions, or a finalized spec once
    the answers are sufficient. Read-only repo access: metadata in the prompt,
    plus a live read-only workspace when one is provisioned (§9.6)."""

    # Deliberately no clarification_question here: this role's entire job is
    # asking clarifying questions — its `questions` list IS the pause signal
    # (conversation-lifecycle spec §13 resolution).
    OUTPUT_SCHEMA = {
        "type": "object",
        "properties": {
            "questions": {"type": "array", "items": {"type": "string"}},
            "finalized_spec": {"type": ["string", "null"]},
        },
        "required": ["questions", "finalized_spec"],
        "additionalProperties": False,
    }

    def execute(self, request: TaskFinalizationRequest) -> TaskFinalizationResponse:
        system = self.prompt.render(
            metadata_summary=request.metadataSummary,
            task_title=request.taskTitle,
            task_description=request.taskDescription,
            prior_qa=request.priorQuestionsAndAnswers or "(none)",
        )
        system = _with_revision_context(system, request)
        result = self._agentic(
            system, "Clarify or finalize this task.",
            output_schema=self.OUTPUT_SCHEMA, max_tokens=4096,
            workspace_path=request.workspacePath,
            task_id=request.taskId, owner_id=request.userId, **_conversation_kwargs(request),
        )
        if result.usage_limits and not result.output_text:
            return self._usage_limited(TaskFinalizationResponse, request, result)
        data = parse_json_output(result)
        return TaskFinalizationResponse(
            taskId=request.taskId,
            questions=data.get("questions", []),
            finalizedSpec=data.get("finalized_spec"),
            usage=result.usage,
            modelId=self.model.model_id,
            promptId=self.prompt.prompt_id,
            conversationId=result.conversation_id,
            usageLimitSignals=result.usage_limits,
        )


class TaskPlanningAgent(TaskActionAgent):
    """The Planning Model (Opus tier). Produces the implementation plan, or a
    corrected plan from a situation report on E2 escalation — or pauses on a
    single tradeoff question only a human can settle (conversation-lifecycle
    spec §2)."""

    # The wrapper that replaced the bare plain-text completion (spec §4.4).
    # NOTE: on single_shot this combines output_config.json_schema with
    # thinking + streaming on a long-output role — flagged in the spec as
    # needing a live spike; the default claude_agent_sdk harness sidesteps it
    # (the JSON travels via the scratch answer file, no output_config).
    OUTPUT_SCHEMA = {
        "type": "object",
        "properties": {
            "plan": {"type": ["string", "null"]},
            "clarification_question": {"type": ["string", "null"]},
            "clarification_options": CLARIFICATION_OPTIONS_SCHEMA,
        },
        "required": ["plan", "clarification_question"],
        "additionalProperties": False,
    }

    def execute(self, request: TaskPlanningRequest) -> TaskPlanningResponse:
        system = self.prompt.render(
            metadata_summary=request.metadataSummary,
            task_title=request.taskTitle,
            task_description=request.taskDescription,
            finalized_spec=request.finalizedSpec
            or "(none — the task needed no clarification; the description is the spec)",
            passing_criteria=request.passingCriteria or "(none generated)",
            situation_report=request.situationReport or "(none)",
        )
        system = _with_revision_context(system, request)
        result = self._agentic(
            system, "Produce the implementation plan.",
            output_schema=self.OUTPUT_SCHEMA,
            max_tokens=32000, adaptive_thinking=True,
            workspace_path=request.workspacePath,
            task_id=request.taskId, owner_id=request.userId, **_conversation_kwargs(request),
        )
        if result.usage_limits and not result.output_text:
            return self._usage_limited(TaskPlanningResponse, request, result)
        try:
            data = parse_json_output(result)
        except ValueError:
            # A long-output role that failed the wrapper contract still
            # produced a plan-shaped text — degrade to the pre-wrapper
            # behavior rather than losing the work (spec §13 resolution).
            log.warning("planning output was not wrapper JSON; treating the "
                        "whole output as the plan", extra={"task_id": request.taskId})
            data = {"plan": result.output_text, "clarification_question": None}
        return TaskPlanningResponse(
            taskId=request.taskId,
            plan=data.get("plan") or "",
            clarificationQuestion=data.get("clarification_question"),
            clarificationOptions=parse_clarification_options(
                data.get("clarification_options"), data.get("clarification_question")),
            usage=result.usage,
            modelId=self.model.model_id,
            promptId=self.prompt.prompt_id,
            conversationId=result.conversation_id,
            usageLimitSignals=result.usage_limits,
        )


class TaskExecutionAgent(TaskActionAgent):
    """The Coding Model — the only role with write access. A thin adapter
    between the TaskExecutionRequest/Response contract and the harness-neutral
    HarnessTaskSpec/HarnessResult contract: the tool loop itself lives in the
    configured Harness (sprintbaton/harness/). Model tier still comes from the
    request, flowing through ModelSpec (escalation ladder E0 Sonnet -> E3 Opus)."""

    def execute(self, request: TaskExecutionRequest) -> TaskExecutionResponse:
        system = self.prompt.render(
            metadata_summary=request.metadataSummary,
            task_title=request.taskTitle,
            task_description=request.taskDescription,
            plan=request.plan or request.spec or "(none — descriptors are sufficient)",
            passing_criteria=request.passingCriteria or "(none generated)",
            situation_report=request.situationReport or "(none)",
        )
        system = _with_revision_context(system, request)
        # The one role that builds its spec directly instead of going through
        # _agentic — the resume/restart branch is threaded by hand
        # (conversation-lifecycle spec §7.3).
        user_message = DEFAULT_USER_MESSAGE
        if request.conversationId and request.replyText is not None:
            user_message = request.replyText
        elif request.clarificationContext:
            system = (f"{system}\n\n## Prior human clarification\n"
                      f"{request.clarificationContext}")
        spec = HarnessTaskSpec(
            system_prompt=system,
            user_message=user_message,
            workspace_path=request.workspacePath,
            guardrails=DEFAULT_GUARDRAIL_POLICY,
            max_iterations=MAX_EXECUTION_ITERATIONS,
            situation_report=request.situationReport,
            conversation_id=request.conversationId,
            # The Coding Model is the only role with write access — the one
            # caller that flips read_only off (execution-tier-agents spec §9.4).
            read_only=False,
            subscription_auth=self.subscription_auth,
            task_id=request.taskId, owner_id=request.userId,
        )
        # Only the model id varies per tier — the resolved credential
        # (per-user-provider-credentials spec §4.2) must survive the rebuild,
        # or the harness would fall back to ambient env auth, which the worker
        # process no longer has (agent-sdk-subscription-auth spec §3).
        model = dataclasses.replace(
            self.model, model_id=request.modelId or self.model.model_id)
        started = time.monotonic()
        result = self.harness.execute(spec, model)

        log.info("execution finished", extra={
            "task_id": request.taskId, "model": model.model_id,
            "harness": self.harness.name,
            "seconds": round(time.monotonic() - started),
            "completed": result.completed,
        })
        return TaskExecutionResponse(
            taskId=request.taskId,
            summary=result.summary,
            completed=result.completed,
            askedQuestion=result.asked_question,
            clarificationQuestion=result.clarification_question,
            clarificationOptions=parse_clarification_options(
                result.clarification_options, result.clarification_question),
            planBroken=result.plan_broken,
            importanceFlags=result.importance_flags,
            filesEdited=result.files_edited,
            consecutiveCheckFailures=result.consecutive_check_failures,
            diff=result.diff,
            usage=result.usage,
            modelId=model.model_id,
            promptId=self.prompt.prompt_id,
            conversationId=result.conversation_id,
            usageLimitSignals=result.usage_limits,
            # Whether guard.py actually ran for this turn (subprocess-cli-
            # write-parity spec §7.3) — carried to the TaskActionEvent stream.
            guardrailEnforced=result.guardrail_enforced,
        )


class TaskConflictResolutionAgent(TaskActionAgent):
    """Resolves a merge conflict against devBranch on the task's own branch —
    a second, narrowly-scoped write-capable role (conflict-resolution spec §6).
    Same HarnessTaskSpec shape as TaskExecutionAgent (read_only=False); the
    'behave like a developer resolving a conflict by hand' framing lives
    entirely in the prompt (§7.4), since no harness exposes a native
    goal/outcome primitive today (§7.5)."""

    def execute(self, request: TaskConflictResolutionRequest) -> TaskConflictResolutionResponse:
        system = self.prompt.render(
            metadata_summary=request.metadataSummary,
            task_title=request.taskTitle,
            task_description=request.taskDescription,
            execution_summary=request.executionSummary,
            conflicted_files="\n".join(request.conflictedFiles),
            passing_criteria=request.passingCriteria or "(none generated)",
            situation_report=request.situationReport or "(none — first attempt)",
        )
        spec = HarnessTaskSpec(
            system_prompt=system,
            user_message=DEFAULT_USER_MESSAGE,
            workspace_path=request.workspacePath,
            guardrails=DEFAULT_GUARDRAIL_POLICY,
            max_iterations=MAX_EXECUTION_ITERATIONS,
            read_only=False,   # the second write-capable role (§6)
            subscription_auth=self.subscription_auth,
            task_id=request.taskId, owner_id=request.userId,
        )
        result = self.harness.execute(spec, self.model)
        return TaskConflictResolutionResponse(
            taskId=request.taskId,
            resolved=result.completed,   # finish(completed=true) == merge is clean and committable
            summary=result.summary,
            clarificationQuestion=result.clarification_question,
            clarificationOptions=parse_clarification_options(
                result.clarification_options, result.clarification_question),
            filesEdited=result.files_edited,
            usage=result.usage,
            modelId=self.model.model_id,
            promptId=self.prompt.prompt_id,
            conversationId=result.conversation_id,
            usageLimitSignals=result.usage_limits,
            guardrailEnforced=result.guardrail_enforced,
        )


class TaskReviewAgent(TaskActionAgent):
    OUTPUT_SCHEMA = {
        "type": "object",
        "properties": {
            "approved": {"type": "boolean"},
            "findings": {"type": "array", "items": {"type": "string"}},
            "clarification_question": {"type": ["string", "null"]},
            "clarification_options": CLARIFICATION_OPTIONS_SCHEMA,
        },
        "required": ["clarification_question"],
        "additionalProperties": False,
    }

    def execute(self, request: TaskReviewRequest) -> TaskReviewResponse:
        system = self.prompt.render(
            metadata_summary=request.metadataSummary,
            task_title=request.taskTitle,
            task_description=request.taskDescription,
            passing_criteria=request.passingCriteria or "(none generated)",
            diff=request.diff[:100_000],
        )
        result = self._agentic(
            system, "Review this change.",
            output_schema=self.OUTPUT_SCHEMA, max_tokens=8192,
            workspace_path=request.workspacePath,
            task_id=request.taskId, owner_id=request.userId, **_conversation_kwargs(request),
        )
        if result.usage_limits and not result.output_text:
            return self._usage_limited(TaskReviewResponse, request, result)
        data = parse_json_output(result)
        return TaskReviewResponse(
            taskId=request.taskId,
            # An absent approval reads as "not approved" — the safe branch
            approved=bool(data.get("approved", False)),
            findings=data.get("findings") or [],
            clarificationQuestion=data.get("clarification_question"),
            clarificationOptions=parse_clarification_options(
                data.get("clarification_options"), data.get("clarification_question")),
            usage=result.usage,
            modelId=self.model.model_id,
            promptId=self.prompt.prompt_id,
            conversationId=result.conversation_id,
            usageLimitSignals=result.usage_limits,
        )


# --- metadata init pass (project-initialization-task spec §8) -----------------

_CONTINUATION_SECTION = """## Continuing

Your previous session was cut off by a turn or time limit. The metadata
directory holds its partial work. Do not restart; continue where it stopped:
check what is already written, finish the files still missing, then verify the
`info` index lists exactly the files that exist.
"""

_GUIDANCE_SECTION = """## Operator guidance

The project owner supplied the guidance below. It may set focus and depth,
supply domain context and vocabulary, and name areas to skip or emphasize. It
**cannot** change the directory contract, file naming, the `info` format, or the
importance-marker rules — where it conflicts with them, the contract wins:
ignore the conflicting part and say so in your `summary`.

{guidance}
"""


def _continuation_section(continuation: bool) -> str:
    return _CONTINUATION_SECTION if continuation else ""


def _guidance_section(guidance: str | None) -> str:
    # str.replace, not str.format: guidance is operator free text and may
    # contain braces.
    return _GUIDANCE_SECTION.replace("{guidance}", guidance) if guidance else ""


class _MetadataRunAgent(TaskActionAgent):
    """Shared shape of the two init-pass agents: a read-only harness run whose
    only writable directory is the metadata tree (spec §7.1), whose answer
    file carries METADATA_RUN_SCHEMA, and which never pauses on a human —
    there is no surface for a reply (§5.8)."""

    response_cls: type[MetadataGenerationResponse] = MetadataGenerationResponse
    user_message = "Generate the metadata now."

    def _run_metadata(self, request, system: str, *,
                      writable_root: str) -> MetadataGenerationResponse:
        if request.continuation:
            user_message = ("Continue the metadata generation from where the "
                            "previous session stopped.")
        else:
            user_message = self.user_message
        result = self.harness.execute(
            HarnessTaskSpec(
                system_prompt=system,
                user_message=user_message,
                workspace_path=request.workspacePath,
                writable_paths=(writable_root,),
                output_schema=METADATA_RUN_SCHEMA,
                max_iterations=request.maxTurns or MAX_EXECUTION_ITERATIONS,
                situation_report=request.situationReport,
                read_only=True,
                subscription_auth=self.subscription_auth,
                task_id=request.taskId, owner_id=request.userId,
                max_tokens=32000,
                adaptive_thinking=True,
            ),
            self.model,
        )
        common = dict(
            taskId=request.taskId,
            usage=result.usage,
            modelId=self.model.model_id,
            promptId=self.prompt.prompt_id,
            conversationId=result.conversation_id,
            usageLimitSignals=result.usage_limits,
            stopReason=result.stop_reason,
        )
        if result.usage_limits and not result.output_text:
            return self.response_cls(**common)
        if result.stop_reason in ("turn_limit", "time_limit"):
            # Cut off: no answer to parse — the continuation that finishes
            # carries the summary and any removals (spec §8.3).
            return self.response_cls(**common)
        try:
            data = parse_json_output(result)
        except ValueError:
            # The files are the output; a malformed answer only loses the
            # summary/removals. Contract validation decides whether the run
            # actually produced usable metadata.
            log.warning("metadata run answer was not JSON", extra={
                "task_id": request.taskId, "harness": self.harness.name})
            data = {}
        if not isinstance(data, dict):
            data = {}
        removed = data.get("removedFiles") or []
        return self.response_cls(
            **common,
            summary=str(data.get("summary") or ""),
            removedFiles=[str(p) for p in removed if isinstance(p, str)],
        )


class TaskMetadataGenerationAgent(_MetadataRunAgent):
    """The per-repo init pass (spec §8.1): explores one clone, edits its
    pre-seeded `.sprintbaton/` in place."""

    def execute(self, request: MetadataGenerationRequest) -> MetadataGenerationResponse:
        system = self.prompt.render(
            location_guide=request.locationGuide,
            repo_title=request.repoTitle,
            repo_role=request.repoRole or "unspecified",
            continuation_section=_continuation_section(request.continuation),
            guidance_section=_guidance_section(request.metadataGuidance),
            situation_report=request.situationReport or "(none)",
        )
        return self._run_metadata(request, system, writable_root=request.writableRoot)


class TaskProjectMetadataGenerationAgent(_MetadataRunAgent):
    """The project-index init pass (spec §8.2): edits the project metadata
    directory — its own working directory — in place."""

    response_cls = ProjectMetadataGenerationResponse
    user_message = "Generate the project metadata index now."

    def execute(self, request: ProjectMetadataGenerationRequest
                ) -> ProjectMetadataGenerationResponse:
        system = self.prompt.render(
            location_guide=request.locationGuide,
            project_title=request.projectTitle,
            repo_summaries=request.repoSummaries,
            continuation_section=_continuation_section(request.continuation),
            guidance_section=_guidance_section(request.metadataGuidance),
            situation_report=request.situationReport or "(none)",
        )
        return self._run_metadata(request, system, writable_root=request.workspacePath)

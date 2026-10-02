"""TaskActionRequestAdapters (Task -> TaskActionRequest) and
TaskActionResponseAdapters (TaskActionResponse -> todolist comment bodies)."""

from sprintbaton.clarification.translator import ClarificationTranslator
from sprintbaton.entities.actions import (
    TaskClassificationRequest,
    TaskConflictResolutionRequest,
    TaskExecutionRequest,
    TaskExecutionResponse,
    TaskFinalizationRequest,
    TaskFinalizationResponse,
    TaskPassingCriteriaRequest,
    TaskPassingCriteriaResponse,
    TaskPlanClassificationRequest,
    TaskPlanningRequest,
    TaskPlanningResponse,
    TaskRepoScopingRequest,
    TaskRevisionClassificationRequest,
    TaskReviewRequest,
    TaskReviewResponse,
    TaskSpecClassificationRequest,
)
from sprintbaton.entities.project import Project
from sprintbaton.entities.repository import Repository
from sprintbaton.entities.task import Task


def _base_fields(task: Task, owner: "Project | Repository", metadata_summary: str, *,
                 conversation_id: str | None = None,
                 reply_text: str | None = None,
                 clarification_context: str | None = None,
                 selected_answer: str | None = None,
                 selected_answers: list[str] | None = None,
                 amendment=None) -> dict:
    """`owner` is a Project for the seven project-scoped adapters and a
    Repository for the three per-repo ones — hence the deliberately loose
    annotation; `repoId` carries whichever id, and nothing reads it."""
    return {
        "taskId": task.id,
        "userId": task.userId,
        "repoId": owner.id,
        "taskTitle": task.title,
        "taskDescription": task.description or "",
        "metadataSummary": metadata_summary,
        # Conversation-lifecycle threading (spec §4.3): conversation_id is the
        # harness session id, set together with reply_text on a resume attempt.
        "conversationId": conversation_id,
        "replyText": reply_text,
        "clarificationContext": clarification_context,
        # Best-effort reply-to-option matches (clarification-options spec §4.4)
        "selectedAnswer": selected_answer,
        "selectedAnswers": selected_answers,
        # Revisions context (task-revisions spec §7.6/§9.3)
        "amendment": amendment,
        "priorRound": task.priorRound,
    }


class TaskClassificationRequestAdapter:
    def adapt(self, task: Task, repo: Repository, metadata_summary: str) -> TaskClassificationRequest:
        # The Router is excluded from the clarification rollout (conversation-
        # lifecycle spec §13 resolution) — no conversation fields.
        return TaskClassificationRequest(**_base_fields(task, repo, metadata_summary))


class TaskFinalizationRequestAdapter:
    def adapt(self, task: Task, repo: Repository, metadata_summary: str,
              prior_qa: str, workspace_path: str = "",
              **conversation_kwargs) -> TaskFinalizationRequest:
        return TaskFinalizationRequest(
            **_base_fields(task, repo, metadata_summary, **conversation_kwargs),
            priorQuestionsAndAnswers=prior_qa,
            workspacePath=workspace_path,
        )


class TaskSpecClassificationRequestAdapter:
    def adapt(self, task: Task, repo: Repository, metadata_summary: str,
              finalized_spec: str, **conversation_kwargs) -> TaskSpecClassificationRequest:
        return TaskSpecClassificationRequest(
            **_base_fields(task, repo, metadata_summary, **conversation_kwargs),
            finalizedSpec=finalized_spec,
            important=task.important,
        )


class TaskPassingCriteriaRequestAdapter:
    def adapt(self, task: Task, repo: Repository, metadata_summary: str,
              spec_text: str, **conversation_kwargs) -> TaskPassingCriteriaRequest:
        return TaskPassingCriteriaRequest(
            **_base_fields(task, repo, metadata_summary, **conversation_kwargs),
            specText=spec_text,
        )


class TaskPlanClassificationRequestAdapter:
    def adapt(self, task: Task, repo: Repository, metadata_summary: str,
              finalized_spec: str, plan: str,
              **conversation_kwargs) -> TaskPlanClassificationRequest:
        return TaskPlanClassificationRequest(
            **_base_fields(task, repo, metadata_summary, **conversation_kwargs),
            finalizedSpec=finalized_spec,
            plan=plan,
            important=task.important,
        )


class TaskRevisionClassificationRequestAdapter:
    """taskTitle/taskDescription (from _base_fields) carry the NEW content —
    the caller has not accepted the revision yet, so they come from `new`."""

    def adapt(self, task: Task, owner, metadata_summary: str, *, previous, new,
              diff: str, finalized_spec: str, passing_criteria: str, plan: str,
              repo_summary: str, open_question: str | None, guidance: str | None,
              **conversation_kwargs) -> TaskRevisionClassificationRequest:
        fields = _base_fields(task, owner, metadata_summary, **conversation_kwargs)
        fields.update(taskTitle=new.title, taskDescription=new.description or "")
        return TaskRevisionClassificationRequest(
            **fields,
            previousTitle=previous.title,
            previousDescription=previous.description or "",
            diff=diff,
            currentStatus=str(task.status),
            taskType=str(task.type or ""),
            important=task.important,
            finalizedSpec=finalized_spec,
            passingCriteria=passing_criteria,
            plan=plan,
            repoSummary=repo_summary,
            openQuestion=open_question,
            guidance=guidance,
        )


class TaskRepoScopingRequestAdapter:
    def adapt(self, task: Task, owner, metadata_summary: str, *,
              spec_text: str, plan: str,
              candidate_repos: list[dict]) -> "TaskRepoScopingRequest":
        return TaskRepoScopingRequest(
            **_base_fields(task, owner, metadata_summary),
            specText=spec_text,
            plan=plan,
            candidateRepos=candidate_repos,
        )


class TaskPlanningRequestAdapter:
    def adapt(self, task: Task, repo: Repository, metadata_summary: str,
              situation_report: str | None = None,
              workspace_path: str = "", finalized_spec: str = "",
              passing_criteria: str = "",
              **conversation_kwargs) -> TaskPlanningRequest:
        return TaskPlanningRequest(
            **_base_fields(task, repo, metadata_summary, **conversation_kwargs),
            situationReport=situation_report,
            finalizedSpec=finalized_spec,
            passingCriteria=passing_criteria,
            workspacePath=workspace_path,
        )


class TaskExecutionRequestAdapter:
    def adapt(self, task: Task, repo: Repository, metadata_summary: str, *,
              workspace_path: str, model_id: str, plan: str | None = None,
              spec: str | None = None, passing_criteria: str | None = None,
              situation_report: str | None = None,
              **conversation_kwargs) -> TaskExecutionRequest:
        return TaskExecutionRequest(
            **_base_fields(task, repo, metadata_summary, **conversation_kwargs),
            workspacePath=workspace_path,
            modelId=model_id,
            plan=plan,
            spec=spec,
            passingCriteria=passing_criteria,
            situationReport=situation_report,
        )


class TaskReviewRequestAdapter:
    def adapt(self, task: Task, repo: Repository, metadata_summary: str,
              workspace_path: str, diff: str,
              passing_criteria: str | None = None,
              **conversation_kwargs) -> TaskReviewRequest:
        return TaskReviewRequest(
            **_base_fields(task, repo, metadata_summary, **conversation_kwargs),
            workspacePath=workspace_path,
            diff=diff,
            passingCriteria=passing_criteria,
        )


class TaskConflictResolutionRequestAdapter:
    def adapt(self, task: Task, repo: Repository, metadata_summary: str, *,
              workspace_path: str, conflicted_files: list[str],
              execution_summary: str, passing_criteria: str | None = None,
              situation_report: str | None = None,
              **conversation_kwargs) -> TaskConflictResolutionRequest:
        return TaskConflictResolutionRequest(
            **_base_fields(task, repo, metadata_summary, **conversation_kwargs),
            workspacePath=workspace_path,
            conflictedFiles=conflicted_files,
            executionSummary=execution_summary,
            passingCriteria=passing_criteria,
            situationReport=situation_report,
        )


# --- Response adapters: model output -> provider comment bodies -------------

class TaskFinalizationResponseAdapter:
    def __init__(self, translator: ClarificationTranslator):
        self._translator = translator

    def to_comment(self, response: TaskFinalizationResponse) -> str | None:
        if response.questions:
            return self._translator.render_questions(response.questions)
        return None


class TaskPlanningResponseAdapter:
    def __init__(self, translator: ClarificationTranslator):
        self._translator = translator

    def to_comment(self, response: TaskPlanningResponse, plan_url: str) -> str:
        return self._translator.render_plan_notice(plan_url)


class TaskExecutionResponseAdapter:
    def __init__(self, translator: ClarificationTranslator):
        self._translator = translator

    def to_comment(self, response: TaskExecutionResponse, pr_url: str) -> str:
        return self._translator.render_pr_notice(pr_url, response.summary)


class TaskPassingCriteriaResponseAdapter:
    """Renders the criteria list into the markdown artifact persisted on
    Task.taskPassingCriteriaAction (machine-only — never a provider comment)."""

    @staticmethod
    def to_markdown(response: TaskPassingCriteriaResponse) -> str:
        return "\n".join(f"- {c}" for c in response.criteria)


class TaskReviewResponseAdapter:
    def to_comment(self, response: TaskReviewResponse) -> str:
        status = "approved ✅" if response.approved else "changes requested ❌"
        lines = [f"🤖 **SprintBaton review:** {status}"]
        lines += [f"- {f}" for f in response.findings]
        return "\n".join(lines)

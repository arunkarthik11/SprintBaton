"""ContextSnapshotService — turns a classification decision into a reproducible
snapshot + a ClassificationRecord (docs/classification-provenance-spec.md §4.2).

Every public method is best-effort: a MinIO / git / Redis failure is logged and
swallowed, never raised into the orchestrator. Snapshotting is a side-channel
for offline calibration — it must never fail a task.
"""

import json
import logging

from sprintbaton.entities.base import now_millis
from sprintbaton.entities.provenance import ClassificationRecord
from sprintbaton.entities.task import Task
from sprintbaton.prompts.registry import PROMPT_VERSION
from sprintbaton.provenance.format import PROVENANCE_FORMAT_VERSION
from sprintbaton.provenance.git_store import ProvenanceGitStore
from sprintbaton.storage.base import EntityDAO
from sprintbaton.workspace.task_workspace import TaskWorkspaceService

log = logging.getLogger(__name__)


class ContextSnapshotService:
    def __init__(self, git_store: ProvenanceGitStore,
                 task_workspace: TaskWorkspaceService,
                 record_repo: EntityDAO[ClassificationRecord],
                 *, enabled: bool = True):
        self._git = git_store
        self._workspace = task_workspace
        self._records = record_repo
        self._enabled = enabled

    # ------------------------------------------------------------- write path

    def record_classification(self, project: str, task: Task, repo, *,
                              action: str, response, metadata_summary: str,
                              upstream_sha: str) -> ClassificationRecord | None:
        """Snapshot the context this classification saw and persist a
        ClassificationRecord referencing it. Returns the record, or None when
        disabled or on any failure."""
        if not self._enabled:
            return None
        category = self._enum_str(getattr(response, "category", None))
        verdict = self._enum_str(getattr(response, "verdict", None))
        rationale = getattr(response, "rationale", "") or ""
        provenance = {
            "provenanceVersion": PROVENANCE_FORMAT_VERSION,
            "action": action,
            "taskId": task.id,
            "taskType": self._enum_str(task.type),
            "important": task.important,
            "upstreamSha": upstream_sha,
            "modelId": response.modelId,
            "promptId": response.promptId,
            "promptVersion": PROMPT_VERSION,
            "category": category,
            "verdict": verdict,
            "rationale": rationale,
            "usage": response.usage.model_dump(),
            "timestamp": now_millis(),
        }
        branch = f"task/{task.id}"
        try:
            files = self._workspace.snapshot_files(project, task)
            prefixed = {f"tasks/{task.id}/{name}": content
                        for name, content in files.items()}
            prefixed[f"tasks/{task.id}/inputs/{action}.md"] = self._render_inputs(
                task, action, metadata_summary)
            prefixed[f"tasks/{task.id}/provenance-{action}.json"] = json.dumps(
                provenance, indent=2, sort_keys=True)
            ref = self._git.commit(
                task.userId, project, branch, prefixed,
                message=f"{action} snapshot for task {task.id}")
        except Exception:
            log.warning("provenance git snapshot failed",
                        extra={"task_id": task.id, "action": action})
            return None

        record = ClassificationRecord(
            userId=task.userId,
            taskId=task.id, repoId=task.repoId, project=project, action=action,
            category=category, verdict=verdict, important=task.important,
            rationale=rationale,
            upstreamSha=upstream_sha,
            snapshotBranch=ref.branch, snapshotCommitSha=ref.commit_sha,
            snapshotBundleUrl=ref.bundle_url,
            modelId=response.modelId, promptId=response.promptId,
            promptVersion=PROMPT_VERSION,
            provenanceVersion=PROVENANCE_FORMAT_VERSION,
            usage=response.usage,
        )
        try:
            self._records.save(record)
        except Exception:
            log.warning("classification record save failed",
                        extra={"task_id": task.id, "action": action})
            return None
        return record

    def stamp_outcome(self, task: Task) -> None:
        """Join every ClassificationRecord for the task to its final outcome
        (spec §4.2) so an AI judge has a label, not just an input. Called at
        Shipped / Blocked. Best-effort."""
        if not self._enabled:
            return
        try:
            records = self._records.find(
                {"taskId": task.id, "userId": task.userId})
        except Exception:
            log.warning("classification record lookup failed",
                        extra={"task_id": task.id})
            return
        for record in records:
            record.outcomeStatus = self._enum_str(task.status)
            record.outcomeTier = self._enum_str(task.escalationTier)
            # Per-repo counters now live on RepoWork (multi-repo-project spec
            # §4.3) — aggregate across the task's repos for the outcome label.
            record.outcomeReviewFailures = sum(w.reviewFailures for w in task.repoWork)
            record.outcomeAiReviewRounds = sum(w.aiReviewRounds for w in task.repoWork)
            record.outcomeUpdatedTime = now_millis()
            record.touch()
            try:
                self._records.save(record)
            except Exception:
                log.warning("classification outcome stamp failed",
                            extra={"task_id": task.id, "record_id": record.id})

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def _enum_str(value) -> str:
        return str(value) if value is not None else ""

    @staticmethod
    def _render_inputs(task: Task, action: str, metadata_summary: str) -> str:
        """The verbatim context the single_shot classifier saw — for a
        spec-alone classifier this IS the complete reproducible input
        (spec §3)."""
        return (
            f"# Classifier inputs — {action}\n\n"
            f"task_id: {task.id}\n"
            f"category: {ContextSnapshotService._enum_str(task.type)}\n"
            f"important: {task.important}\n\n"
            f"## Title\n\n{task.title}\n\n"
            f"## Description\n\n{task.description or '(none)'}\n\n"
            f"## Repository metadata summary\n\n{metadata_summary}\n"
        )

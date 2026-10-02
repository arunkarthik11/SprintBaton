"""ReleaseWindowJob — the scheduled dev -> staging release cutover
(three-branch promotion spec §5).

A wall-clock sibling of PendingTaskPollingJob: on each check, for every active
Repository whose release cadence has elapsed, it sweeps the tasks whose review
PRs have merged into `dev` into one batch, opens a single dev -> staging
promotion PR, creates a Release entity for the batch, and moves every swept
task to QA together. The Repository row is the source of truth for cadence,
columns, and branches (repository-onboarding spec §9). Decisions baked in
(spec §6):

- Readiness = the task's review PR is merged (checked via the GitHub API at cut
  time); a merged task always ships with the next window.
- Releases are atomic: while a release sits InQA, no new window is cut for that
  repo — regression failures bounce individual tasks via the normal review
  mechanics, but the batch holds until sign-off.
- The cutover PR is merged by a human on GitHub (conflicts included);
  SprintBaton only opens it. The first window for a repo cuts as soon as ready
  tasks exist; subsequent windows wait out the cadence from the last cut.
"""

import logging
import threading
from datetime import date

from sprintbaton.entities.base import now_millis
from sprintbaton.orchestrator.board import move_card
from sprintbaton.entities.enums import ReleaseStatus, TaskStatus
from sprintbaton.entities.project import Project
from sprintbaton.entities.release import Release
from sprintbaton.entities.repository import Repository
from sprintbaton.entities.task import Task
from sprintbaton.services.base import ServiceContext

log = logging.getLogger(__name__)

MILLIS_PER_DAY = 86_400_000


class ReleaseWindowJob:
    def __init__(self, ctx: ServiceContext, interval_seconds: int = 3600):
        self._ctx = ctx
        self._interval = interval_seconds
        self._stop = threading.Event()

    # -------------------------------------------------------------- lifecycle

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self._loop, name="release-window-job", daemon=True)
        thread.start()
        return thread

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        log.info("release window job started", extra={"interval": self._interval})
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:
                log.exception("release window check failed")
            self._stop.wait(self._interval)

    # ------------------------------------------------------------------ work

    def run_once(self) -> list[Release]:
        cut = []
        # The Project row is the source of truth, across every user —
        # find() already excludes soft-deleted rows.
        for project in self._ctx.project_repo.find({"active": True}):
            try:
                if not self.window_due(project):
                    continue
                release = self.cut_release(project)
                if release is not None:
                    cut.append(release)
            except Exception:
                # One project's failure (bad credentials, dead board) must not
                # block every other tenant's window check.
                log.exception("release window check failed for project",
                              extra={"project_id": project.id})
        return cut

    def window_due(self, project: Project) -> bool:
        """Due when no batch is still under QA (atomic hold) and the cadence
        has elapsed since the last cut (immediately, for a project's first cut)."""
        releases = self._ctx.release_repo.find(
            {"projectId": project.id, "userId": project.userId})
        if any(r.status == ReleaseStatus.InQA for r in releases):
            return False
        if not releases:
            return True
        last_cut = max(r.createdTime for r in releases)
        cadence_millis = project.releaseCadenceDays * MILLIS_PER_DAY
        return now_millis() >= last_cut + cadence_millis

    def cut_release(self, project: Project) -> Release | None:
        repos = self._ctx.repositories_for(project)
        ready = self._ready_tasks(project, repos)
        if not ready:
            return None

        title = f"Release {date.today().isoformat()} — {project.title}"
        promotion_urls: dict[str, str] = {}
        # One dev -> staging promotion PR per member repo with commits to promote
        # (multi-repo-project spec §9). GitService.promote returns "" for a repo
        # with nothing to promote, which is naturally skipped.
        for repo in repos:
            pr = self._ctx.git_for(repo).promote(
                repo.githubRepo, from_branch=repo.devBranch, to_branch=repo.stagingBranch,
                title=f"{title}: {repo.devBranch} → {repo.stagingBranch}",
            )
            if pr:
                promotion_urls[repo.id] = pr
        release = Release(
            userId=project.userId,
            title=title, projectId=project.id,
            taskIds=[t.id for t in ready],
            promotionPrUrls=promotion_urls,
        )
        self._ctx.release_repo.save(release)

        adapter = self._ctx.task_adapter_for(project)
        any_pr = next(iter(promotion_urls.values()), None)
        for task in ready:
            from_status = task.status
            task.releaseId = release.id
            task.status = TaskStatus.QA
            # Our move, recorded as ours (task-revisions spec §8.1).
            move_card(self._ctx, task, project, project.columns.qa, force=True)
            adapter.add_comment(
                task.externalId,
                self._ctx.translator.render_qa_notice(title, any_pr),
            )
            self._ctx.observer.task_processed(str(task.type), "qa_promoted")
            self._ctx.analytics.record(task, action="qa_promoted", from_status=from_status)
            task.touch()
            self._ctx.task_repo.save(task)

        log.info("release cut", extra={"release_id": release.id, "project_id": project.id,
                                       "tasks": len(ready), "prs": promotion_urls})
        return release

    def _ready_tasks(self, project: Project, repos: list[Repository]) -> list[Task]:
        """In-review tasks whose feature PRs have ALL merged into their repos'
        dev branches (multi-repo-project spec §8.3 — atomic across repos)."""
        by_id = {r.id: r for r in repos}
        ready = []
        for task in self._ctx.task_repo.find({"projectId": project.id,
                                              "status": TaskStatus.InReview,
                                              "userId": project.userId}):
            works = [w for w in task.repoWork if w.prUrl]
            if not works:
                continue
            all_merged = True
            for work in works:
                repo = by_id.get(work.repoId)
                if repo is None:
                    all_merged = False
                    break
                git = self._ctx.git_for(repo)
                pr_number = git.pr_number_from_url(work.prUrl or "")
                if pr_number is None:
                    all_merged = False
                    break
                try:
                    if not git.pull_request_merged(repo.githubRepo, pr_number):
                        all_merged = False
                        break
                except Exception:
                    log.exception("merged check failed", extra={"task_id": task.id})
                    all_merged = False
                    break
            if all_merged:
                ready.append(task)
        return ready

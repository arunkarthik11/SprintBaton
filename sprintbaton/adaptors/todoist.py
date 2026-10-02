"""Todoist adapter (unified API v1 — https://developer.todoist.com/api/v1/).

REST API v2 and Sync API v9 were shut down on 2026-02-10; the unified v1 API
replaces both (repository-onboarding spec §11). Differences that shaped this
adapter, confirmed against the official `Doist/todoist-api-python` SDK source:

- List endpoints are cursor-paginated: `{"results": [...], "next_cursor": ...}`
  instead of v2's bare arrays — `_paginated` follows the cursor.
- Moving a task is a first-class `POST /tasks/{id}/move` (body `section_id`) —
  the old Sync-API `item_move` workaround is gone entirely.
- Responses use sync-flavoured field names: a task's assignee arrives as
  `responsible_uid`, a comment's author as `posted_uid` (v2 exposed no comment
  author at all — v1 lets ExternalComment.authorId be populated for real; the
  header-recognition fallback in the clarification lifecycle stays as a
  baseline). Request bodies still take `assignee_id`/`labels`/`task_id`.

Mapping notes:
- board_id  -> Todoist project id
- column_id -> Todoist section id
- assignee  -> Todoist collaborator id (shared projects only)
Labels are Todoist labels; attachments arrive as comment file attachments.
"""

import logging
from datetime import datetime
from typing import Iterator

import httpx

from sprintbaton.adaptors.base import (
    BoardProvisioningError,
    ExternalColumn,
    ExternalComment,
    ExternalTask,
    TaskAdapter,
)

log = logging.getLogger(__name__)

BASE_URL = "https://api.todoist.com/api/v1"
PAGE_LIMIT = 200  # v1 maximum page size

# Routing labels (todoist-label-routing spec §2.2). These strings live *only*
# here — no other file references them, branches on the provider, or calls the
# generic add_label primitive with them. The Todoist assignee field is no
# longer touched by SprintBaton at all; it means whatever a human wants.
LABEL_AGENT = "sprintbaton-agent"    # queued for the agent
LABEL_HUMAN = "sprintbaton-human"    # waiting on a human
# Todoist's fixed colour-name palette (user asked for salmon / green).
LABEL_COLORS = {LABEL_AGENT: "salmon", LABEL_HUMAN: "green"}


def _posted_at_millis(posted_at: str | None) -> int:
    """Todoist `posted_at` (ISO 8601, e.g. "2016-09-22T07:00:00.000000Z")
    -> epoch millis; 0 when absent or unparseable."""
    if not posted_at:
        return 0
    try:
        return int(datetime.fromisoformat(posted_at).timestamp() * 1000)
    except ValueError:
        log.warning("unparseable comment timestamp", extra={"posted_at": posted_at})
        return 0


class TodoistTaskAdapter(TaskAdapter):
    def __init__(self, api_token: str):
        # SPRINTBATON_AGENT_USER_ID dropped out as a routing input entirely
        # (todoist-label-routing spec §2.2): routing is by label now, and the
        # assignee field is left alone.
        self._client = httpx.Client(
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {api_token}"},
            timeout=30,
        )

    def _paginated(self, path: str, params: dict) -> Iterator[dict]:
        """Follow v1's cursor pagination until next_cursor runs out."""
        cursor: str | None = None
        while True:
            page_params = {**params, "limit": PAGE_LIMIT}
            if cursor:
                page_params["cursor"] = cursor
            resp = self._client.get(path, params=page_params)
            resp.raise_for_status()
            data = resp.json()
            yield from data.get("results", [])
            cursor = data.get("next_cursor")
            if not cursor:
                break

    def list_agent_tasks(self, board_id: str) -> list[ExternalTask]:
        # "Queued for the agent" = carries the sprintbaton-agent label. A task
        # a human has taken back (sprintbaton-human, no agent label) drops out
        # of intake; re-applying the agent label by hand re-queues it — the
        # native gesture that replaces reassigning to a bot account (§2.2).
        tasks = []
        for raw in self._paginated("/tasks", {"project_id": board_id}):
            if LABEL_AGENT not in (raw.get("labels") or []):
                continue
            tasks.append(self._to_external(raw))
        return tasks

    def get_task(self, external_id: str) -> ExternalTask | None:
        resp = self._client.get(f"/tasks/{external_id}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return self._to_external(resp.json())

    def move_task(self, external_id: str, column_id: str) -> None:
        resp = self._client.post(f"/tasks/{external_id}/move",
                                 json={"section_id": column_id})
        resp.raise_for_status()

    def route_to_human(self, external_id: str) -> None:
        # Swap to exactly [<others>, sprintbaton-human] in one atomic update
        # (never separate add/remove calls) so the task is never observably in
        # both states mid-transition (§2.2).
        self._set_routing_label(external_id, LABEL_HUMAN)

    def route_to_agent(self, external_id: str) -> None:
        self._set_routing_label(external_id, LABEL_AGENT)

    def _set_routing_label(self, external_id: str, keep: str) -> None:
        """Atomically set the task's labels so that `keep` is the only routing
        label present, preserving every non-routing label the human added."""
        task = self.get_task(external_id)
        others = [lbl for lbl in (task.labels if task else [])
                  if lbl not in (LABEL_AGENT, LABEL_HUMAN)]
        resp = self._client.post(f"/tasks/{external_id}",
                                 json={"labels": [*others, keep]})
        resp.raise_for_status()

    def add_comment(self, external_id: str, body: str) -> str:
        resp = self._client.post("/comments", json={"task_id": external_id, "content": body})
        resp.raise_for_status()
        return resp.json()["id"]

    def list_comments(self, external_id: str) -> list[ExternalComment]:
        comments = [
            ExternalComment(
                externalId=raw["id"],
                taskExternalId=external_id,
                body=raw.get("content", ""),
                authorId=raw.get("posted_uid") or "",
                postedAtMillis=_posted_at_millis(raw.get("posted_at")),
            )
            for raw in self._paginated("/comments", {"task_id": external_id})
        ]
        # TaskAdapter contract: oldest first
        return sorted(comments, key=lambda c: c.postedAtMillis)

    def add_label(self, external_id: str, label: str) -> None:
        task = self.get_task(external_id)
        labels = list(task.labels) if task else []
        if label not in labels:
            labels.append(label)
        resp = self._client.post(f"/tasks/{external_id}", json={"labels": labels})
        resp.raise_for_status()

    def create_board_with_template(self, title: str,
                                   sections: list[str]) -> tuple[str, list[str]]:
        resp = self._client.post("/projects", json={"name": title})
        resp.raise_for_status()
        board_id = resp.json()["id"]
        section_ids = []
        for name in sections:
            try:
                resp = self._client.post(
                    "/sections", json={"name": name, "project_id": board_id})
                resp.raise_for_status()
            except httpx.HTTPError as e:
                # No rollback (spec §12.3): surface the orphaned board id so
                # the caller can name it in the error and the user can clean up.
                raise BoardProvisioningError(board_id, name) from e
            section_ids.append(resp.json()["id"])
        return board_id, section_ids

    def provision_routing(self) -> None:
        """Create the sprintbaton-agent/-human labels if absent (§2.3).
        Idempotent: list account labels first, create only what's missing —
        a second Project linked under the same account finds them present."""
        existing = {raw.get("name") for raw in self._paginated("/labels", {})}
        for name in (LABEL_AGENT, LABEL_HUMAN):
            if name in existing:
                continue
            resp = self._client.post(
                "/labels", json={"name": name, "color": LABEL_COLORS[name]})
            resp.raise_for_status()

    def list_columns(self, board_id: str) -> list[ExternalColumn]:
        return [
            ExternalColumn(id=raw["id"], name=raw.get("name", ""))
            for raw in self._paginated("/sections", {"project_id": board_id})
        ]

    def create_column(self, board_id: str, name: str) -> str:
        resp = self._client.post(
            "/sections", json={"name": name, "project_id": board_id})
        resp.raise_for_status()
        return resp.json()["id"]

    @staticmethod
    def _assignee(raw: dict) -> str | None:
        # v1 responses use the sync-flavoured `responsible_uid`; tolerate the
        # REST-flavoured `assignee_id` too (the official SDK aliases both).
        return raw.get("responsible_uid") or raw.get("assignee_id")

    @classmethod
    def _to_external(cls, raw: dict) -> ExternalTask:
        return ExternalTask(
            externalId=raw["id"],
            boardId=raw.get("project_id", ""),
            columnId=raw.get("section_id") or "",
            title=raw.get("content", ""),
            description=raw.get("description") or None,
            priority=raw.get("priority"),
            labels=raw.get("labels") or [],
            assigneeId=cls._assignee(raw),
        )

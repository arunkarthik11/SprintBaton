"""TaskAdapter — the pluggable todolist provider interface.

Providers differ in capability (comments vs child tasks for clarification);
the common interface below is what the orchestrator and polling job depend on.
All ids in this interface are provider-side (external) ids.
"""

from abc import ABC, abstractmethod

from pydantic import BaseModel


class ProvisioningNotSupported(Exception):
    """Raised by providers that cannot create a template board
    (repository-onboarding spec §12.3-§12.4) — converted to a validation
    failure at the API boundary, never a 500."""

    def __init__(self, provider_name: str):
        super().__init__(f"{provider_name} does not support board provisioning")


class BoardProvisioningError(Exception):
    """Template-board provisioning failed after the provider-side project was
    already created (spec §12.3). No automatic rollback — the board id is
    carried here so the error surfaced to the user names the orphaned project
    for manual cleanup."""

    def __init__(self, board_id: str, failed_section: str):
        self.board_id = board_id
        self.failed_section = failed_section
        super().__init__(
            f"board {board_id} was created but section {failed_section!r} "
            f"could not be added — delete the partially-built board by hand"
        )


class ExternalTask(BaseModel):
    """Provider-shape task, before adaptation into the internal Task entity."""

    externalId: str
    boardId: str = ""
    columnId: str = ""
    title: str = ""
    description: str | None = None
    priority: int | None = None
    labels: list[str] = []
    assigneeId: str | None = None
    attachments: list[str] = []


class ExternalComment(BaseModel):
    externalId: str
    taskExternalId: str
    authorId: str = ""
    body: str = ""
    postedAtMillis: int = 0


class ExternalColumn(BaseModel):
    """A board's existing column, as the provider exposes it (Todoist: a
    section). Used by the optional column-mapping flow (todoist-label-routing
    spec §3.4) to match unmapped ColumnConfig keys against what already exists."""

    id: str
    name: str = ""


class TaskAdapter(ABC):
    """Task CRUD against a todolist provider.

    Routing — *whose turn a task is on*, agent vs. human — is described here as
    behavior, never as mechanism (todoist-label-routing spec §2.1). Callers
    (polling job, orchestrator) speak only in "queued for the agent" / "waiting
    on a human"; how a provider represents that (Todoist: two labels; a future
    provider: assignment, a status field, a custom property) is entirely the
    adapter's private business and never leaks upward."""

    @abstractmethod
    def list_agent_tasks(self, board_id: str) -> list[ExternalTask]:
        """The tasks on the board currently queued for the agent (todoist-label-
        routing spec §2.2). How "queued for the agent" is represented is the
        adapter's own choice."""

    @abstractmethod
    def get_task(self, external_id: str) -> ExternalTask | None: ...

    @abstractmethod
    def move_task(self, external_id: str, column_id: str) -> None: ...

    @abstractmethod
    def route_to_human(self, external_id: str) -> None:
        """Mark the task as waiting on a human (todoist-label-routing spec §2.2).
        Must leave the task in an unambiguous state — never observably both
        "queued for agent" and "waiting on human" at once mid-transition (the
        adapter upholds this on its own terms; Todoist: a single atomic
        label-list update)."""

    @abstractmethod
    def route_to_agent(self, external_id: str) -> None:
        """Mark the task as queued for the agent again once a human has acted
        (todoist-label-routing spec §2.2), with the same atomicity guarantee as
        route_to_human."""

    @abstractmethod
    def add_comment(self, external_id: str, body: str) -> str:
        """Post a comment; returns the provider comment id."""

    @abstractmethod
    def list_comments(self, external_id: str) -> list[ExternalComment]:
        """Every comment on the task, oldest first, with postedAtMillis
        populated. authorId is populated when the provider exposes one; the
        clarification lifecycle falls back to recognizing the agent's own
        comments by their fixed headers when it doesn't (conversation-
        lifecycle spec §13)."""

    @abstractmethod
    def add_label(self, external_id: str, label: str) -> None: ...

    # ---- optional provisioning capabilities (default: not supported) --------
    # Same pattern as create_board_with_template: a provider overrides only what
    # its own board-structure/routing mechanism actually needs; the generic
    # linking flow calls these without knowing what happens underneath.

    def provision_routing(self) -> None:
        """One-time setup a provider's routing mechanism needs, called once when
        a Project's provider connection is first established (todoist-label-
        routing spec §2.3). Idempotent and internal — the caller never reasons
        about what, if anything, is created. Defaults to a no-op for providers
        with nothing to provision (e.g. assignment-based routing)."""

    def list_columns(self, board_id: str) -> list[ExternalColumn]:
        """The board's existing columns (todoist-label-routing spec §3.4).
        Optional capability behind the auto column-mapping flow."""
        raise ProvisioningNotSupported(type(self).__name__)

    def create_column(self, board_id: str, name: str) -> str:
        """Create one column on the board; returns its id (todoist-label-routing
        spec §3.4). Optional capability behind the auto column-mapping flow."""
        raise ProvisioningNotSupported(type(self).__name__)

    def create_board_with_template(self, title: str,
                                   sections: list[str]) -> tuple[str, list[str]]:
        """Create a new board plus one section per name in `sections` (same
        order); returns (board_id, [section_id, ...]). Optional capability —
        only Todoist implements it today (repository-onboarding spec §12.3)."""
        raise ProvisioningNotSupported(type(self).__name__)

"""Project onboarding — the one shared create/upsert path behind both the
/projects API and `sprintbaton project create -f <manifest>` (multi-repo-project
spec §11). A Project owns the todolist board (columns/cadence/provider) and a set
of member Repository rows (git-only). A single-repo project is just a one-element
`repositories` list — the uniform path.

There is deliberately no second implementation of "what a valid Project looks
like": the API request body *is* ProjectSpec, and the manifest parser normalizes
into the same model."""

from __future__ import annotations

import re
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import TYPE_CHECKING

import yaml
from pydantic import BaseModel, field_validator

from sprintbaton.adaptors import create_adapter
from sprintbaton.entities.enums import CredentialProvider
from sprintbaton.entities.project import TEMPLATE_SECTIONS, ColumnConfig, Project
from sprintbaton.entities.repository import Repository

from sprintbaton.entities.task import Task
from sprintbaton.metadata.initialization import (
    MAX_GUIDANCE_CHARS,
    ensure_initialization_task,
)
from sprintbaton.metadata.revisions import held_init_lock

if TYPE_CHECKING:
    from sprintbaton.config.settings import Settings
    from sprintbaton.storage.base import DistributedLock, EntityDAO
    from sprintbaton.users.credentials import CredentialService

GUIDANCE_CHANGED_HINT = "metadataGuidance changed — run `sprintbaton init` to apply"

MANIFEST_API_VERSION = "sprintbaton/v1"
MANIFEST_KIND = "Project"

# The always-required column keys — a live Project.columns must map every one
# (`blocked` stays optional). Derived from ColumnConfig so the two never drift.
REQUIRED_COLUMN_FIELDS = [f for f in ColumnConfig.model_fields if f != "blocked"]


class OnboardingError(ValueError):
    """A validation failure in a create/upsert request — 422 at the API
    boundary, a plain error message from the CLI."""


class PartialColumnConfig(BaseModel):
    """A caller-supplied column mapping where any subset may be omitted
    (todoist-label-routing spec §3). Every unmapped key is resolved at link time
    by exact-name match against the board's existing columns, else by creating a
    new one. A mapping covering every REQUIRED_COLUMN_FIELD is equivalent to
    today's fully-specified ColumnConfig — nothing left to match or create."""

    model_config = {"extra": "forbid"}

    icebox: str | None = None
    task_finalization: str | None = None
    passing_criteria: str | None = None
    task_finalized: str | None = None
    plan_finalization: str | None = None
    plan_finalized: str | None = None
    in_progress: str | None = None
    code_review: str | None = None
    in_review: str | None = None
    qa: str | None = None
    shipped: str | None = None
    blocked: str | None = None

    def required_complete(self) -> bool:
        return all(getattr(self, f) for f in REQUIRED_COLUMN_FIELDS)

    def to_full(self) -> ColumnConfig:
        return ColumnConfig(**self.model_dump())


class PlannedColumn(BaseModel):
    """One line of a column-provisioning plan shown to the user before anything
    is created on their live board (todoist-label-routing spec §3.1)."""

    columnKey: str
    sectionName: str
    action: str            # "explicit" | "matched" | "create"
    sectionId: str | None = None  # the existing id for explicit/matched


class ColumnProvisioningRequired(Exception):
    """Raised by build_project when unmapped columns would have to be *created*
    on a live board and the caller hasn't confirmed (todoist-label-routing spec
    §3.1 / §5 confirmation surface). Carries the concrete plan so the CLI can
    prompt interactively and the API can return it for a re-submit with
    confirmColumnProvisioning=true. No board mutation and no Project row has
    been written when this fires."""

    def __init__(self, plan: list[PlannedColumn]):
        self.plan = plan
        creating = [p.sectionName for p in plan if p.action == "create"]
        super().__init__(
            "column provisioning needs confirmation — would create: "
            + ", ".join(creating))


class GitIdentitySpec(BaseModel):
    """Optional git identity block, valid at both the project and the member-repo
    level (storage-layout-and-git-identity spec §5.3). Additive: both enclosing
    models declare `extra: forbid`, so no manifest written before this existed
    can already carry a `git:` block, and adding it cannot change how any
    existing manifest parses."""

    model_config = {"extra": "forbid"}

    authorName: str | None = None
    authorEmail: str | None = None


class MemberRepositorySpec(BaseModel):
    """A git-repo member of a project (multi-repo-project spec §4.2)."""

    model_config = {"extra": "forbid"}

    title: str
    role: str = ""
    remoteUrl: str = ""
    githubRepo: str = ""
    devBranch: str = "dev"
    stagingBranch: str = "staging"
    productionBranch: str = "main"
    githubCredentialId: str | None = None
    git: GitIdentitySpec | None = None   # per-repo commit identity override


class ProjectSpec(BaseModel):
    """The validated creation payload (multi-repo-project spec §11) — also the
    POST /projects request body. `columns` is required unless
    createTodolistProject is true, in which case boardId/columns must both be
    omitted and are filled in by the provisioning flow."""

    model_config = {"extra": "forbid"}

    title: str
    boardId: str = ""
    todolistProvider: str = "todoist"
    releaseCadenceDays: int = 7
    active: bool = True
    # A partial mapping is now allowed (todoist-label-routing spec §3): unmapped
    # keys are matched/created at link time. None = map every key automatically.
    columns: PartialColumnConfig | None = None
    todolistCredentialId: str | None = None
    createTodolistProject: bool = False  # request-only, never persisted
    # Non-interactive confirmation of the column-provisioning plan (§5): the API
    # / scripted CLI re-submits with this set to true after seeing the dry-run
    # plan. Request-only, never persisted.
    confirmColumnProvisioning: bool = False
    git: GitIdentitySpec | None = None   # project-wide commit identity default
    # Metadata init pass (project-initialization-task spec §10.1): the opt-out
    # (false = never auto-generate, never gate board tasks) and optional
    # operator guidance passed to both init agents.
    generateMetadata: bool = True
    metadataGuidance: str | None = None
    repositories: list[MemberRepositorySpec] = []

    @field_validator("metadataGuidance")
    @classmethod
    def _guidance_bounded(cls, value: str | None) -> str | None:
        if value is not None and len(value) > MAX_GUIDANCE_CHARS:
            raise ValueError(
                f"metadataGuidance must be at most {MAX_GUIDANCE_CHARS} characters "
                f"(got {len(value)})")
        return (value.strip() or None) if value is not None else None


def _snake(name: str) -> str:
    """Manifest camelCase column keys -> ColumnConfig field names."""
    return re.sub(r"(?<!^)([A-Z])", r"_\1", name).lower()


def parse_project_manifest(text: str) -> ProjectSpec:
    """Parse the k8s-style manifest (multi-repo-project spec §11.1) into a
    ProjectSpec. apiVersion/kind mismatches fail loudly."""
    raw = yaml.safe_load(text)
    if not isinstance(raw, dict):
        raise OnboardingError("manifest must be a YAML mapping")
    if raw.get("apiVersion") != MANIFEST_API_VERSION:
        raise OnboardingError(
            f"unsupported apiVersion: {raw.get('apiVersion')!r} "
            f"(expected {MANIFEST_API_VERSION!r})")
    if raw.get("kind") != MANIFEST_KIND:
        raise OnboardingError(
            f"unsupported kind: {raw.get('kind')!r} (expected {MANIFEST_KIND!r})")
    name = (raw.get("metadata") or {}).get("name")
    if not name:
        raise OnboardingError("metadata.name is required")

    spec = dict(raw.get("spec") or {})
    credentials = spec.pop("credentials", None) or {}
    columns = spec.pop("columns", None)
    repositories = spec.pop("repositories", None) or []

    payload: dict = {"title": name, **spec}
    if "todolist" in credentials:
        payload["todolistCredentialId"] = credentials["todolist"]
    if columns is not None:
        payload["columns"] = {_snake(k): v for k, v in columns.items()}

    member_specs = []
    for entry in repositories:
        entry = dict(entry or {})
        branches = entry.pop("branches", None) or {}
        member_creds = entry.pop("credentials", None) or {}
        member_name = entry.pop("name", None) or entry.pop("title", None)
        if not member_name:
            raise OnboardingError("each repository requires a name")
        member = {"title": member_name, **entry}
        for manifest_key, field in (("dev", "devBranch"), ("staging", "stagingBranch"),
                                    ("production", "productionBranch")):
            if manifest_key in branches:
                member[field] = branches[manifest_key]
        if "github" in member_creds:
            member["githubCredentialId"] = member_creds["github"]
        member_specs.append(member)
    payload["repositories"] = member_specs

    try:
        return ProjectSpec.model_validate(payload)
    except ValueError as e:
        raise OnboardingError(f"invalid manifest spec: {e}") from e


def _check_credential_refs(spec: ProjectSpec, user_id: str,
                           credentials: "CredentialService") -> None:
    """A project/repo cannot reference someone else's (or a deleted) credential."""
    refs = [spec.todolistCredentialId]
    refs += [r.githubCredentialId for r in spec.repositories]
    for credential_id in refs:
        if credential_id and credentials.owned(user_id, credential_id) is None:
            raise OnboardingError(f"no such credential: {credential_id}")


def _adapter_for(spec: ProjectSpec, user_id: str,
                 credentials: "CredentialService", settings: "Settings"):
    """The todolist adapter for a spec's provider, with the token resolved
    through the standard three-tier credential chain."""
    token = credentials.resolve(
        user_id, CredentialProvider(spec.todolistProvider),
        spec.todolistCredentialId, fallback=settings.todolist_api_token)
    return create_adapter(spec.todolistProvider, token)


def _provision_board(spec: ProjectSpec, user_id: str,
                     credentials: "CredentialService",
                     settings: "Settings") -> tuple[str, ColumnConfig]:
    """Create the template Todoist board + sections and zip the section ids onto
    ColumnConfig (repository-onboarding spec §12.2-§12.5)."""
    adapter = _adapter_for(spec, user_id, credentials, settings)
    board_id, section_ids = adapter.create_board_with_template(
        spec.title, [display for _, display in TEMPLATE_SECTIONS])
    columns = ColumnConfig(**{
        field: section_id
        for (field, _), section_id in zip(TEMPLATE_SECTIONS, section_ids)
    })
    return board_id, columns


def plan_columns(partial: PartialColumnConfig,
                 existing: list) -> list[PlannedColumn]:
    """Match every column key against the board's existing columns, deciding
    per key whether it is already mapped (explicit), matched by exact
    case-insensitive name, or must be created (todoist-label-routing spec §3.1).
    Fuzzy/synonym matching is deliberately out of scope for v1 (§5)."""
    by_name = {c.name.strip().casefold(): c.id
               for c in existing if c.name.strip()}
    plan: list[PlannedColumn] = []
    for field, display in TEMPLATE_SECTIONS:
        explicit = getattr(partial, field)
        if explicit:
            plan.append(PlannedColumn(columnKey=field, sectionName=display,
                                      action="explicit", sectionId=explicit))
            continue
        match = by_name.get(display.strip().casefold())
        if match:
            plan.append(PlannedColumn(columnKey=field, sectionName=display,
                                      action="matched", sectionId=match))
        else:
            plan.append(PlannedColumn(columnKey=field, sectionName=display,
                                      action="create"))
    return plan


def _map_or_create_columns(spec: ProjectSpec, user_id: str,
                           credentials: "CredentialService", settings: "Settings",
                           partial: PartialColumnConfig) -> ColumnConfig:
    """Resolve a partial column mapping into a full ColumnConfig against an
    existing Todoist board: explicit keys as given, unmapped keys matched by
    name, the rest created — but only after confirmation (§3.1). Idempotent:
    always list-then-create-if-missing, so a re-run maps prior creations by name
    instead of duplicating them."""
    if not spec.boardId:
        raise OnboardingError(
            "boardId is required to map or create columns on an existing board")
    adapter = _adapter_for(spec, user_id, credentials, settings)
    plan = plan_columns(partial, adapter.list_columns(spec.boardId))
    if any(p.action == "create" for p in plan) and not spec.confirmColumnProvisioning:
        # Never create sections silently as a side effect of linking (§3.1) —
        # bounce the concrete plan back to the caller to confirm.
        raise ColumnProvisioningRequired(plan)
    resolved: dict[str, str] = {}
    for p in plan:
        if p.action == "create":
            resolved[p.columnKey] = adapter.create_column(spec.boardId, p.sectionName)
        else:
            resolved[p.columnKey] = p.sectionId  # type: ignore[assignment]
    return ColumnConfig(**resolved)


def provision_provider_routing(project: Project, *, credentials: "CredentialService",
                               settings: "Settings") -> None:
    """Best-effort one-time routing setup when a Project's provider connection is
    established (todoist-label-routing spec §2.3) — for Todoist, creating the
    sprintbaton-agent/-human labels. Idempotent and internal to the adapter; a
    hiccup here must never fail onboarding, so failures are logged, not raised."""
    import logging
    try:
        token = credentials.resolve(
            project.userId, CredentialProvider(project.todolistProvider),
            project.todolistCredentialId, fallback=settings.todolist_api_token)
        create_adapter(project.todolistProvider, token).provision_routing()
    except Exception:
        logging.getLogger(__name__).warning(
            "routing provisioning failed", extra={"project_id": project.id})


def build_project(spec: ProjectSpec, *, user_id: str,
                  credentials: "CredentialService", settings: "Settings",
                  existing: Project | None = None) -> Project:
    """Validate a ProjectSpec and produce the (unsaved) Project row — provisioning
    a template board first when asked. Member repositories are applied separately
    (see apply_project) so this stays the board-level unit."""
    _check_credential_refs(spec, user_id, credentials)

    if spec.createTodolistProject:
        if spec.boardId or spec.columns is not None:
            raise OnboardingError(
                "createTodolistProject requires boardId and columns to be omitted")
        if spec.todolistProvider != "todoist":
            raise OnboardingError(
                "createTodolistProject is not supported for provider "
                f"{spec.todolistProvider}")
        if existing is not None and existing.boardId:
            board_id, columns = existing.boardId, existing.columns
        else:
            board_id, columns = _provision_board(spec, user_id, credentials, settings)
    else:
        partial = spec.columns or PartialColumnConfig()
        if partial.required_complete():
            # Today's fully-specified state (§5): nothing to match or create.
            board_id, columns = spec.boardId, partial.to_full()
        elif spec.todolistProvider == "todoist":
            # Optional column mapping / auto-provisioning (§3): match unmapped
            # keys against the live board, create the rest after confirmation.
            columns = _map_or_create_columns(spec, user_id, credentials,
                                             settings, partial)
            board_id = spec.boardId
        else:
            raise OnboardingError(
                "columns is required unless createTodolistProject is true")

    fields = dict(
        userId=user_id,
        title=spec.title,
        boardId=board_id,
        todolistProvider=spec.todolistProvider,
        releaseCadenceDays=spec.releaseCadenceDays,
        active=spec.active,
        columns=columns,
        todolistCredentialId=spec.todolistCredentialId,
        gitAuthorName=spec.git.authorName if spec.git else None,
        gitAuthorEmail=spec.git.authorEmail if spec.git else None,
        generateMetadata=spec.generateMetadata,
        metadataGuidance=spec.metadataGuidance,
    )
    # Only spec fields are assigned — the metadata pointer, initializedAt, and
    # the operational gate override survive a re-apply untouched.
    if existing is None:
        return Project(createdBy=user_id, modifiedBy=user_id, **fields)
    for field, value in fields.items():
        setattr(existing, field, value)
    existing.touch(modified_by=user_id)
    return existing


@dataclass
class ProjectApplyResult:
    project: Project
    created: bool
    # The automatic metadata init run this apply queued, if any
    # (project-initialization-task spec §5.1).
    initialization_task: Task | None = None
    # User-facing notes, e.g. the guidance-changed hint (spec §10.1).
    hints: list[str] = dataclass_field(default_factory=list)


def apply_project(spec: ProjectSpec, *, user_id: str,
                  project_repo: "EntityDAO[Project]",
                  repo_repo: "EntityDAO[Repository]",
                  credentials: "CredentialService",
                  settings: "Settings",
                  task_repo: "EntityDAO[Task]",
                  lock: "DistributedLock") -> ProjectApplyResult:
    """`kubectl apply`-style upsert of a project and its member repositories,
    keyed on title within the tenant (multi-repo-project spec §11), ending with
    the automatic metadata init run when one is needed.

    Holds the `project-init:<projectId>` lock across the whole upsert
    (project-initialization-task spec §5.1): Project and Repository rows are
    saved as whole documents, and apply and the init pass's pointer swap are
    their only writers — one lock means neither overwrites the other's fields
    with a stale copy. A brand-new project has no id to lock until it is
    built, and nothing else can hold its lock yet."""
    existing = project_repo.find_one({"title": spec.title, "userId": user_id})
    if existing is None:
        project = build_project(spec, user_id=user_id, credentials=credentials,
                                settings=settings, existing=None)
        with held_init_lock(lock, project.id):
            return _apply_locked(spec, project, None, user_id=user_id,
                                 project_repo=project_repo, repo_repo=repo_repo,
                                 task_repo=task_repo)
    with held_init_lock(lock, existing.id):
        # Re-read under the lock: a swap may have landed since the lookup.
        existing = project_repo.get(existing.id) or existing
        previous_guidance = existing.metadataGuidance
        project = build_project(spec, user_id=user_id, credentials=credentials,
                                settings=settings, existing=existing)
        return _apply_locked(spec, project, previous_guidance, user_id=user_id,
                             project_repo=project_repo, repo_repo=repo_repo,
                             task_repo=task_repo, created=False)


def _apply_locked(spec: ProjectSpec, project: Project,
                  previous_guidance: str | None, *, user_id: str,
                  project_repo: "EntityDAO[Project]",
                  repo_repo: "EntityDAO[Repository]",
                  task_repo: "EntityDAO[Task]",
                  created: bool = True) -> ProjectApplyResult:
    project_repo.save(project)

    # Upsert member repositories by title within the project. The metadata
    # pointer is not a spec field, so it survives a re-apply.
    current = {r.title: r for r in repo_repo.find(
        {"projectId": project.id, "deleted": False})}
    for member in spec.repositories:
        repo = current.get(member.title)
        fields = dict(
            userId=user_id,
            projectId=project.id,
            role=member.role,
            remoteUrl=member.remoteUrl,
            githubRepo=member.githubRepo,
            devBranch=member.devBranch,
            stagingBranch=member.stagingBranch,
            productionBranch=member.productionBranch,
            githubCredentialId=member.githubCredentialId,
            gitAuthorName=member.git.authorName if member.git else None,
            gitAuthorEmail=member.git.authorEmail if member.git else None,
        )
        if repo is None:
            repo = Repository(createdBy=user_id, modifiedBy=user_id,
                              title=member.title, **fields)
        else:
            for name, value in fields.items():
                setattr(repo, name, value)
            repo.touch(modified_by=user_id)
        repo_repo.save(repo)

    all_members = list(repo_repo.find({"projectId": project.id, "deleted": False}))
    initialization_task = ensure_initialization_task(
        project, all_members, task_repo=task_repo)
    hints: list[str] = []
    if (not created and project.metadataInitializedAt is not None
            and project.metadataGuidance != previous_guidance):
        # A text edit never queues a full Opus pass per repo (spec §10.1).
        hints.append(GUIDANCE_CHANGED_HINT)
    return ProjectApplyResult(project=project, created=created,
                              initialization_task=initialization_task, hints=hints)

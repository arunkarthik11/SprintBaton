"""Project CRUD (user-multitenancy spec §9; multi-repo-project spec §11) —
onboards a todolist board plus its member git repositories. The create body is
the shared ProjectSpec (the same model the CLI manifest parser produces — one
implementation of "what a valid Project looks like"), including the
createTodolistProject convenience flag that provisions a template board and fills
in boardId/columns.

The metadata routes (project-initialization-task spec §10.3) only persist or
read init-run Task rows: `sprintbaton serve`'s polling lane discovers and runs
them, so this pod never calls a model, clones a repo, or enqueues."""

import threading

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from sprintbaton.adaptors.base import BoardProvisioningError, ProvisioningNotSupported
from sprintbaton.api.app import ApiState
from sprintbaton.api.auth import api_state, require_user
from sprintbaton.entities.enums import InitializationTrigger, MetadataScope
from sprintbaton.entities.project import ColumnConfig, Project
from sprintbaton.entities.user import User
from sprintbaton.metadata.initialization import (
    InitializationInProgress,
    MetadataStatusResponse,
    initialization_status_view,
    latest_initialization_task,
    request_initialization,
    set_metadata_gate_override,
)
from sprintbaton.metadata.revisions import MetadataLockTimeout
from sprintbaton.onboarding import (
    ColumnProvisioningRequired,
    OnboardingError,
    ProjectSpec,
    apply_project,
    provision_provider_routing,
)

router = APIRouter(prefix="/projects", tags=["projects"])

CreateProjectRequest = ProjectSpec


class ProjectResponse(BaseModel):
    id: str
    title: str
    boardId: str
    todolistProvider: str
    releaseCadenceDays: int
    active: bool
    columns: ColumnConfig
    todolistCredentialId: str | None
    repositoryIds: list[str]
    generateMetadata: bool = True
    metadataGuidance: str | None = None
    # Set on create/apply only (project-initialization-task spec §10.3): the
    # automatic metadata init run this request queued, and user-facing hints
    # such as "metadataGuidance changed — run `sprintbaton init` to apply".
    initializationTaskId: str | None = None
    hints: list[str] = []


def _view(state: ApiState, project: Project) -> ProjectResponse:
    members = state.repositories.find({"projectId": project.id, "deleted": False})
    return ProjectResponse(
        id=project.id, title=project.title, boardId=project.boardId,
        todolistProvider=project.todolistProvider,
        releaseCadenceDays=project.releaseCadenceDays, active=project.active,
        columns=project.columns,
        todolistCredentialId=project.todolistCredentialId,
        repositoryIds=[r.id for r in members],
        generateMetadata=project.generateMetadata,
        metadataGuidance=project.metadataGuidance,
    )


@router.get("", response_model=list[ProjectResponse])
def list_projects(user: User = Depends(require_user),
                  state: ApiState = Depends(api_state)) -> list[ProjectResponse]:
    return [_view(state, p) for p in state.projects.find({"userId": user.id})]


@router.post("", status_code=201, response_model=ProjectResponse)
def create_project(body: CreateProjectRequest,
                   user: User = Depends(require_user),
                   state: ApiState = Depends(api_state)) -> ProjectResponse:
    try:
        result = apply_project(
            body, user_id=user.id, project_repo=state.projects,
            repo_repo=state.repositories, credentials=state.credentials,
            settings=state.settings, task_repo=state.tasks, lock=state.lock)
    except ColumnProvisioningRequired as e:
        # Non-interactive confirmation surface (todoist-label-routing spec §5):
        # nothing was created; return the plan and let the client re-POST with
        # confirmColumnProvisioning=true. 409 = "your request is fine but needs
        # a follow-up decision" — never happens without a partial column map.
        raise HTTPException(status_code=409, detail={
            "message": "column provisioning needs confirmation; re-submit with "
                       "confirmColumnProvisioning=true",
            "columnPlan": [p.model_dump() for p in e.plan],
        })
    except (OnboardingError, ProvisioningNotSupported, ValueError) as e:
        raise HTTPException(status_code=422, detail=str(e))
    except BoardProvisioningError as e:
        # Partial provider-side state, no rollback: the message names the
        # orphaned board id so the user can delete it by hand.
        raise HTTPException(status_code=422, detail=str(e))
    except MetadataLockTimeout as e:
        raise HTTPException(status_code=503, detail=str(e))
    project = result.project
    # Establish the provider's routing mechanism once at link time (§2.3),
    # off the request thread so provider-side latency never blocks the response.
    threading.Thread(
        target=provision_provider_routing, args=(project,),
        kwargs={"credentials": state.credentials, "settings": state.settings},
        name=f"routing-provision-{project.id}", daemon=True,
    ).start()
    view = _view(state, project)
    view.initializationTaskId = (result.initialization_task.id
                                 if result.initialization_task else None)
    view.hints = result.hints
    return view


@router.delete("/{project_id}", status_code=204)
def delete_project(project_id: str, user: User = Depends(require_user),
                   state: ApiState = Depends(api_state)) -> None:
    project = state.projects.get(project_id)
    if project is None or project.userId != user.id:
        raise HTTPException(status_code=404, detail="no such project")
    state.projects.soft_delete(project.id)


def _owned_project(state: ApiState, user: User, project_id: str) -> Project:
    project = state.projects.get(project_id)
    if project is None or project.userId != user.id:
        raise HTTPException(status_code=404, detail="no such project")
    return project


def _metadata_view(state: ApiState, project: Project) -> MetadataStatusResponse:
    return initialization_status_view(
        project, latest_initialization_task(state.tasks, project),
        stale_after_seconds=state.settings.sprintbaton_reconcile_stale_after_seconds)


class GenerateMetadataRequest(BaseModel):
    scope: MetadataScope = MetadataScope.All


class MetadataGateRequest(BaseModel):
    open: bool


@router.post("/{project_id}/metadata", status_code=202,
             response_model=MetadataStatusResponse)
def generate_metadata(project_id: str,
                      body: GenerateMetadataRequest | None = Body(default=None),
                      user: User = Depends(require_user),
                      state: ApiState = Depends(api_state)):
    """CLI/API parity for `sprintbaton init` (spec §10.3): persist an explicit
    init run for the polling lane to discover. 409 (with the unfinished run's
    taskId) when one is already in progress; poll GET for the outcome."""
    project = _owned_project(state, user, project_id)
    members = state.repositories.find({"projectId": project.id, "deleted": False})
    if not any(m.remoteUrl for m in members):
        raise HTTPException(status_code=422,
                            detail="project has no repository with a remoteUrl to clone")
    try:
        request_initialization(
            project, task_repo=state.tasks, lock=state.lock,
            scope=(body.scope if body is not None else MetadataScope.All),
            trigger=InitializationTrigger.Api)
    except InitializationInProgress as e:
        return JSONResponse(status_code=409, content={
            "detail": "a metadata initialization run is already in progress",
            "taskId": e.task.id})
    except MetadataLockTimeout as e:
        raise HTTPException(status_code=503, detail=str(e))
    return _metadata_view(state, project)


@router.get("/{project_id}/metadata", response_model=MetadataStatusResponse)
def metadata_status(project_id: str, user: User = Depends(require_user),
                    state: ApiState = Depends(api_state)) -> MetadataStatusResponse:
    """The latest init run (highest createdTime), or status "none"."""
    return _metadata_view(state, _owned_project(state, user, project_id))


@router.put("/{project_id}/metadata/gate", response_model=MetadataStatusResponse)
def metadata_gate(project_id: str, body: MetadataGateRequest,
                  user: User = Depends(require_user),
                  state: ApiState = Depends(api_state)) -> MetadataStatusResponse:
    """Open (or re-enforce) the TaskPending gate without metadata (spec §5.4)."""
    project = _owned_project(state, user, project_id)
    try:
        project = set_metadata_gate_override(
            project, body.open, project_repo=state.projects, lock=state.lock,
            user_id=user.id)
    except MetadataLockTimeout as e:
        raise HTTPException(status_code=503, detail=str(e))
    return _metadata_view(state, project)

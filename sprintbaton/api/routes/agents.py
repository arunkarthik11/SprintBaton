"""AgentDefinition CRUD (user-multitenancy spec §9) — the API twin of
`sprintbaton agents create|list|delete`, scoped to the caller."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from sprintbaton.api.app import ApiState, harness_class
from sprintbaton.api.auth import api_state, require_user
from sprintbaton.entities.agent_definition import AgentDefinition
from sprintbaton.harness.base import unusable_harness_reason
from sprintbaton.entities.user import User
from sprintbaton.prompts.registry import PromptRegistry

router = APIRouter(prefix="/agents", tags=["agents"])


class CreateAgentRequest(BaseModel):
    """Same fields as `sprintbaton agents create`."""

    name: str
    harness: str
    model: str
    provider: str = "anthropic"
    # A system preamble prepended before the action's own template, never a
    # replacement for it (agent-system-prompt spec §6).
    systemPrompt: str | None = None
    description: str | None = None


class AdvisoryResponse(BaseModel):
    """A known upstream defect that changes what this harness guarantees
    (subprocess-cli-write-parity spec §6.3)."""

    harness: str
    severity: str
    summary: str
    detail: str
    referenceUrl: str
    verifiedOn: str


class AgentResponse(BaseModel):
    id: str
    name: str
    harnessName: str
    modelProvider: str
    modelId: str
    systemPromptName: str | None
    description: str | None
    # Non-empty when the chosen harness carries advisories. Deliberately part of
    # a 201, never a 4xx: this is information, not a validation failure.
    warnings: list[AdvisoryResponse] = []


def _view(definition: AgentDefinition,
          warnings: list[AdvisoryResponse] | None = None) -> AgentResponse:
    return AgentResponse(
        id=definition.id, name=definition.name or "",
        harnessName=definition.harnessName,
        modelProvider=definition.modelProvider, modelId=definition.modelId,
        systemPromptName=definition.systemPromptName,
        description=definition.description,
        warnings=warnings or [],
    )


def _warnings_for(harness: str, base_url: str = "") -> list[AdvisoryResponse]:
    from sprintbaton.harness.advisories import advisories_for, gateway_advisories

    return [AdvisoryResponse(**a.as_dict())
            for a in (*advisories_for(harness), *gateway_advisories(harness, base_url))]


def _base_url(state: ApiState, user_id: str, provider: str) -> str:
    if state.providers is None:
        return ""
    row = state.providers.find_one({"name": provider, "userId": user_id})
    return (row.baseUrl or "") if row is not None else ""


@router.get("", response_model=list[AgentResponse])
def list_agents(user: User = Depends(require_user),
                state: ApiState = Depends(api_state)) -> list[AgentResponse]:
    return [_view(d) for d in state.agent_definitions.find({"userId": user.id})]


@router.post("", status_code=201, response_model=AgentResponse)
def create_agent(body: CreateAgentRequest, user: User = Depends(require_user),
                 state: ApiState = Depends(api_state)) -> AgentResponse:
    if body.harness not in state.harness_names:
        raise HTTPException(
            status_code=422,
            detail=f"unknown harness: {body.harness} (registered: {state.harness_names})")
    # Fail at write time, not once per task at resolution time (auth-mode-
    # resolution spec §9 q2): a harness that cannot be sandboxed where runs
    # must be isolated, or a login-only CLI off a tool-mode install, is a
    # definition that could never execute. The rule lives in
    # unusable_harness_reason, shared with `sprintbaton agents create`
    # (cli-subscription-auth-parity spec §4.5); nothing about the *account*
    # enters it since hosted-sandbox-isolation spec §9.
    refusal = unusable_harness_reason(
        harness_class(body.harness),
        isolated=state.settings.backend_for("sandbox") == "remote",
        local_login_sessions=state.settings.local_login_sessions)
    if refusal:
        raise HTTPException(status_code=422, detail=refusal)
    if state.agent_definitions.find_one(
            {"name": body.name, "userId": user.id}) is not None:
        raise HTTPException(
            status_code=409,
            detail=f"an AgentDefinition named {body.name!r} already exists")
    # Fail at write time, not once per task inside the dispatch path
    # (agent-system-prompt spec §5).
    if body.systemPrompt:
        prompts = PromptRegistry()
        if not prompts.exists(body.systemPrompt):
            raise HTTPException(
                status_code=422,
                detail=(f"unknown system prompt template: {body.systemPrompt!r} "
                        f"— expected {prompts.path_for(body.systemPrompt)}"))
    definition = AgentDefinition(
        userId=user.id, createdBy=user.id, modifiedBy=user.id,
        name=body.name, harnessName=body.harness, modelProvider=body.provider,
        modelId=body.model, systemPromptName=body.systemPrompt,
        description=body.description,
    )
    state.agent_definitions.save(definition)
    return _view(definition, _warnings_for(
        body.harness, _base_url(state, user.id, body.provider)))


@router.delete("/{definition_id}", status_code=204)
def delete_agent(definition_id: str, user: User = Depends(require_user),
                 state: ApiState = Depends(api_state)) -> None:
    definition = state.agent_definitions.get(definition_id)
    # 404, not 403, on someone else's id — don't confirm existence to a
    # non-owner (spec §9)
    if definition is None or definition.userId != user.id:
        raise HTTPException(status_code=404, detail="no such agent definition")
    state.agent_definitions.soft_delete(definition_id)

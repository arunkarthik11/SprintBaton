"""Credential CRUD (user-multitenancy spec §9). POST is the one place a
plaintext token ever crosses the wire — never echoed back; every response
carries only the masked form. "Rotate" is delete + re-create (POSTing a new
default replaces the old default), never update-in-place. Non-default
credentials coexist as repo-level overrides addressable by id
(repository-onboarding spec §4.1)."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from sprintbaton.api.app import ApiState
from sprintbaton.api.auth import api_state, require_user
from sprintbaton.entities.enums import CredentialProvider
from sprintbaton.entities.user import User

router = APIRouter(prefix="/credentials", tags=["credentials"])


class CreateCredentialRequest(BaseModel):
    provider: CredentialProvider
    token: str
    # None: the first credential of a provider becomes the user's default
    # (repository-onboarding spec §4.1)
    isDefault: bool | None = None


class CredentialResponse(BaseModel):
    """provider + maskedKey only — never encryptedKey (spec §9)."""

    id: str
    provider: CredentialProvider
    maskedKey: str
    isDefault: bool
    createdTime: int


@router.get("", response_model=list[CredentialResponse])
def list_credentials(user: User = Depends(require_user),
                     state: ApiState = Depends(api_state)) -> list[CredentialResponse]:
    return [CredentialResponse(**state.credentials.public_view(c))
            for c in state.credentials.list_for(user.id)]


@router.post("", status_code=201, response_model=CredentialResponse)
def store_credential(body: CreateCredentialRequest,
                     user: User = Depends(require_user),
                     state: ApiState = Depends(api_state)) -> CredentialResponse:
    if not body.token:
        raise HTTPException(status_code=422, detail="token must not be empty")
    credential = state.credentials.store(user.id, body.provider, body.token,
                                         is_default=body.isDefault)
    return CredentialResponse(**state.credentials.public_view(credential))


@router.delete("/{credential_id}", status_code=204)
def revoke_credential(credential_id: str, user: User = Depends(require_user),
                      state: ApiState = Depends(api_state)) -> None:
    # 404, not 403, on someone else's id (spec §9)
    if not state.credentials.delete(user.id, credential_id):
        raise HTTPException(status_code=404, detail="no such credential")

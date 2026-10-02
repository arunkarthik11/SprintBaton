"""Account creation and login (user-multitenancy spec §9)."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from sprintbaton.api.app import ApiState
from sprintbaton.api.auth import api_state
from sprintbaton.entities.user import User

router = APIRouter(tags=["accounts"])


class CreateAccountRequest(BaseModel):
    email: str
    password: str
    fullName: str = ""


class LoginRequest(BaseModel):
    email: str
    password: str


class AuthResponse(BaseModel):
    token: str
    userId: str
    email: str
    fullName: str


def _auth_response(user: User, token: str) -> AuthResponse:
    return AuthResponse(token=token, userId=user.id, email=user.email,
                        fullName=user.fullName)


@router.post("/accounts", status_code=201, response_model=AuthResponse)
def create_account(body: CreateAccountRequest,
                   state: ApiState = Depends(api_state)) -> AuthResponse:
    """Create a User and return a bearer token immediately — no separate
    login step required right after signup (spec §9)."""
    try:
        user, token = state.auth.create_account(
            body.email, body.password, body.fullName)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _auth_response(user, token)


@router.post("/auth/login", response_model=AuthResponse)
def login(body: LoginRequest, state: ApiState = Depends(api_state)) -> AuthResponse:
    result = state.auth.login(body.email, body.password)
    if result is None:
        raise HTTPException(status_code=401, detail="invalid credentials")
    user, token = result
    return _auth_response(user, token)

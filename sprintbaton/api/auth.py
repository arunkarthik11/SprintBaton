"""Bearer-token auth dependency (user-multitenancy spec §8): every route
except account creation and login requires `Authorization: Bearer <token>` and
resolves the caller's userId from it."""

from fastapi import Depends, HTTPException, Request

from sprintbaton.api.app import ApiState
from sprintbaton.entities.user import User


def api_state(request: Request) -> ApiState:
    return request.app.state.api


def require_user(request: Request, state: ApiState = Depends(api_state)) -> User:
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="missing bearer token")
    user = state.auth.resolve_token(token.strip())
    if user is None:
        raise HTTPException(status_code=401, detail="invalid or expired token")
    return user

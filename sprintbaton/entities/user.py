"""User, Session — accounts for hosted mode (user-multitenancy spec §3, §8).

CLI/local mode never touches these: it stamps LOCAL_USER_ID on everything and
has no login at all (spec §10). UserType.AGENT stays the identity slot for
SprintBaton's own todolist-provider user, never something account creation
produces.
"""

from pydantic import Field

from sprintbaton.entities.base import BaseEntity, new_id
from sprintbaton.entities.enums import UserType


class User(BaseEntity):
    type: UserType = UserType.HUMAN
    email: str = ""              # unique, the login identifier
    fullName: str = ""
    # One-way bcrypt hash (spec §8) — deliberately NOT the reversible envelope
    # encryption Credential uses; the two must never share a code path.
    hashedPassword: str = ""
    # No isAdmin: it gated only whose tasks could spend the operator's Claude
    # subscription, and since hosted-sandbox-isolation spec §9 every owner
    # brings their own credentials, so there is nothing left to be eligible
    # for (§12.1). A stored `isAdmin` key on an old row is ignored on read.


class Session(BaseEntity):
    """One opaque bearer token issued at signup/login (spec §8): no refresh
    dance, no scopes — one token per login, reissued each time. Only the
    SHA-256 of the token is stored, so a database dump can't impersonate."""

    id: str = Field(default_factory=lambda: new_id("session"))
    userId: str = ""
    tokenHash: str = ""     # sha256 hex of the bearer token
    expiresTime: int = 0    # epoch millis

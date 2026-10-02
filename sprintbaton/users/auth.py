"""AuthService — the smallest auth that works (user-multitenancy spec §8).

bcrypt password hashing (one-way, verify-only — deliberately a different
mechanism from Credential's reversible envelope encryption) plus opaque
bearer tokens stored server-side as a Session entity. No refresh tokens, no
scopes, no multi-device management: one token per login, reissued each time.
Tokens are stored hashed (sha256) so a database dump can't impersonate.
"""

import hashlib
import logging
import secrets

from sprintbaton.dependencies import require_storage_module
from sprintbaton.entities.base import now_millis
from sprintbaton.entities.user import Session, User
from sprintbaton.storage.base import EntityDAO

log = logging.getLogger(__name__)


def hash_password(password: str) -> str:
    # Lazy import: bcrypt is the `api` extra (only serve-api's account/login
    # surface needs it — CLI/local mode never creates a User). A tool-mode
    # install never triggers this path.
    bcrypt = require_storage_module("bcrypt", package="bcrypt", extra="api",
                                    feature="account login")

    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    bcrypt = require_storage_module("bcrypt", package="bcrypt", extra="api",
                                    feature="account login")

    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except ValueError:
        return False


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class AuthService:
    def __init__(self, user_repo: EntityDAO[User],
                 session_repo: EntityDAO[Session],
                 session_ttl_seconds: int):
        self._users = user_repo
        self._sessions = session_repo
        self._ttl_millis = session_ttl_seconds * 1000

    # ---------------------------------------------------------------- accounts

    def create_account(self, email: str, password: str,
                       full_name: str = "") -> tuple[User, str]:
        """Create a HUMAN user (account creation never produces AGENT, spec
        §3) and return it with a fresh bearer token — no separate login step
        required right after signup (spec §9)."""
        email = email.strip().lower()
        if not email or not password:
            raise ValueError("email and password are required")
        if self._users.find_one({"email": email}) is not None:
            raise ValueError(f"an account already exists for {email}")
        user = User(email=email, fullName=full_name,
                    hashedPassword=hash_password(password))
        user.createdBy = user.modifiedBy = user.id
        self._users.save(user)
        log.info("account created", extra={"user_id": user.id})
        return user, self._issue_token(user)

    def login(self, email: str, password: str) -> tuple[User, str] | None:
        """Email + password -> (user, bearer token); None on any mismatch —
        the caller can't distinguish wrong-password from no-such-account."""
        user = self._users.find_one({"email": email.strip().lower()})
        if user is None or not verify_password(password, user.hashedPassword):
            return None
        return user, self._issue_token(user)

    # ----------------------------------------------------------------- tokens

    def resolve_token(self, token: str) -> User | None:
        """Bearer token -> its user; None when unknown or expired."""
        session = self._sessions.find_one({"tokenHash": _token_hash(token)})
        if session is None or session.expiresTime <= now_millis():
            return None
        return self._users.get(session.userId)

    def _issue_token(self, user: User) -> str:
        token = secrets.token_urlsafe(32)
        session = Session(
            userId=user.id,
            tokenHash=_token_hash(token),
            expiresTime=now_millis() + self._ttl_millis,
        )
        session.createdBy = session.modifiedBy = user.id
        self._sessions.save(session)
        return token

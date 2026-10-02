"""Credential — a user-supplied third-party API token, envelope-encrypted
(user-multitenancy spec §7).

The raw token is encrypted with a per-credential data key; that data key is
itself wrapped by a master key held outside the database and referenced (not
embedded) via encryptionKeyRef. A stolen database dump yields only ciphertext
— the vault is a separate trust boundary. CredentialService.decrypt is the
only code path that ever produces plaintext, called just-in-time before the
one call site that needs it — never logged, never cached, never persisted or
returned over the API (GETs return maskedKey only).
"""

from pydantic import Field

from sprintbaton.entities.base import BaseEntity, new_id
from sprintbaton.entities.enums import CredentialProvider


class Credential(BaseEntity):
    id: str = Field(default_factory=lambda: new_id("cred"))
    userId: str = ""
    provider: CredentialProvider = CredentialProvider.GITHUB
    encryptedKey: str = ""       # token ciphertext (encrypted by the data key), base64
    encryptedDataKey: str = ""   # the per-credential data key, wrapped by the vault key
    maskedKey: str = ""          # e.g. "ghp_****wxyz" — the only form a GET ever returns
    encryptionKeyRef: str = ""   # which vault key wrapped this credential (rotation handle)
    # At most one *default* per (userId, provider) — the one resolve() picks
    # when no explicit credential id is given; any number of non-default
    # credentials may coexist as repo-level overrides, addressable only by id
    # (repository-onboarding spec §4.1).
    isDefault: bool = True

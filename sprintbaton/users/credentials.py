"""CredentialService — envelope encryption and just-in-time decryption of
user-supplied third-party API tokens (user-multitenancy spec §7).

decrypt() is the only code path in the codebase that ever produces a
plaintext token from storage. It is called immediately before the one call
site that needs the token — never logged, never cached beyond that call,
never placed on any entity or API response.
"""

import logging
from typing import Any

from cryptography.fernet import Fernet

from sprintbaton.entities.credential import Credential
from sprintbaton.entities.enums import CredentialProvider
from sprintbaton.storage.base import EntityDAO
from sprintbaton.users.vault import VaultKeyProvider

log = logging.getLogger(__name__)


def mask_token(token: str) -> str:
    """The only form of a token a GET ever returns: first 4 + last 4 chars,
    fully starred when the token is too short to reveal anything safely."""
    if len(token) <= 8:
        return "*" * len(token)
    return f"{token[:4]}****{token[-4:]}"


class CredentialService:
    def __init__(self, credential_repo: EntityDAO[Credential],
                 vault: VaultKeyProvider):
        self._repo = credential_repo
        self._vault = vault

    def store(self, user_id: str, provider: CredentialProvider,
              plaintext_token: str, is_default: bool | None = None) -> Credential:
        """Envelope-encrypt and persist: a fresh data key encrypts the token;
        the vault wraps the data key. At most one *default* credential exists
        per (user, provider) — storing a new default replaces the old one
        ("rotate" is delete + re-create, multitenancy spec §9); non-default
        credentials coexist freely as repo-level overrides (repository-
        onboarding spec §4.1). When is_default is unspecified, the first
        credential of a provider becomes the default — preserving the old
        one-token-per-provider behavior with zero new configuration."""
        if is_default is None:
            is_default = self._default_for(user_id, provider) is None
        if is_default:
            existing_default = self._default_for(user_id, provider)
            if existing_default is not None:
                self._repo.soft_delete(existing_default.id)

        data_key = Fernet.generate_key()
        credential = Credential(
            userId=user_id, createdBy=user_id, modifiedBy=user_id,
            provider=provider,
            encryptedKey=Fernet(data_key).encrypt(plaintext_token.encode()).decode(),
            encryptedDataKey=self._vault.wrap(data_key),
            maskedKey=mask_token(plaintext_token),
            encryptionKeyRef=self._vault.key_ref,
            isDefault=is_default,
        )
        self._repo.save(credential)
        log.info("credential stored", extra={
            "user_id": user_id, "provider": str(provider),
            "credential_id": credential.id,
        })
        return credential

    def decrypt(self, credential: Credential) -> str:
        data_key = self._vault.unwrap(
            credential.encryptedDataKey, credential.encryptionKeyRef)
        return Fernet(data_key).decrypt(credential.encryptedKey.encode()).decode()

    def resolve(self, user_id: str, provider: CredentialProvider,
                credential_id: str | None = None, fallback: str = "") -> str:
        """The three-tier resolution rule (repository-onboarding spec §4.2):
        an explicit credential id (a repo-level override) wins and fails
        loudly when dangling — a deleted/foreign reference is a config bug,
        not a silent fallback; otherwise the user's default credential for
        the provider; otherwise the deployment-wide Settings value — which is
        how CLI mode's plain .env tokens keep working unchanged."""
        if credential_id:
            credential = self.owned(user_id, credential_id)
            if credential is None:
                raise ValueError(f"no such credential: {credential_id}")
            return self.decrypt(credential)
        credential = self._default_for(user_id, provider)
        if credential is None:
            return fallback
        return self.decrypt(credential)

    def owned(self, user_id: str, credential_id: str) -> Credential | None:
        """The credential, iff it exists and belongs to user_id."""
        credential = self._repo.get(credential_id)
        if credential is None or credential.userId != user_id:
            return None
        return credential

    def _default_for(self, user_id: str,
                     provider: CredentialProvider) -> Credential | None:
        return self._repo.find_one({"userId": user_id, "provider": provider,
                                    "isDefault": True})

    def list_for(self, user_id: str) -> list[Credential]:
        return self._repo.find({"userId": user_id})

    def delete(self, user_id: str, credential_id: str) -> bool:
        """Ownership-checked revoke; False when the id isn't the caller's."""
        credential = self._repo.get(credential_id)
        if credential is None or credential.userId != user_id:
            return False
        self._repo.soft_delete(credential_id)
        return True

    @staticmethod
    def public_view(credential: Credential) -> dict[str, Any]:
        """The API-safe shape: provider + masked form, never ciphertext."""
        return {
            "id": credential.id,
            "provider": str(credential.provider),
            "maskedKey": credential.maskedKey,
            "isDefault": credential.isDefault,
            "createdTime": credential.createdTime,
        }

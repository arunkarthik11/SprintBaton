"""Vault key providers — the outer layer of Credential envelope encryption
(user-multitenancy spec §7).

A vault wraps/unwraps per-credential data keys with a master key held outside
the entity database, so the two are different trust boundaries: a stolen
database dump yields ciphertext the vault has to be separately compromised to
unwrap.

Two backends ship:
  - ``local``     — master key in a local file (chmod 600), generated on first
                    use. Zero platform dependencies; right-sized for CLI/local
                    mode and small hosted installs.
  - ``hashicorp`` — master key read once from a HashiCorp Vault KV secret and
                    held in memory for the process lifetime. The recommended
                    hosted-mode backend: the key never touches disk or the
                    entity database, and a multi-replica deployment shares one
                    key without a shared volume.

The master key is a Fernet key in both backends, so it lives only inside the
``Fernet`` instance for the process lifetime — it is never stored as a raw
attribute, never logged, and never placed in ``repr`` (structured loggers
commonly repr objects passed in ``extra=``).
"""

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Protocol

import httpx
from cryptography.fernet import Fernet

if TYPE_CHECKING:
    from sprintbaton.config.settings import Settings

log = logging.getLogger(__name__)


class VaultKeyProvider(Protocol):
    """Wraps and unwraps per-credential data keys (spec §7)."""

    @property
    def key_ref(self) -> str: ...

    def wrap(self, data_key: bytes) -> str: ...

    def unwrap(self, wrapped: str, key_ref: str) -> bytes: ...


class _FernetMasterKeyVault:
    """Shared envelope wrap/unwrap over a Fernet master key held in memory.

    The key material lives only inside the ``Fernet`` instance — subclasses
    pass it in and never retain a raw copy. ``__repr__`` is redacted so the
    key can't leak through a log line that reprs the provider.
    """

    def __init__(self, master_key: bytes, key_ref: str):
        self._fernet = Fernet(master_key)  # holds the key material internally
        self._key_ref = key_ref

    @property
    def key_ref(self) -> str:
        return self._key_ref

    def wrap(self, data_key: bytes) -> str:
        return self._fernet.encrypt(data_key).decode()

    def unwrap(self, wrapped: str, key_ref: str) -> bytes:
        if key_ref != self._key_ref:
            raise ValueError(
                f"credential was wrapped by vault key {key_ref!r}, "
                f"but this vault holds {self._key_ref!r}"
            )
        return self._fernet.decrypt(wrapped.encode())

    def __repr__(self) -> str:  # never reveal key material through a log/repr
        return f"{type(self).__name__}(key_ref={self._key_ref!r})"


class LocalMasterKeyVault(_FernetMasterKeyVault):
    """Master key in a local file (chmod 600), generated on first use."""

    KEY_REF = "local-master:v1"

    def __init__(self, path: str):
        self._path = Path(path).expanduser()
        super().__init__(self._load_or_create(), self.KEY_REF)

    def _load_or_create(self) -> bytes:
        if self._path.exists():
            return self._path.read_bytes().strip()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        key = Fernet.generate_key()
        self._path.touch(mode=0o600)
        self._path.write_bytes(key)
        os.chmod(self._path, 0o600)
        return key


class HashiCorpVaultKeyProvider(_FernetMasterKeyVault):
    """Master key read once from a HashiCorp Vault KV secret and kept in memory.

    The key is fetched at construction and never re-read (a process restart
    re-fetches). Only the Vault address and secret path are ever logged — never
    the token, never the key. ``key_path`` is ``<mount>/<secret-path>``; the KV
    engine version (1 or 2) governs the ``/data/`` API-path insertion.
    """

    KEY_REF = "hashicorp-master:v1"

    def __init__(
        self,
        addr: str,
        token: str,
        key_path: str,
        key_field: str = "value",
        kv_version: int = 2,
        namespace: str = "",
        verify_tls: bool = True,
        client: Optional[httpx.Client] = None,
    ):
        if not addr:
            raise ValueError(
                "VAULT_ADDR is required for the 'hashicorp' vault backend"
            )
        if not token:
            raise ValueError(
                "VAULT_TOKEN is required for the 'hashicorp' vault backend"
            )
        master_key = self._fetch_key(
            addr, token, key_path, key_field, kv_version, namespace,
            verify_tls, client,
        )
        super().__init__(master_key, self.KEY_REF)

    @staticmethod
    def _fetch_key(
        addr: str,
        token: str,
        key_path: str,
        key_field: str,
        kv_version: int,
        namespace: str,
        verify_tls: bool,
        client: Optional[httpx.Client],
    ) -> bytes:
        mount, _, subpath = key_path.strip("/").partition("/")
        if not subpath:
            raise ValueError(
                f"vault key path {key_path!r} must be '<mount>/<secret-path>'"
            )
        base = addr.rstrip("/")
        if kv_version == 2:
            url = f"{base}/v1/{mount}/data/{subpath}"
        else:
            url = f"{base}/v1/{mount}/{subpath}"

        headers = {"X-Vault-Token": token}
        if namespace:
            headers["X-Vault-Namespace"] = namespace

        # Log the location, never the token or the key value.
        log.info(
            "reading master key from vault",
            extra={"vault_addr": base, "vault_key_path": key_path},
        )

        owns_client = client is None
        client = client or httpx.Client(verify=verify_tls, timeout=10.0)
        try:
            response = client.get(url, headers=headers)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            # The message carries only the URL/status from httpx — never the
            # token (a header) nor the secret value.
            raise RuntimeError(
                f"failed to read master key from vault path {key_path!r}: {exc}"
            ) from exc
        finally:
            if owns_client:
                client.close()

        # KV v2 nests the secret under data.data; KV v1 is a flat data map.
        data = payload.get("data", {})
        if kv_version == 2:
            data = data.get("data", {})
        if key_field not in data:
            # Report the missing field name only — never the surrounding values.
            raise ValueError(
                f"vault secret {key_path!r} has no field {key_field!r}"
            )
        value = data[key_field]
        return value.encode() if isinstance(value, str) else value


def build_vault(settings: "Settings") -> VaultKeyProvider:
    """Fail-loudly vault factory, same shape as build_storage."""
    backend = settings.sprintbaton_vault_backend
    if backend == "local":
        return LocalMasterKeyVault(settings.sprintbaton_master_key_path)
    if backend == "hashicorp":
        return HashiCorpVaultKeyProvider(
            addr=settings.vault_addr,
            token=settings.vault_token,
            key_path=settings.sprintbaton_vault_key_path,
            key_field=settings.sprintbaton_vault_key_field,
            kv_version=settings.sprintbaton_vault_kv_version,
            namespace=settings.vault_namespace,
            verify_tls=not settings.vault_skip_verify,
        )
    raise ValueError(f"unknown vault backend: {backend!r}")

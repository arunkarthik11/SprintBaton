"""Azure Blob Storage BlobStore (pluggable-hosted-backends spec §4.5)."""

import logging

from sprintbaton.dependencies import require_storage_module
from sprintbaton.storage.keys import BlobKeyMixin

log = logging.getLogger(__name__)

AZURE_URL_SCHEME = "azure://"


def _is_not_found(error: Exception) -> bool:
    # azure.core.exceptions.ResourceNotFoundError carries status_code 404;
    # matched by value so an injected client's errors need no azure import.
    return getattr(error, "status_code", None) == 404


def _account_from_connection_string(connection_string: str) -> str:
    parts = dict(part.split("=", 1) for part in connection_string.split(";")
                 if "=" in part)
    return parts.get("AccountName", "")


class AzureBlobStore(BlobKeyMixin):
    """A BlobStore on one Azure Blob Storage container, keyed identically to
    every other driver (BlobKeyMixin).

    URLs are `azure://<account>/<container>/<key>` — an opaque handle, not a
    link: independent of the cloud's host suffix (public or sovereign clouds),
    private endpoints and Azurite, and never carrying a credential, SAS token
    or connection string, since stored URLs are persisted and logged.

    Credentials (decided in spec §10.4): DefaultAzureCredential (managed /
    workload identity, environment) by default; an optional connection string
    for non-identity deployments and the Azurite emulator. It never creates the
    container: a missing one is a startup error. `container_client` is a test
    seam standing in for azure.storage.blob.ContainerClient.
    """

    def __init__(self, account: str, container: str, connection_string: str = "",
                 account_url: str = "", container_client=None):
        if not container:
            raise ValueError("the azure blob backend needs AZURE_CONTAINER")
        if connection_string:
            cs_account = _account_from_connection_string(connection_string)
            if account and cs_account and account != cs_account:
                raise ValueError(
                    f"AZURE_STORAGE_ACCOUNT {account!r} disagrees with the account "
                    f"in AZURE_STORAGE_CONNECTION_STRING ({cs_account!r})")
            account = account or cs_account
        if not account:
            raise ValueError("the azure blob backend needs AZURE_STORAGE_ACCOUNT "
                             "(or an AZURE_STORAGE_CONNECTION_STRING naming one)")
        self._account = account
        self._container = container
        self._content_settings = lambda content_type: {"content_type": content_type}
        if container_client is None:
            blob = require_storage_module(
                "azure.storage.blob", package="azure-storage-blob", extra="azure",
                feature="the azure blob backend")
            if connection_string:
                service = blob.BlobServiceClient.from_connection_string(connection_string)
            else:
                identity = require_storage_module(
                    "azure.identity", package="azure-identity", extra="azure",
                    feature="the azure blob backend")
                service = blob.BlobServiceClient(
                    account_url or f"https://{account}.blob.core.windows.net",
                    credential=identity.DefaultAzureCredential())
            container_client = service.get_container_client(container)
            self._content_settings = (
                lambda content_type: blob.ContentSettings(content_type=content_type))
        self._client = container_client
        self._ensure_container()

    def _ensure_container(self) -> None:
        # A one-blob listing rather than exists(): listing is a data-plane
        # operation every Storage Blob Data role grants.
        try:
            next(iter(self._client.list_blobs(results_per_page=1)), None)
        except Exception as e:
            if _is_not_found(e):
                raise RuntimeError(
                    f"Azure container {self._container!r} does not exist in "
                    f"storage account {self._account!r} (AZURE_CONTAINER); create "
                    "it before starting SprintBaton") from e
            raise

    def _put(self, key: str, data: bytes, content_type: str) -> str:
        self._client.upload_blob(key, data, overwrite=True,
                                 content_settings=self._content_settings(content_type))
        return self.url_for(key)

    def _get(self, key: str) -> bytes | None:
        try:
            return self._client.download_blob(key).readall()
        except Exception as e:
            if _is_not_found(e):
                return None
            raise

    def put_text(self, key: str, text: str) -> str:
        return self._put(key, text.encode("utf-8"), "text/markdown")

    def get_text(self, key: str) -> str | None:
        data = self._get(key)
        return data.decode("utf-8") if data is not None else None

    def put_bytes(self, key: str, data: bytes,
                  content_type: str = "application/octet-stream") -> str:
        return self._put(key, data, content_type)

    def get_bytes(self, key: str) -> bytes | None:
        return self._get(key)

    def get_text_by_url(self, url: str) -> str | None:
        key = self.key_for(url)
        return self.get_text(key) if key is not None else None

    def url_for(self, key: str) -> str:
        return f"{AZURE_URL_SCHEME}{self._account}/{self._container}/{key}"

    def key_for(self, url: str) -> str | None:
        """Strict prefix match on this account and container; None otherwise."""
        prefix = f"{AZURE_URL_SCHEME}{self._account}/{self._container}/"
        return url[len(prefix):] if url.startswith(prefix) else None

    def list_keys(self, prefix: str = "") -> list[str]:
        # The client's item pager follows continuation tokens itself.
        return [b.name for b in self._client.list_blobs(name_starts_with=prefix or None)]

    def delete(self, key: str) -> None:
        """Idempotent: a missing key is a no-op."""
        try:
            self._client.delete_blob(key)
        except Exception as e:
            if not _is_not_found(e):
                raise

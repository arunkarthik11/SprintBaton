"""Google Cloud Storage BlobStore (pluggable-hosted-backends spec §4.4)."""

import logging

from sprintbaton.dependencies import require_storage_module
from sprintbaton.storage.keys import BlobKeyMixin

log = logging.getLogger(__name__)


def _is_not_found(error: Exception) -> bool:
    # google.api_core.exceptions.NotFound carries code 404; matched by value so
    # an injected client's errors need no google-api-core import.
    return getattr(error, "code", None) == 404


class GcsBlobStore(BlobKeyMixin):
    """A BlobStore on a GCS bucket, keyed identically to every other driver
    (BlobKeyMixin) and addressed as `gs://<bucket>/<key>`.

    Authenticates with application-default credentials (workload identity on
    GKE, GOOGLE_APPLICATION_CREDENTIALS, gcloud's own login) — no static key
    setting. It never creates the bucket: a missing one is a startup error.
    `client` is a test seam standing in for google.cloud.storage.Client.
    """

    def __init__(self, bucket: str, project: str = "", client=None):
        if not bucket:
            raise ValueError("the gcs blob backend needs GCS_BUCKET")
        if client is None:
            storage = require_storage_module(
                "google.cloud.storage", package="google-cloud-storage",
                extra="gcs", feature="the gcs blob backend")
            client = storage.Client(project=project or None)
        self._client = client
        self._bucket_name = bucket
        self._bucket = client.bucket(bucket)
        self._ensure_bucket()

    def _ensure_bucket(self) -> None:
        # A one-object listing rather than Bucket.exists(): listing needs only
        # storage.objects.list, which the driver needs anyway, where exists()
        # needs storage.buckets.get — outside a least-privilege objects role.
        try:
            next(iter(self._client.list_blobs(self._bucket_name, max_results=1)), None)
        except Exception as e:
            if _is_not_found(e):
                raise RuntimeError(
                    f"GCS bucket {self._bucket_name!r} does not exist (GCS_BUCKET); "
                    "create it before starting SprintBaton") from e
            raise

    def _put(self, key: str, data: bytes, content_type: str) -> str:
        self._bucket.blob(key).upload_from_string(data, content_type=content_type)
        return self.url_for(key)

    def _get(self, key: str) -> bytes | None:
        try:
            return self._bucket.blob(key).download_as_bytes()
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
        return f"gs://{self._bucket_name}/{key}"

    def key_for(self, url: str) -> str | None:
        """Inverse of url_for; None when the URL is not in this bucket."""
        prefix = f"gs://{self._bucket_name}/"
        return url[len(prefix):] if url.startswith(prefix) else None

    def list_keys(self, prefix: str = "") -> list[str]:
        # The client's iterator follows page tokens itself.
        return [blob.name for blob in
                self._client.list_blobs(self._bucket_name, prefix=prefix or None)]

    def delete(self, key: str) -> None:
        """Idempotent: a missing key is a no-op."""
        try:
            self._bucket.blob(key).delete()
        except Exception as e:
            if not _is_not_found(e):
                raise

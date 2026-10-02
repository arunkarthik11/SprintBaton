import logging

from sprintbaton.dependencies import require_storage_module
from sprintbaton.storage.keys import BlobKeyMixin

log = logging.getLogger(__name__)


class S3BlobStore(BlobKeyMixin):
    """S3 BlobStore for large artifacts: plans, finalized specs, situation
    reports, large messages, and repository-metadata bundles. The hosted-mode
    default (zero-infra-storage spec §3.1).

    Vendor-neutral (pluggable-hosted-backends spec §4.3): the same driver
    talks to AWS S3, MinIO and other S3-compatible stores. An empty endpoint
    means the client library's default endpoint for the region (real AWS);
    empty credentials mean the default credential chain (environment, shared
    config, web identity/IRSA, instance metadata). `client` is a test seam.
    """

    ADDRESSING_STYLES = ("path", "virtual", "auto")

    def __init__(self, bucket: str, endpoint_url: str = "", region: str = "",
                 access_key: str = "", secret_key: str = "",
                 create_bucket: bool = False, addressing_style: str = "",
                 client=None):
        if bool(access_key) != bool(secret_key):
            raise ValueError(
                "S3_ACCESS_KEY and S3_SECRET_KEY must be set together, or both "
                "left empty to use the default AWS credential chain")
        if addressing_style and addressing_style not in self.ADDRESSING_STYLES:
            raise ValueError(
                f"unknown S3_ADDRESSING_STYLE {addressing_style!r}; expected one "
                f"of {', '.join(self.ADDRESSING_STYLES)}")
        # Lazy import (zero-infra-storage spec §11.1): boto3 is the `s3`
        # extra; a tool-mode install never triggers this path.
        require_storage_module("boto3", package="boto3", extra="s3",
                               feature="the s3 blob backend")
        from botocore.exceptions import ClientError

        self._client_error = ClientError
        self._bucket = bucket
        self._region = region
        if client is None:
            import boto3
            from botocore.config import Config

            kwargs: dict = {}
            if endpoint_url:
                kwargs["endpoint_url"] = endpoint_url
            if region:
                kwargs["region_name"] = region
            if access_key:
                kwargs["aws_access_key_id"] = access_key
                kwargs["aws_secret_access_key"] = secret_key
            if addressing_style:
                kwargs["config"] = Config(s3={"addressing_style": addressing_style})
            client = boto3.client("s3", **kwargs)
        self._client = client
        self._ensure_bucket(create_bucket)

    def _ensure_bucket(self, create: bool) -> None:
        try:
            self._client.head_bucket(Bucket=self._bucket)
            return
        except self._client_error as e:
            code = str(e.response.get("Error", {}).get("Code", ""))
            if code not in ("404", "NoSuchBucket", "NotFound"):
                raise
        if not create:
            raise RuntimeError(
                f"S3 bucket {self._bucket!r} does not exist; create it, or set "
                "S3_CREATE_BUCKET=true to let SprintBaton create it")
        kwargs: dict = {"Bucket": self._bucket}
        # us-east-1 is the one region the API rejects an explicit constraint for.
        if self._region and self._region != "us-east-1":
            kwargs["CreateBucketConfiguration"] = {"LocationConstraint": self._region}
        self._client.create_bucket(**kwargs)
        log.info("created bucket", extra={"bucket": self._bucket})

    def put_text(self, key: str, text: str) -> str:
        self._client.put_object(
            Bucket=self._bucket, Key=key, Body=text.encode("utf-8"),
            ContentType="text/markdown",
        )
        return self.url_for(key)

    def get_text(self, key: str) -> str | None:
        try:
            resp = self._client.get_object(Bucket=self._bucket, Key=key)
            return resp["Body"].read().decode("utf-8")
        except self._client_error as e:
            if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
                return None
            raise

    def put_bytes(self, key: str, data: bytes,
                  content_type: str = "application/octet-stream") -> str:
        """Binary counterpart of put_text — used for git bundles (classification
        provenance spec §7), which are not UTF-8 text."""
        self._client.put_object(
            Bucket=self._bucket, Key=key, Body=data, ContentType=content_type,
        )
        return self.url_for(key)

    def get_bytes(self, key: str) -> bytes | None:
        try:
            resp = self._client.get_object(Bucket=self._bucket, Key=key)
            return resp["Body"].read()
        except self._client_error as e:
            if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
                return None
            raise

    def get_text_by_url(self, url: str) -> str | None:
        key = self.key_for(url)
        return self.get_text(key) if key is not None else None

    def url_for(self, key: str) -> str:
        return f"s3://{self._bucket}/{key}"

    def key_for(self, url: str) -> str | None:
        """Inverse of url_for; None when the URL is not in this bucket."""
        prefix = f"s3://{self._bucket}/"
        return url[len(prefix):] if url.startswith(prefix) else None

    def list_keys(self, prefix: str = "") -> list[str]:
        # Paginated: a metadata root spans every retained revision, which can
        # exceed one list_objects_v2 page (1000 keys).
        keys: list[str] = []
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
            keys.extend(obj["Key"] for obj in page.get("Contents", []))
        return keys

    def delete(self, key: str) -> None:
        """S3 DeleteObject is already idempotent — a missing key succeeds
        (project-initialization-task spec §9.3)."""
        self._client.delete_object(Bucket=self._bucket, Key=key)


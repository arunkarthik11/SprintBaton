import threading
from typing import TYPE_CHECKING

from sprintbaton.storage.base import (
    BlobStore,
    DistributedLock,
    EntityDAO,
    PersistenceBackend,
    TaskQueue,
)
from sprintbaton.storage.blob_azure import AzureBlobStore
from sprintbaton.storage.blob_fs import FilesystemBlobStore
from sprintbaton.storage.blob_gcs import GcsBlobStore
from sprintbaton.storage.lock_fs import FileLock
from sprintbaton.storage.mongo import EntityRepository, MongoStorage
from sprintbaton.storage.object_storage import S3BlobStore
from sprintbaton.storage.postgres import PostgresEntityRepository, PostgresStorage
from sprintbaton.storage.queue_local import InProcessTaskQueue
from sprintbaton.storage.redis_cache import RedisCache
from sprintbaton.storage.sqlite import SqliteEntityRepository, SqliteStorage

if TYPE_CHECKING:
    from sprintbaton.config.settings import Settings

# Every factory below resolves its backend through Settings.backend_for —
# the two-level SPRINTBATON_MODE / per-concern-var selection (zero-infra-
# storage spec §6) — and fails loudly on unknown names, the same shape as
# HarnessRegistry.get and create_adapter. The hosted-only modules import
# their clients lazily inside __init__ (spec §11.1), so importing this module
# never requires any storage extra (pluggable-hosted-backends spec §5).


def build_storage(settings: "Settings") -> PersistenceBackend:
    """The persistence-backend factory (persistence-abstraction spec §5)."""
    backend = settings.backend_for("persistence")
    if backend == "mongo":
        return MongoStorage(settings.mongo_base_uri, settings.mongo_database)
    if backend == "postgres":
        return PostgresStorage(settings.postgres_dsn, settings.postgres_schema,
                               settings.postgres_pool_max_size)
    if backend == "sqlite":
        return SqliteStorage(settings.resolved_sqlite_path)
    raise ValueError(f"unknown persistence backend: {backend!r}")


def build_blob_store(settings: "Settings") -> BlobStore:
    backend = settings.backend_for("blob")
    if backend == "s3":
        return S3BlobStore(
            bucket=settings.s3_bucket,
            endpoint_url=settings.s3_endpoint,
            region=settings.s3_region,
            access_key=settings.s3_access_key,
            secret_key=settings.s3_secret_key,
            create_bucket=settings.s3_create_bucket,
            addressing_style=settings.s3_addressing_style,
        )
    if backend == "gcs":
        return GcsBlobStore(bucket=settings.gcs_bucket, project=settings.gcs_project)
    if backend == "azure":
        return AzureBlobStore(
            account=settings.azure_storage_account,
            container=settings.azure_container,
            connection_string=settings.azure_storage_connection_string,
            account_url=settings.azure_account_url,
        )
    if backend == "filesystem":
        return FilesystemBlobStore(settings.resolved_blob_root)
    raise ValueError(f"unknown blob backend: {backend!r}")


_redis_clients: dict[str, RedisCache] = {}
_redis_clients_lock = threading.Lock()


def shared_redis(uri: str) -> RedisCache:
    """One RedisCache — so one connection pool — per process per URI
    (pluggable-hosted-backends spec §4.1). The queue, the lock and the
    config cache all come through here, so a deployment where all three are
    Redis-backed opens a single pool rather than three. This is the only
    place outside storage/redis_cache.py that constructs RedisCache."""
    with _redis_clients_lock:
        client = _redis_clients.get(uri)
        if client is None:
            client = _redis_clients[uri] = RedisCache(uri)
        return client


def build_task_queue(settings: "Settings") -> TaskQueue:
    backend = settings.backend_for("queue")
    if backend == "redis":
        return shared_redis(settings.redis_uri)
    if backend == "in_process":
        return InProcessTaskQueue()
    raise ValueError(f"unknown queue backend: {backend!r}")


def build_lock(settings: "Settings") -> DistributedLock:
    backend = settings.backend_for("lock")
    if backend == "redis":
        return shared_redis(settings.redis_uri)
    if backend == "file":
        return FileLock(str(settings.local_storage_root / "locks"))
    raise ValueError(f"unknown lock backend: {backend!r}")


def build_cache_client(settings: "Settings") -> RedisCache | None:
    """The Redis client the config cache needs, or None when it needs none
    (spec §4.1). build_config_cache keeps its own fail-loudly validation of
    the backend name; this only decides whether a client exists."""
    if settings.sprintbaton_config_cache_backend == "redis":
        return shared_redis(settings.redis_uri)
    return None


__all__ = [
    "EntityDAO",
    "PersistenceBackend",
    "BlobStore",
    "TaskQueue",
    "DistributedLock",
    "MongoStorage",
    "EntityRepository",
    "SqliteStorage",
    "SqliteEntityRepository",
    "PostgresStorage",
    "PostgresEntityRepository",
    "RedisCache",
    "S3BlobStore",
    "GcsBlobStore",
    "AzureBlobStore",
    "FilesystemBlobStore",
    "InProcessTaskQueue",
    "FileLock",
    "build_storage",
    "build_blob_store",
    "build_task_queue",
    "build_lock",
    "build_cache_client",
    "shared_redis",
]

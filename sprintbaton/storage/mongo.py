import logging
from typing import Any, Generic, TypeVar

from sprintbaton.dependencies import require_storage_module
from sprintbaton.entities.base import BaseEntity

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseEntity)


class EntityRepository(Generic[T]):
    """Generic Mongo repository for BaseEntity subclasses.

    Entities are stored with their `id` as Mongo `_id`. Soft-deleted entities
    are excluded from reads unless include_deleted is set.
    """

    def __init__(self, collection, entity_cls: type[T]):
        self._collection = collection
        self._entity_cls = entity_cls

    def ensure_indexes(self, *fields: str) -> None:
        """Idempotently create a single-field index per field name."""
        for field in fields:
            self._collection.create_index(field)

    def save(self, entity: T) -> T:
        doc = entity.model_dump(mode="json")
        doc["_id"] = doc.pop("id")
        self._collection.replace_one({"_id": doc["_id"]}, doc, upsert=True)
        return entity

    def get(self, entity_id: str, include_deleted: bool = False) -> T | None:
        doc = self._collection.find_one({"_id": entity_id})
        if doc is None:
            return None
        if doc.get("deleted") and not include_deleted:
            return None
        return self._to_entity(doc)

    def find(self, query: dict[str, Any], limit: int = 0) -> list[T]:
        query = {"deleted": False, **query}
        cursor = self._collection.find(query)
        if limit:
            cursor = cursor.limit(limit)
        return [self._to_entity(doc) for doc in cursor]

    def find_one(self, query: dict[str, Any]) -> T | None:
        results = self.find(query, limit=1)
        return results[0] if results else None

    def soft_delete(self, entity_id: str) -> None:
        self._collection.update_one({"_id": entity_id}, {"$set": {"deleted": True}})

    def _to_entity(self, doc: dict[str, Any]) -> T:
        doc = dict(doc)
        doc["id"] = doc.pop("_id")
        return self._entity_cls.model_validate(doc)


def _instrument_pymongo() -> None:
    """OTel pymongo instrumentation, activated only when the Mongo backend is
    the selected one (pluggable-hosted-backends spec §4.6), and before the
    client exists so its command listener is registered. Absence of the
    instrumentation package is a silent no-op."""
    try:
        from opentelemetry.instrumentation.pymongo import PymongoInstrumentor
    except ImportError:
        return
    instrumentor = PymongoInstrumentor()
    if not instrumentor.is_instrumented_by_opentelemetry:
        instrumentor.instrument()


class MongoStorage:
    def __init__(self, base_uri: str, database: str):
        # Lazy import (zero-infra-storage spec §11.1): pymongo is the `mongo`
        # extra; a tool-mode install never constructs this class.
        pymongo = require_storage_module(
            "pymongo", package="pymongo", extra="mongo",
            feature="the mongo persistence backend")

        _instrument_pymongo()
        self._client = pymongo.MongoClient(base_uri)
        self._db = self._client[database]
        log.info("connected to mongo", extra={"database": database})

    def repository(self, entity_cls: type[T], collection: str) -> EntityRepository[T]:
        return EntityRepository(self._db[collection], entity_cls)

    def close(self) -> None:
        self._client.close()

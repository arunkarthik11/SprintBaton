"""SqliteStorage — the file-based local persistence backend
(docs/persistence-abstraction-spec.md §4.2).

Deliberately schemaless, mirroring Mongo: one table per collection, two
columns (id, doc-as-JSON). `find`/`find_one` scan the table and match the
equality-only filter dict against the parsed document in Python — never a
compiled SQL WHERE clause — so the query semantics can't silently diverge
from Mongo's. At this backend's target scale (one local user's tasks) a
full-table scan is not a performance concern; indexed querying is a flagged
later optimization (spec §12), which is also why `ensure_indexes` is a
documented no-op here.
"""

import json
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any, Generic, TypeVar

from sprintbaton.entities.base import BaseEntity

T = TypeVar("T", bound=BaseEntity)

_COLLECTION_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class SqliteEntityRepository(Generic[T]):
    """EntityDAO over one SQLite table. Same contract as the Mongo
    EntityRepository: upsert save, soft-delete-aware reads, equality-only
    filter dicts."""

    def __init__(self, connection: sqlite3.Connection, write_lock: threading.Lock,
                 collection: str, entity_cls: type[T]):
        if not _COLLECTION_NAME.match(collection):
            raise ValueError(f"invalid collection name: {collection!r}")
        self._conn = connection
        self._lock = write_lock
        self._table = collection
        self._entity_cls = entity_cls
        with self._lock, self._conn:
            self._conn.execute(
                f"CREATE TABLE IF NOT EXISTS {self._table} "
                "(id TEXT PRIMARY KEY, doc TEXT NOT NULL)"
            )

    def ensure_indexes(self, *fields: str) -> None:
        """No-op (spec §4.2): queries scan-and-filter regardless, so secondary
        indexes change nothing about correctness — only large-scale performance,
        which is not this backend's target use case."""

    def save(self, entity: T) -> T:
        doc = json.dumps(entity.model_dump(mode="json"))
        with self._lock, self._conn:
            self._conn.execute(
                f"INSERT OR REPLACE INTO {self._table} (id, doc) VALUES (?, ?)",
                (entity.id, doc),
            )
        return entity

    def get(self, entity_id: str, include_deleted: bool = False) -> T | None:
        row = self._conn.execute(
            f"SELECT doc FROM {self._table} WHERE id = ?", (entity_id,)
        ).fetchone()
        if row is None:
            return None
        doc = json.loads(row[0])
        if doc.get("deleted") and not include_deleted:
            return None
        return self._entity_cls.model_validate(doc)

    def find(self, query: dict[str, Any], limit: int = 0) -> list[T]:
        query = {"deleted": False, **query}
        results: list[T] = []
        for (raw,) in self._conn.execute(f"SELECT doc FROM {self._table}"):
            doc = json.loads(raw)
            if all(self._matches(doc.get(key), value) for key, value in query.items()):
                results.append(self._entity_cls.model_validate(doc))
                if limit and len(results) >= limit:
                    break
        return results

    def find_one(self, query: dict[str, Any]) -> T | None:
        results = self.find(query, limit=1)
        return results[0] if results else None

    def soft_delete(self, entity_id: str) -> None:
        entity = self.get(entity_id, include_deleted=True)
        if entity is None:
            return
        entity.deleted = True
        self.save(entity)

    @staticmethod
    def _matches(doc_value: Any, query_value: Any) -> bool:
        """Equality with the same tolerance Mongo shows for enum/str query
        values: StrEnums are str subclasses, so `==` already matches the
        stored JSON string; everything else is plain equality."""
        return doc_value == query_value


class SqliteStorage:
    """PersistenceBackend over one .db file (or ":memory:").

    Concurrency (spec §7): `sprintbaton serve` hits the same repositories from
    the polling thread, the release-window thread, and the main dequeue loop,
    so the shared connection is opened with check_same_thread=False and WAL
    journaling, and every write is serialized behind one process-wide lock.
    """

    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)
            path = str(Path(path).expanduser())
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._write_lock = threading.Lock()

    def repository(self, entity_cls: type[T], collection: str) -> SqliteEntityRepository[T]:
        return SqliteEntityRepository(self._conn, self._write_lock, collection, entity_cls)

    def close(self) -> None:
        self._conn.close()

"""PostgresStorage — the second hosted persistence backend
(pluggable-hosted-backends spec §4.2).

The same document model as SqliteStorage: one table per collection, the
entity `id` as primary key, the entity serialized as one JSON document (here a
JSONB column), soft delete through the document's own `deleted` flag. Unlike
SQLite, the equality-only filter compiles to SQL — `doc->'<field>' = <json>` —
so `ensure_indexes` can create a real expression index per field, which the
planner uses because the key is inlined as a literal, never a bind parameter.

Equality semantics match the other two backends field for field: a value
matches when its JSON form is equal (strings, StrEnums, numbers, booleans,
lists and objects alike — a list matches only an equal list), and None matches
a null or absent field. Filter keys are top-level field names only, as in
SQLite; anything else is rejected rather than silently matching nothing.

Connection pooling is psycopg_pool's; TLS and every other connection option
ride the DSN as standard libpq parameters (`sslmode=verify-full`, …).
"""

import hashlib
import json
import logging
import re
from typing import Any, Generic, TypeVar

from sprintbaton.dependencies import require_storage_module
from sprintbaton.entities.base import BaseEntity

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseEntity)

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MAX_IDENTIFIER = 63  # Postgres NAMEDATALEN - 1


def _identifier(value: str, what: str) -> str:
    if not _IDENTIFIER.match(value):
        raise ValueError(f"invalid {what}: {value!r}")
    return value


def _index_name(table: str, field: str) -> str:
    name = f"{table}_{field}_idx"
    if len(name) <= _MAX_IDENTIFIER:
        return name
    digest = hashlib.sha1(name.encode()).hexdigest()[:10]
    return f"{name[:_MAX_IDENTIFIER - 11]}_{digest}"


def compile_filter(query: dict[str, Any]) -> tuple[str, list[str]]:
    """An equality-only filter dict as a WHERE clause over the `doc` column,
    plus its parameters (JSON text, cast to jsonb). Keys are validated and
    inlined as literals so expression indexes apply."""
    clauses: list[str] = []
    params: list[str] = []
    for key, value in query.items():
        path = f"doc->'{_identifier(key, 'filter field')}'"
        if value is None:
            clauses.append(f"({path} IS NULL OR {path} = 'null'::jsonb)")
        else:
            clauses.append(f"{path} = %s::jsonb")
            params.append(json.dumps(value))
    return (" AND ".join(clauses) or "TRUE"), params


class PostgresEntityRepository(Generic[T]):
    """EntityDAO over one Postgres table (same contract as the Mongo and
    SQLite repositories)."""

    def __init__(self, pool, schema: str, collection: str, entity_cls: type[T]):
        self._pool = pool
        self._table_name = _identifier(collection, "collection name")
        self._table = (f'"{schema}"."{self._table_name}"' if schema
                       else f'"{self._table_name}"')
        self._entity_cls = entity_cls
        # seq keeps results in first-insert order, which an upsert preserves.
        self._execute(
            f"CREATE TABLE IF NOT EXISTS {self._table} ("
            "id TEXT PRIMARY KEY, seq BIGSERIAL, doc JSONB NOT NULL)")

    def _execute(self, sql: str, params: list | tuple = ()) -> list[tuple]:
        with self._pool.connection() as conn:
            cur = conn.execute(sql, params)
            return cur.fetchall() if cur.description else []

    def ensure_indexes(self, *fields: str) -> None:
        """One expression index per field on `doc->'<field>'`, idempotently."""
        for field in fields:
            field = _identifier(field, "index field")
            self._execute(
                f'CREATE INDEX IF NOT EXISTS "{_index_name(self._table_name, field)}" '
                f"ON {self._table} ((doc->'{field}'))")

    def save(self, entity: T) -> T:
        self._execute(
            f"INSERT INTO {self._table} (id, doc) VALUES (%s, %s::jsonb) "
            "ON CONFLICT (id) DO UPDATE SET doc = EXCLUDED.doc",
            (entity.id, json.dumps(entity.model_dump(mode="json"))))
        return entity

    def get(self, entity_id: str, include_deleted: bool = False) -> T | None:
        rows = self._execute(f"SELECT doc FROM {self._table} WHERE id = %s",
                             (entity_id,))
        if not rows:
            return None
        doc = rows[0][0]
        if doc.get("deleted") and not include_deleted:
            return None
        return self._entity_cls.model_validate(doc)

    def find(self, query: dict[str, Any], limit: int = 0) -> list[T]:
        where, params = compile_filter({"deleted": False, **query})
        sql = f"SELECT doc FROM {self._table} WHERE {where} ORDER BY seq"
        if limit:
            sql += " LIMIT %s"
            params.append(limit)
        return [self._entity_cls.model_validate(doc)
                for (doc,) in self._execute(sql, params)]

    def find_one(self, query: dict[str, Any]) -> T | None:
        results = self.find(query, limit=1)
        return results[0] if results else None

    def soft_delete(self, entity_id: str) -> None:
        self._execute(
            f"UPDATE {self._table} SET doc = jsonb_set(doc, '{{deleted}}', 'true') "
            "WHERE id = %s", (entity_id,))


class PostgresStorage:
    """PersistenceBackend over one Postgres database. `pool` is a test seam
    standing in for a psycopg_pool.ConnectionPool."""

    def __init__(self, dsn: str, schema: str = "", pool_max_size: int = 10,
                 pool=None):
        self._schema = _identifier(schema, "schema name") if schema else ""
        if pool is None:
            # Lazy import (spec §4.2): psycopg is the `postgres` extra.
            require_storage_module("psycopg", package="psycopg", extra="postgres",
                                   feature="the postgres persistence backend")
            pool_module = require_storage_module(
                "psycopg_pool", package="psycopg-pool", extra="postgres",
                feature="the postgres persistence backend")
            if not dsn:
                raise ValueError("the postgres persistence backend needs POSTGRES_DSN")
            pool = pool_module.ConnectionPool(
                dsn, min_size=1, max_size=pool_max_size,
                kwargs={"autocommit": True}, open=True)
        self._pool = pool
        if self._schema:
            with self._pool.connection() as conn:
                conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{self._schema}"')
        log.info("connected to postgres", extra={"schema": self._schema or "(default)"})

    def repository(self, entity_cls: type[T], collection: str) -> PostgresEntityRepository[T]:
        return PostgresEntityRepository(self._pool, self._schema, collection, entity_cls)

    def close(self) -> None:
        self._pool.close()

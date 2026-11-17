from __future__ import annotations

import heapq
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from flmh_agent.config import Settings
from flmh_agent.domain.sessions import (
    SessionCursor, SessionNotFoundError, SessionPage, SessionRecord,
    SessionVersionConflict, session_page,
)
from flmh_agent.ports.sessions import SessionDirectoryPort


class InMemorySessionDirectory:
    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], SessionRecord] = {}

    async def find(self, owner_id: str, run_id: str) -> SessionRecord | None:
        return self._rows.get((owner_id, run_id))

    async def reserve(self, owner_id: str, run_id: str, title: str) -> SessionRecord:
        previous = self._rows.get((owner_id, run_id))
        if previous is not None:
            return _active(previous)
        now = datetime.now(timezone.utc)
        record = SessionRecord(owner_id, run_id, title, "", "starting", 1, now, now)
        self._rows[(owner_id, run_id)] = record
        return record

    async def list(self, owner_id: str, limit: int, cursor: SessionCursor | None) -> SessionPage:
        boundary = (cursor.updated_at, cursor.run_id) if cursor else None
        records = (row for row in self._rows.values() if row.owner_id == owner_id and row.deleted_at is None
                   and (boundary is None or (row.updated_at, row.run_id) < boundary))
        selected = heapq.nlargest(limit + 1, records, key=lambda row: (row.updated_at, row.run_id))
        return session_page(selected, limit)

    async def synchronize(self, owner_id: str, run_id: str, preview: str, status: str) -> SessionRecord:
        previous = _active(self._rows.get((owner_id, run_id)))
        if previous.preview == preview and previous.status == status:
            return previous
        record = replace(previous, preview=preview, status=status, version=previous.version + 1,
                         updated_at=datetime.now(timezone.utc))
        self._rows[(owner_id, run_id)] = record
        return record

    async def rename(self, owner_id: str, run_id: str, title: str, expected_version: int) -> SessionRecord:
        previous = _active(self._rows.get((owner_id, run_id)))
        _version(previous, expected_version)
        if previous.title == title:
            return previous
        record = replace(previous, title=title, version=previous.version + 1, updated_at=datetime.now(timezone.utc))
        self._rows[(owner_id, run_id)] = record
        return record

    async def delete(self, owner_id: str, run_id: str, expected_version: int) -> SessionRecord:
        previous = self._rows.get((owner_id, run_id))
        if previous is None:
            raise SessionNotFoundError("session does not exist")
        if previous.deleted_at is not None:
            return previous
        _version(previous, expected_version)
        now = datetime.now(timezone.utc)
        record = replace(previous, version=previous.version + 1, updated_at=now, deleted_at=now)
        self._rows[(owner_id, run_id)] = record
        return record


_COLUMNS = "owner_id, run_id, title, preview, run_status, version, created_at, updated_at, deleted_at"
_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS agent_session_directory (
    owner_id TEXT NOT NULL,
    run_id VARCHAR(128) NOT NULL,
    title VARCHAR(80) NOT NULL,
    preview VARCHAR(280) NOT NULL DEFAULT '',
    run_status VARCHAR(40) NOT NULL DEFAULT 'starting',
    version BIGINT NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT statement_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT statement_timestamp(),
    deleted_at TIMESTAMPTZ,
    PRIMARY KEY (owner_id, run_id)
)
"""
_CREATE_INDEX = """
CREATE INDEX IF NOT EXISTS agent_session_directory_owner_active_idx
ON agent_session_directory (owner_id, updated_at DESC, run_id DESC)
WHERE deleted_at IS NULL
"""


class PostgresSessionDirectory:
    def __init__(self, pool: Any) -> None:
        self.pool = pool

    async def setup(self) -> None:
        async with self.pool.connection() as connection:
            await connection.execute(_CREATE_TABLE)
            await connection.execute(_CREATE_INDEX)

    async def find(self, owner_id: str, run_id: str) -> SessionRecord | None:
        async with self.pool.connection() as connection:
            return await self._find(connection, owner_id, run_id)

    async def _find(self, connection: Any, owner_id: str, run_id: str) -> SessionRecord | None:
        result = await connection.execute(
            f"SELECT {_COLUMNS} FROM agent_session_directory WHERE owner_id = %s AND run_id = %s",
            (owner_id, run_id),
        )
        row = await result.fetchone()
        return SessionRecord(*row) if row else None

    async def reserve(self, owner_id: str, run_id: str, title: str) -> SessionRecord:
        async with self.pool.connection() as connection:
            result = await connection.execute(
                f"INSERT INTO agent_session_directory (owner_id, run_id, title) VALUES (%s, %s, %s) "
                f"ON CONFLICT (owner_id, run_id) DO NOTHING RETURNING {_COLUMNS}",
                (owner_id, run_id, title),
            )
            row = await result.fetchone()
            return _active(SessionRecord(*row) if row else await self._find(connection, owner_id, run_id))

    async def list(self, owner_id: str, limit: int, cursor: SessionCursor | None) -> SessionPage:
        params: tuple[Any, ...] = (owner_id,)
        boundary = ""
        if cursor:
            boundary = " AND (updated_at, run_id) < (%s, %s)"
            params += (cursor.updated_at, cursor.run_id)
        async with self.pool.connection() as connection:
            result = await connection.execute(
                f"SELECT {_COLUMNS} FROM agent_session_directory WHERE owner_id = %s AND deleted_at IS NULL"
                f"{boundary} ORDER BY updated_at DESC, run_id DESC LIMIT %s", params + (limit + 1,),
            )
            return session_page([SessionRecord(*row) for row in await result.fetchall()], limit)

    async def synchronize(self, owner_id: str, run_id: str, preview: str, status: str) -> SessionRecord:
        async with self.pool.connection() as connection:
            result = await connection.execute(
                f"UPDATE agent_session_directory SET preview = %s, run_status = %s, version = version + 1, "
                "updated_at = statement_timestamp() WHERE owner_id = %s AND run_id = %s AND deleted_at IS NULL "
                f"AND (preview IS DISTINCT FROM %s OR run_status IS DISTINCT FROM %s) RETURNING {_COLUMNS}",
                (preview, status, owner_id, run_id, preview, status),
            )
            row = await result.fetchone()
            return _active(SessionRecord(*row) if row else await self._find(connection, owner_id, run_id))

    async def rename(self, owner_id: str, run_id: str, title: str, expected_version: int) -> SessionRecord:
        async with self.pool.connection() as connection:
            result = await connection.execute(
                "UPDATE agent_session_directory SET title = %s, version = version + 1, updated_at = statement_timestamp() "
                "WHERE owner_id = %s AND run_id = %s AND version = %s AND deleted_at IS NULL "
                f"AND title IS DISTINCT FROM %s RETURNING {_COLUMNS}",
                (title, owner_id, run_id, expected_version, title),
            )
            row = await result.fetchone()
            if row:
                return SessionRecord(*row)
            previous = _active(await self._find(connection, owner_id, run_id))
            _version(previous, expected_version)
            return previous

    async def delete(self, owner_id: str, run_id: str, expected_version: int) -> SessionRecord:
        async with self.pool.connection() as connection:
            result = await connection.execute(
                "UPDATE agent_session_directory SET deleted_at = statement_timestamp(), updated_at = statement_timestamp(), "
                "version = version + 1 WHERE owner_id = %s AND run_id = %s AND version = %s AND deleted_at IS NULL "
                f"RETURNING {_COLUMNS}", (owner_id, run_id, expected_version),
            )
            row = await result.fetchone()
            if row:
                return SessionRecord(*row)
            previous = await self._find(connection, owner_id, run_id)
            if previous is None:
                raise SessionNotFoundError("session does not exist")
            if previous.deleted_at is not None:
                return previous
            _version(previous, expected_version)
            return previous


def _active(record: SessionRecord | None) -> SessionRecord:
    if record is None or record.deleted_at is not None:
        raise SessionNotFoundError("session does not exist")
    return record


def _version(record: SessionRecord, expected: int) -> None:
    if record.version != expected:
        raise SessionVersionConflict("session changed; reload before editing")


@asynccontextmanager
async def open_session_directory(settings: Settings) -> AsyncIterator[SessionDirectoryPort]:
    if settings.checkpointer_backend == "memory":
        yield InMemorySessionDirectory()
        return
    try:
        from psycopg_pool import AsyncConnectionPool
    except ImportError as error:
        raise RuntimeError("install the project 'postgres' extra to persist the session directory") from error
    async with AsyncConnectionPool(
        settings.postgres_checkpointer_uri, min_size=1, max_size=5,
        open=False, kwargs={"autocommit": True},
    ) as pool:
        directory = PostgresSessionDirectory(pool)
        if settings.postgres_checkpoint_setup:
            await directory.setup()
        yield directory

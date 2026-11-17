from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from flmh_agent.config import Settings


class AsyncPostgresReadOnlyRunner:
    def __init__(self, settings: Settings, pool: Any | None = None) -> None:
        self.settings = settings
        self._pool = pool

    async def open(self) -> None:
        if self._pool is not None:
            return
        if not self.settings.bi_database_uri:
            raise RuntimeError("BI_DATABASE_URI is not configured")
        try:
            import asyncpg
        except ImportError as error:
            raise RuntimeError("install the project 'postgres' extra to run BI SQL") from error
        self._pool = await asyncpg.create_pool(
            self.settings.bi_database_uri,
            min_size=1,
            max_size=5,
            command_timeout=min(self.settings.request_timeout_seconds, 120.0),
            server_settings={"default_transaction_read_only": "on"},
        )

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def fetch(self, query: str, *, max_rows: int, timeout_ms: int) -> Sequence[Mapping[str, Any]]:
        if self._pool is None:
            await self.open()
        async with self._pool.acquire() as connection:
            async with connection.transaction(readonly=True):
                await connection.fetchval("SELECT set_config('statement_timeout', $1, true)", f"{timeout_ms}ms")
                records = await connection.fetch(query)
                if len(records) > max_rows:
                    raise RuntimeError("SQL result exceeded the configured row limit")
                return [{key: _json_value(value) for key, value in dict(record).items()} for record in records]


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value

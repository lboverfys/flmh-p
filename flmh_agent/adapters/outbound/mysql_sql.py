from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import unquote, urlparse
from uuid import UUID

from flmh_agent.config import Settings


class AsyncMysqlReadOnlyRunner:
    """Async MySQL read-only BI runner with session transaction read-only and timeout enforcement."""

    def __init__(self, settings: Settings, pool: Any | None = None) -> None:
        self.settings = settings
        self._pool = pool
        self._pool_lock = asyncio.Lock()
        self._driver: str = "injected" if pool is not None else ""

    @staticmethod
    def parse_uri(uri: str) -> dict[str, Any]:
        parsed = urlparse(uri)
        scheme = (parsed.scheme or "").casefold()
        if not scheme.startswith("mysql"):
            raise ValueError("BI_DATABASE_URI must use a mysql:// scheme for MySQL dialect")
        database = parsed.path.lstrip("/").strip()
        if not parsed.hostname or not database:
            raise ValueError("MySQL BI_DATABASE_URI must include host and database name")
        return {
            "host": parsed.hostname,
            "port": parsed.port or 3306,
            "user": unquote(parsed.username or ""),
            "password": unquote(parsed.password or ""),
            "db": database,
            "charset": "utf8mb4",
        }

    async def open(self) -> None:
        async with self._pool_lock:
            if self._pool is not None:
                return
            if not self.settings.bi_database_uri:
                raise RuntimeError("BI_DATABASE_URI is not configured")
            params = self.parse_uri(self.settings.bi_database_uri)
            try:
                import aiomysql
                self._driver = "aiomysql"
                self._pool = await aiomysql.create_pool(
                    host=params["host"],
                    port=params["port"],
                    user=params["user"],
                    password=params["password"],
                    db=params["db"],
                    charset=params["charset"],
                    minsize=1,
                    maxsize=5,
                    autocommit=True,
                )
                return
            except ImportError:
                pass
            try:
                import pymysql  # noqa: F401
                self._driver = "pymysql"
                self._pool = params
            except ImportError as error:
                raise RuntimeError("install 'aiomysql' or 'pymysql' to execute MySQL BI SQL queries") from error

    async def close(self) -> None:
        async with self._pool_lock:
            if self._pool is None:
                return
            if self._driver == "pymysql":
                self._pool = None
                return
            close_fn = getattr(self._pool, "close", None)
            if callable(close_fn):
                res = close_fn()
                if asyncio.iscoroutine(res):
                    await res
            wait_closed = getattr(self._pool, "wait_closed", None)
            if callable(wait_closed):
                res = wait_closed()
                if asyncio.iscoroutine(res):
                    await res
            self._pool = None

    async def fetch(self, query: str, *, max_rows: int, timeout_ms: int) -> Sequence[Mapping[str, Any]]:
        if self._pool is None:
            await self.open()
        bounded_timeout = max(250, min(int(timeout_ms), 60000))
        if self._driver == "pymysql":
            return await asyncio.to_thread(self._fetch_pymysql_sync, query, max_rows, bounded_timeout)
        async with self._pool.acquire() as conn:
            async with self._cursor_ctx(conn) as cur:
                await cur.execute("SET SESSION TRANSACTION READ ONLY")
                await cur.execute(f"SET SESSION MAX_EXECUTION_TIME = {bounded_timeout}")
                await cur.execute(query)
                rows = await cur.fetchall()
                if len(rows) > max_rows:
                    raise RuntimeError("SQL result exceeded the configured row limit")
                columns = [col[0] for col in (cur.description or ())]
                return self._normalize_rows(rows, columns)

    def _cursor_ctx(self, conn: Any):
        try:
            import aiomysql
            return conn.cursor(aiomysql.DictCursor)
        except ImportError:
            return conn.cursor()

    def _fetch_pymysql_sync(self, query: str, max_rows: int, timeout_ms: int) -> list[dict[str, Any]]:
        import pymysql
        params = dict(self._pool)
        params["cursorclass"] = pymysql.cursors.DictCursor
        params["read_timeout"] = max(1, int((timeout_ms + 999) // 1000))
        conn = pymysql.connect(**params)
        try:
            with conn.cursor() as cur:
                cur.execute("SET SESSION TRANSACTION READ ONLY")
                cur.execute(f"SET SESSION MAX_EXECUTION_TIME = {timeout_ms}")
                cur.execute(query)
                rows = cur.fetchall()
                if len(rows) > max_rows:
                    raise RuntimeError("SQL result exceeded the configured row limit")
                return self._normalize_rows(rows, [col[0] for col in (cur.description or ())])
        finally:
            conn.close()

    @staticmethod
    def _normalize_rows(rows: Sequence[Any], columns: Sequence[str]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for row in rows:
            if isinstance(row, Mapping):
                normalized.append({key: _json_value(val) for key, val in row.items()})
            else:
                normalized.append({columns[idx]: _json_value(val) for idx, val in enumerate(row)})
        return normalized


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

from __future__ import annotations

import asyncio
import hashlib
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator
from weakref import WeakValueDictionary

from flmh_agent.config import Settings
from flmh_agent.resources import complete_cleanup


class InMemoryRunCoordinator:
    def __init__(self) -> None:
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    @asynccontextmanager
    async def acquire(self, run_id: str) -> AsyncIterator[None]:
        lock = self._locks.setdefault(run_id, asyncio.Lock())
        async with lock:
            yield


class PostgresRunCoordinator:
    def __init__(self, pool: Any) -> None:
        self.pool = pool

    @asynccontextmanager
    async def acquire(self, run_id: str) -> AsyncIterator[None]:
        lock_id = int.from_bytes(hashlib.sha256(run_id.encode("utf-8")).digest()[:8], signed=True)
        async with self.pool.connection() as connection:
            try:
                await connection.execute("SELECT pg_advisory_lock(%s)", (lock_id,))
            except BaseException as failure:
                await complete_cleanup(connection.close(), failure)
                raise
            original = None
            try:
                yield
            except BaseException as failure:
                original = failure
                raise
            finally:
                await complete_cleanup(self._release(connection, lock_id), original)

    async def _release(self, connection: Any, lock_id: int) -> None:
        try:
            await connection.execute("SELECT pg_advisory_unlock(%s)", (lock_id,))
        except BaseException as failure:
            await complete_cleanup(connection.close(), failure)
            raise


@asynccontextmanager
async def open_run_coordinator(settings: Settings) -> AsyncIterator[Any]:
    if settings.checkpointer_backend == "memory":
        yield InMemoryRunCoordinator()
        return
    try:
        from psycopg_pool import AsyncConnectionPool
    except ImportError as error:
        raise RuntimeError("install the project 'postgres' extra to coordinate persistent runs") from error
    async with AsyncConnectionPool(
        settings.postgres_checkpointer_uri,
        min_size=1,
        max_size=5,
        open=False,
        kwargs={"autocommit": True},
    ) as pool:
        yield PostgresRunCoordinator(pool)

import unittest
import asyncio
import sys
from contextlib import asynccontextmanager
from decimal import Decimal
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

from flmh_agent.adapters.outbound.postgres_sql import AsyncPostgresReadOnlyRunner
from flmh_agent.config import Settings


class ReadOnlyPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_open_creates_one_pool_and_close_releases_it(self):
        started = asyncio.Event()
        release = asyncio.Event()
        pools = []

        class Pool:
            async def close(self):
                self.closed = True

        async def create_pool(*args, **kwargs):
            started.set()
            await release.wait()
            pool = Pool()
            pool.closed = False
            pools.append(pool)
            return pool

        runner = AsyncPostgresReadOnlyRunner(Settings(bi_database_uri="postgresql://local/test"))
        with patch.dict(sys.modules, {"asyncpg": SimpleNamespace(create_pool=create_pool)}):
            first = asyncio.create_task(runner.open())
            await started.wait()
            second = asyncio.create_task(runner.open())
            release.set()
            await asyncio.gather(first, second)
        self.assertEqual(len(pools), 1)
        await runner.close()
        self.assertTrue(pools[0].closed)

    async def test_adapter_sets_readonly_transaction_timeout_and_serializes_decimal_values(self):
        calls = []

        class Connection:
            @asynccontextmanager
            async def transaction(self, *, readonly):
                calls.append(("transaction", readonly))
                yield

            async def fetchval(self, sql, value):
                calls.append(("timeout", sql, value))

            async def fetch(self, sql):
                calls.append(("fetch", sql))
                return [{"price": Decimal("1.23"), "stat_date": date(2026, 11, 17)}]

        class Pool:
            @asynccontextmanager
            async def acquire(self):
                yield Connection()

        runner = AsyncPostgresReadOnlyRunner(Settings(), pool=Pool())
        rows = await runner.fetch("SELECT price FROM market_products LIMIT 20", max_rows=20, timeout_ms=5000)
        self.assertEqual(calls[0], ("transaction", True))
        self.assertEqual(calls[1][0], "timeout")
        self.assertEqual(calls[1][2], "5000ms")
        self.assertEqual(calls[2][0], "fetch")
        self.assertEqual(rows, [{"price": "1.23", "stat_date": "2026-11-17"}])

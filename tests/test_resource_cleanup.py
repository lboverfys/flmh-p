import asyncio
import json
import unittest
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from flmh_agent.adapters.outbound.bge_http import ElasticsearchHybridStore
from flmh_agent.adapters.outbound.coordination import PostgresRunCoordinator
from flmh_agent.adapters.outbound.mcp_client import McpProductContentAdapter, REQUIRED_TOOLS
from flmh_agent.application.service import _LazyToolSession
from flmh_agent.config import Settings


class ResourceCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_lock_acquisition_closes_connection_before_pool_return(self):
        executing = asyncio.Event()
        connection = SimpleNamespace(closed=False)
        returned = []

        async def execute(*args):
            executing.set()
            await asyncio.Event().wait()

        async def close():
            connection.closed = True

        connection.execute, connection.close = execute, close

        class Pool:
            @asynccontextmanager
            async def connection(self):
                try:
                    yield connection
                finally:
                    returned.append(connection.closed)

        async def request():
            async with PostgresRunCoordinator(Pool()).acquire("run"):
                self.fail("cancelled acquisition must not enter application code")

        task = asyncio.create_task(request())
        await executing.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(returned, [True])

    async def test_unlock_failure_closes_connection_and_preserves_application_failure(self):
        connection = SimpleNamespace(execute=AsyncMock(side_effect=[None, RuntimeError("unlock failed")]), close=AsyncMock())

        class Pool:
            @asynccontextmanager
            async def connection(self):
                yield connection

        with self.assertRaises(BaseExceptionGroup) as caught:
            async with PostgresRunCoordinator(Pool()).acquire("run"):
                raise ValueError("business failed")
        connection.close.assert_awaited_once()
        self.assertEqual([str(error) for error in caught.exception.exceptions], ["business failed", "unlock failed"])

    async def test_repeated_cancellation_waits_for_postgres_unlock_before_returning_connection(self):
        acquired, unlocking, release = (asyncio.Event() for _ in range(3))
        unlocked = False
        returned = []

        async def execute(sql, params):
            nonlocal unlocked
            if "pg_advisory_unlock" in sql:
                unlocking.set()
                await release.wait()
                unlocked = True

        class Pool:
            @asynccontextmanager
            async def connection(self):
                try:
                    yield SimpleNamespace(execute=execute)
                finally:
                    returned.append(unlocked)

        async def request():
            async with PostgresRunCoordinator(Pool()).acquire("run"):
                acquired.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(request())
        await acquired.wait()
        task.cancel()
        await unlocking.wait()
        task.cancel()
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        self.assertEqual(returned, [True])

    async def test_mcp_handshake_cancel_closes_in_owner_task_without_waiting_for_handshake(self):
        opening, closed = asyncio.Event(), asyncio.Event()
        owners = []

        @asynccontextmanager
        async def session():
            owners.append(asyncio.current_task())
            try:
                opening.set()
                await asyncio.Event().wait()
                yield object()
            finally:
                owners.append(asyncio.current_task())
                closed.set()

        async def request():
            async with _LazyToolSession(session) as tools:
                await tools.search_products({})

        task = asyncio.create_task(request())
        await opening.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        self.assertTrue(closed.is_set())
        self.assertIs(owners[0], owners[1])

    async def test_repeated_request_cancel_cannot_interrupt_mcp_session_cleanup(self):
        opened, closing, release, closed = (asyncio.Event() for _ in range(4))

        @asynccontextmanager
        async def session():
            try:
                yield SimpleNamespace(search_products=AsyncMock(return_value={"products": []}))
            finally:
                closing.set()
                await release.wait()
                closed.set()

        async def request():
            async with _LazyToolSession(session) as tools:
                await tools.search_products({})
                opened.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(request())
        await opened.wait()
        task.cancel()
        await closing.wait()
        task.cancel()
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        self.assertTrue(closed.is_set())

    async def test_pit_cleanup_attempts_all_ids_and_preserves_original_error(self):
        closed = []

        async def handler(request):
            if request.method == "DELETE":
                identifier = json.loads(request.content)["id"]
                closed.append(identifier)
                return httpx.Response(503 if identifier == "pit-1" else 200, json={})
            return httpx.Response(200, json={"id": "pit-1"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            store = ElasticsearchHybridStore(Settings(elasticsearch_url="https://fixture", elasticsearch_index="knowledge"), client)
            with self.assertRaises(ExceptionGroup) as caught:
                async with store.snapshot() as snapshot:
                    snapshot._pit_ids.update({"pit-2", "pit-3"})
                    raise ValueError("query failed")
        self.assertEqual(closed, ["pit-1", "pit-2", "pit-3"])
        self.assertEqual(str(caught.exception.exceptions[0]), "query failed")
        self.assertIsInstance(caught.exception.exceptions[1], ExceptionGroup)

    async def test_mcp_transport_and_rpc_share_configured_timeout(self):
        recorded = {}

        @asynccontextmanager
        async def transport(url, **kwargs):
            recorded.update(kwargs)
            yield object(), object(), None

        @asynccontextmanager
        async def session(*streams, **kwargs):
            recorded.update(kwargs)
            yield SimpleNamespace(initialize=AsyncMock(), list_tools=AsyncMock(
                return_value=SimpleNamespace(tools=[SimpleNamespace(name=name) for name in REQUIRED_TOOLS])))

        with patch("flmh_agent.adapters.outbound.mcp_client.streamablehttp_client", transport), \
             patch("flmh_agent.adapters.outbound.mcp_client.ClientSession", session):
            async with McpProductContentAdapter(Settings(request_timeout_seconds=3)):
                pass
        self.assertEqual(recorded["timeout"], 3)
        self.assertEqual(recorded["sse_read_timeout"], 3)
        self.assertEqual(recorded["read_timeout_seconds"], timedelta(seconds=3))

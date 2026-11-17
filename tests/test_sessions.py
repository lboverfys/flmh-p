import asyncio
import hashlib
import json
import sys
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from langgraph.checkpoint.memory import InMemorySaver
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import ClientDisconnect
from starlette.testclient import TestClient

from flmh_agent.adapters.inbound.identity import IdentityMiddleware
from flmh_agent.adapters.inbound.sessions import _events, session_routes
from flmh_agent.adapters.outbound.coordination import InMemoryRunCoordinator, open_run_coordinator
from flmh_agent.adapters.outbound.sessions import InMemorySessionDirectory, PostgresSessionDirectory, open_session_directory
from flmh_agent.api import _json_body, create_app
from flmh_agent.application.service import AgentApplication, RunNotFoundError
from flmh_agent.config import Settings
from flmh_agent.domain.identity import AuthenticationError, IdentityUnavailableError, UserIdentity
from flmh_agent.domain.planning import RuleBasedPlanner
from flmh_agent.domain.sessions import SessionCursor, SessionNotFoundError, SessionVersionConflict


ALICE = UserIdentity("alice-session", "USER", "LOCAL_MOCK", "Bearer alice-session-unit-token")
BOB = UserIdentity("bob-session", "ADMIN", "LOCAL_MOCK", "Bearer bob-session-unit-token")
REQUEST = {"user_request": "找美国商品写口播，预算10美元"}


class Verifier:
    def __init__(self):
        self.calls = 0
        self.revoked = False
        self.unavailable = False
        self.changed = False

    async def authenticate(self, authorization):
        self.calls += 1
        if self.unavailable:
            raise IdentityUnavailableError("identity unavailable")
        if self.revoked:
            raise AuthenticationError("revoked")
        if self.changed:
            return BOB
        for identity in (ALICE, BOB):
            if identity.authorization == authorization:
                return identity
        raise AuthenticationError("missing identity")


class Tools:
    def __init__(self):
        self.calls = []
        self.status = "QUEUED"

    async def search_products(self, query):
        self.calls.append("search")
        return {"products": [{"id": "session-product", "title": "Session fixture", "region": "US"}]}

    async def create_quote(self, request):
        self.calls.append("quote")
        return {"quoteId": "session-quote", "amount": 1, "currency": "USD"}

    async def confirm_quote(self, quote_id):
        self.calls.append("confirm")
        return {"confirmed": True, "quote_id": quote_id}

    async def submit_task(self, request, idempotency_key):
        self.calls.append("submit")
        return {"taskId": "session-task", "status": "QUEUED"}

    async def get_task(self, task_id):
        self.calls.append("get")
        return {"taskId": task_id, "status": self.status}


def application():
    tools = Tools()

    @asynccontextmanager
    async def session():
        yield tools

    agent = AgentApplication(
        settings=Settings(), planner=RuleBasedPlanner(), checkpointer=InMemorySaver(),
        tool_session_factory=session, trace_factory=SimpleNamespace(create=lambda: None),
        run_coordinator=InMemoryRunCoordinator(), session_directory=InMemorySessionDirectory(),
    )
    return agent, tools


class SessionApplicationTests(unittest.IsolatedAsyncioTestCase):
    async def test_reserves_before_tools_and_directory_contains_no_credentials(self):
        agent, tools = application()
        original = tools.search_products

        async def search(query):
            reserved = await agent.session_directory.find(ALICE.user_id, "reserved")
            self.assertEqual(reserved.status, "starting")
            return await original(query)

        tools.search_products = search
        await agent.start("reserved", REQUEST, identity=ALICE)
        page = await agent.list_sessions(identity=ALICE)
        self.assertEqual(page["sessions"][0]["status"], "awaiting_confirmation")
        self.assertNotIn(ALICE.authorization, repr(agent.session_directory._rows))
        self.assertNotIn(ALICE.authorization, repr(agent.checkpointer.storage))

    async def test_deletion_preserves_checkpoint_and_blocks_every_run_entrypoint(self):
        agent, tools = application()
        await agent.start("deleted", REQUEST, identity=ALICE)
        current = await agent.get_session("deleted", identity=ALICE)
        checkpoint = repr(agent.checkpointer.storage)
        calls = list(tools.calls)
        deleted = await agent.delete_session("deleted", current["session"]["version"], identity=ALICE)
        self.assertTrue(deleted["deleted"])
        self.assertEqual(await agent.delete_session("deleted", 1, identity=ALICE), deleted)
        operations = [
            lambda: agent.start("deleted", REQUEST, identity=ALICE),
            lambda: agent.get("deleted", identity=ALICE),
            lambda: agent.observe("deleted", identity=ALICE),
            lambda: agent.confirm("deleted", {"confirmed": True, "quote_id": "session-quote"}, identity=ALICE),
            lambda: agent.retry("deleted", identity=ALICE),
            lambda: agent.revise("deleted", {"expected_revision": 0, "slot_updates": {"region": "CA"}}, identity=ALICE),
            lambda: agent.advance_media("deleted", identity=ALICE),
            lambda: agent.get_session("deleted", identity=ALICE),
            lambda: agent.rename_session("deleted", "new", deleted["version"], identity=ALICE),
        ]
        for operation in operations:
            with self.assertRaises(RunNotFoundError):
                await operation()
        self.assertEqual(checkpoint, repr(agent.checkpointer.storage))
        self.assertEqual(calls, tools.calls)
        self.assertEqual((await agent.list_sessions(identity=ALICE))["sessions"], [])

    async def test_owner_isolation_covers_metadata_and_same_run_id(self):
        agent, _ = application()
        await agent.start("shared", REQUEST, identity=ALICE)
        for operation in [lambda: agent.get_session("shared", identity=BOB),
                          lambda: agent.rename_session("shared", "stolen", 1, identity=BOB),
                          lambda: agent.delete_session("shared", 1, identity=BOB)]:
            with self.assertRaises(RunNotFoundError):
                await operation()
        self.assertEqual((await agent.list_sessions(identity=BOB))["sessions"], [])
        alice = await agent.get_session("shared", identity=ALICE)
        await agent.delete_session("shared", alice["session"]["version"], identity=ALICE)
        await agent.start("shared", REQUEST, identity=BOB)
        self.assertEqual(len((await agent.list_sessions(identity=BOB))["sessions"]), 1)

    async def test_rename_version_and_stable_reads_do_not_move_directory_order(self):
        agent, _ = application()
        await agent.start("rename", REQUEST, identity=ALICE)
        original = (await agent.get_session("rename", identity=ALICE))["session"]
        await agent.get("rename", identity=ALICE)
        await agent.start("rename", REQUEST, identity=ALICE)
        self.assertEqual((await agent.get_session("rename", identity=ALICE))["session"], original)
        renamed = await agent.rename_session("rename", "  我的  商品  ", original["version"], identity=ALICE)
        self.assertEqual(renamed["title"], "我的 商品")
        self.assertEqual(renamed["version"], original["version"] + 1)
        self.assertEqual(await agent.rename_session("rename", "我的 商品", renamed["version"], identity=ALICE), renamed)
        with self.assertRaises(SessionVersionConflict):
            await agent.rename_session("rename", "stale", original["version"], identity=ALICE)
        with self.assertRaises(SessionVersionConflict):
            await agent.delete_session("rename", original["version"], identity=ALICE)
        with self.assertRaises(ValueError):
            await agent.rename_session("rename", " ", renamed["version"], identity=ALICE)

    async def test_concurrent_changes_have_one_winner(self):
        agent, _ = application()
        await agent.start("race", REQUEST, identity=ALICE)
        version = (await agent.get_session("race", identity=ALICE))["session"]["version"]
        results = await asyncio.gather(
            agent.rename_session("race", "first", version, identity=ALICE),
            agent.rename_session("race", "second", version, identity=ALICE), return_exceptions=True,
        )
        self.assertEqual(sum(isinstance(value, dict) for value in results), 1)
        self.assertEqual(sum(isinstance(value, SessionVersionConflict) for value in results), 1)

    async def test_observe_and_session_detail_never_refresh_or_open_tools(self):
        agent, tools = application()
        await agent.start("pending", REQUEST, identity=ALICE)
        await agent.confirm("pending", {"confirmed": True, "quote_id": "session-quote"}, identity=ALICE)
        original = (await agent.get_session("pending", identity=ALICE))["session"]
        calls = list(tools.calls)
        tools.status = "SUCCEEDED"
        agent.tool_session_factory = lambda: self.fail("read-only session access must not open tools")
        observed = await agent.observe("pending", identity=ALICE)
        detail = await agent.get_session("pending", identity=ALICE)
        self.assertEqual(observed["status"], "processing")
        self.assertEqual(detail["run"]["taskStatus"]["status"], "QUEUED")
        self.assertEqual(detail["session"], original)
        self.assertEqual(tools.calls, calls)

    async def test_legacy_checkpoint_is_registered_only_after_owner_access(self):
        agent, _ = application()
        await agent.start("legacy", REQUEST, identity=ALICE)
        agent.session_directory = InMemorySessionDirectory()
        with self.assertRaises(RunNotFoundError):
            await agent.get_session("legacy", identity=BOB)
        self.assertEqual(agent.session_directory._rows, {})
        detail = await agent.get_session("legacy", identity=ALICE)
        self.assertEqual(detail["session"]["status"], "awaiting_confirmation")
        self.assertEqual(len(agent.session_directory._rows), 1)

    async def test_list_is_bounded_and_never_loads_checkpoints_per_row(self):
        agent, _ = application()
        for index in range(12):
            await agent.session_directory.reserve(ALICE.user_id, f"run-{index:02}", str(index))
        agent._graph = lambda tools: self.fail("listing must not load checkpoints")
        first = await agent.list_sessions(limit=5, identity=ALICE)
        second = await agent.list_sessions(limit=5, cursor=first["nextCursor"], identity=ALICE)
        third = await agent.list_sessions(limit=5, cursor=second["nextCursor"], identity=ALICE)
        rows = first["sessions"] + second["sessions"] + third["sessions"]
        self.assertEqual([len(first["sessions"]), len(second["sessions"]), len(third["sessions"])], [5, 5, 2])
        self.assertEqual(len({row["runId"] for row in rows}), 12)
        self.assertIsNone(third["nextCursor"])
        self.assertEqual((await agent.list_sessions(limit=5, cursor=first["nextCursor"], identity=BOB))["sessions"], [])

    async def test_failed_progress_is_catalogued_and_retry_does_not_resubmit(self):
        agent, tools = application()
        await agent.start("failed-read", REQUEST, identity=ALICE)
        get_task = tools.get_task
        tools.get_task = AsyncMock(side_effect=RuntimeError("status unavailable"))
        with self.assertRaisesRegex(RuntimeError, "status unavailable"):
            await agent.confirm("failed-read", {"confirmed": True, "quote_id": "session-quote"}, identity=ALICE)
        self.assertEqual((await agent.list_sessions(identity=ALICE))["sessions"][0]["status"], "retryable")
        tools.get_task = get_task
        await agent.retry("failed-read", identity=ALICE)
        self.assertEqual(tools.calls.count("submit"), 1)

    async def test_delete_waits_for_active_run_and_does_not_cancel_submitted_work(self):
        agent, tools = application()
        await agent.start("serial", REQUEST, identity=ALICE)
        entered = asyncio.Event()
        release = asyncio.Event()
        original = tools.submit_task

        async def delayed_submit(request, key):
            entered.set()
            await release.wait()
            return await original(request, key)

        tools.submit_task = delayed_submit
        version = (await agent.get_session("serial", identity=ALICE))["session"]["version"]
        running = asyncio.create_task(agent.confirm("serial", {"confirmed": True, "quote_id": "session-quote"}, identity=ALICE))
        await entered.wait()
        deleting = asyncio.create_task(agent.delete_session("serial", version, identity=ALICE))
        await asyncio.sleep(0)
        self.assertFalse(deleting.done())
        release.set()
        await running
        with self.assertRaises(SessionVersionConflict):
            await deleting
        current = (await agent.get_session("serial", identity=ALICE))["session"]
        await agent.delete_session("serial", current["version"], identity=ALICE)
        self.assertEqual(tools.calls.count("submit"), 1)
        self.assertFalse(any("cancel" in value for value in tools.calls))

    async def test_reserved_but_unstarted_session_can_be_deleted_and_not_reused(self):
        agent, tools = application()
        record = await agent.session_directory.reserve(ALICE.user_id, "reserved-only", "Interrupted before checkpoint")
        self.assertIsNone((await agent.get_session("reserved-only", identity=ALICE))["run"])
        await agent.delete_session("reserved-only", record.version, identity=ALICE)
        with self.assertRaises(RunNotFoundError):
            await agent.start("reserved-only", REQUEST, identity=ALICE)
        self.assertEqual(tools.calls, [])

    async def test_cursor_tie_breaker_and_malformed_cursor_validation(self):
        directory = InMemorySessionDirectory()
        now = datetime.now(timezone.utc)
        with patch("flmh_agent.adapters.outbound.sessions.datetime") as clock:
            clock.now.return_value = now
            for key in ["a", "c", "b"]:
                await directory.reserve(ALICE.user_id, key, key)
        first = await directory.list(ALICE.user_id, 2, None)
        self.assertEqual([row.run_id for row in first.sessions], ["c", "b"])
        cursor = SessionCursor.decode(first.next_cursor.encode())
        self.assertEqual([row.run_id for row in (await directory.list(ALICE.user_id, 2, cursor)).sessions], ["a"])
        for value in ["", "not-json", "a" * 513, "!bad", "e30", "W10", "WyIyMDI2LTAxLTAxIiwiciJd"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                SessionCursor.decode(value)


class SessionHttpTests(unittest.TestCase):
    def test_http_routes_enforce_owner_versions_tombstones_and_input_limits(self):
        agent, tools = application()
        with TestClient(create_app(Settings(), identity_verifier=Verifier())) as client:
            client.app.state.agent = agent
            headers = {"Authorization": ALICE.authorization}
            bob = {"Authorization": BOB.authorization}
            self.assertEqual(client.post("/v1/agent/runs", json={"run_id": "http", **REQUEST}, headers=headers).status_code, 202)
            listing = client.get("/v1/agent/sessions?limit=1", headers=headers)
            self.assertEqual(listing.status_code, 200)
            self.assertEqual(listing.headers["cache-control"], "no-store")
            version = listing.json()["sessions"][0]["version"]
            for path in ["/v1/agent/sessions/http", "/v1/agent/runs/http/events"]:
                self.assertEqual(client.get(path, headers=bob).status_code, 404)
            self.assertEqual(client.patch("/v1/agent/sessions/http", json={"title": "新标题", "expectedVersion": version}, headers=bob).status_code, 404)
            self.assertEqual(client.delete(f"/v1/agent/sessions/http?expectedVersion={version}", headers=bob).status_code, 404)
            renamed = client.patch("/v1/agent/sessions/http", json={"title": "新标题", "expectedVersion": version}, headers=headers)
            self.assertEqual(renamed.status_code, 200)
            self.assertEqual(client.delete(f"/v1/agent/sessions/http?expectedVersion={version}", headers=headers).status_code, 409)
            version = renamed.json()["version"]
            deleted = client.delete(f"/v1/agent/sessions/http?expectedVersion={version}", headers=headers)
            self.assertTrue(deleted.json()["deleted"])
            self.assertEqual(client.delete(f"/v1/agent/sessions/http?expectedVersion={version}", headers=headers).json(), deleted.json())
            self.assertEqual(client.get("/v1/agent/runs/http", headers=headers).status_code, 404)
            self.assertEqual(client.post("/v1/agent/runs", json={"run_id": "http", **REQUEST}, headers=headers).status_code, 404)
            self.assertEqual(client.get("/v1/agent/runs/http/events", headers=headers).status_code, 404)
            self.assertEqual(tools.calls, ["search", "quote"])
            for query in ["limit=51", "limit=0", "unknown=1"]:
                self.assertEqual(client.get("/v1/agent/sessions?" + query, headers=headers).status_code, 422)
            self.assertEqual(client.get("/v1/agent/sessions?limit=1&limit=2", headers=headers).status_code, 400)
            self.assertEqual(client.get("/v1/agent/sessions?cursor=bad", headers=headers).status_code, 400)
            self.assertEqual(client.patch("/v1/agent/sessions/http", json={"title": "new", "expectedVersion": True}, headers=headers).status_code, 422)


class RecordingConnection:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.commands = []

    async def execute(self, sql, parameters=None):
        self.commands.append((sql, parameters))
        value = self.responses.pop(0) if self.responses else None
        return SimpleNamespace(fetchone=AsyncMock(return_value=value), fetchall=AsyncMock(return_value=value or []))


class RecordingPool:
    def __init__(self, connection=None):
        self.value = connection or RecordingConnection()
        self.acquisitions = 0
        self.active = 0
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    @asynccontextmanager
    async def connection(self):
        self.acquisitions += 1
        self.active += 1
        try:
            yield self.value
        finally:
            self.active -= 1


def row(run_id="postgres-run", *, version=2, deleted=None):
    now = datetime(2026, 11, 17, tzinfo=timezone.utc)
    return (ALICE.user_id, run_id, "title", "preview", "processing", version, now, now, deleted)


class PostgresSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_uses_one_bounded_owner_keyset_query_without_checkpoint_reads(self):
        connection = RecordingConnection([[row("c"), row("b"), row("a")]])
        pool = RecordingPool(connection)
        directory = PostgresSessionDirectory(pool)
        cursor = SessionCursor(datetime(2026, 11, 18, tzinfo=timezone.utc), "last")
        result = await directory.list(ALICE.user_id, 2, cursor)
        self.assertEqual(len(result.sessions), 2)
        self.assertIsNotNone(result.next_cursor)
        self.assertEqual(pool.acquisitions, 1)
        self.assertEqual(len(connection.commands), 1)
        sql, parameters = connection.commands[0]
        self.assertIn("owner_id = %s AND deleted_at IS NULL", sql)
        self.assertIn("(updated_at, run_id) < (%s, %s)", sql)
        self.assertIn("ORDER BY updated_at DESC, run_id DESC LIMIT %s", sql)
        self.assertNotIn("SELECT *", sql)
        self.assertNotIn("OFFSET", sql)
        self.assertEqual(parameters, (ALICE.user_id, cursor.updated_at, cursor.run_id, 3))

    async def test_reservation_does_not_resurrect_deleted_rows_or_hold_nested_connections(self):
        connection = RecordingConnection([None, row(deleted=datetime.now(timezone.utc))])
        pool = RecordingPool(connection)
        with self.assertRaises(SessionNotFoundError):
            await PostgresSessionDirectory(pool).reserve(ALICE.user_id, "postgres-run", "new")
        self.assertEqual(pool.acquisitions, 1)
        self.assertEqual(pool.active, 0)
        self.assertIn("ON CONFLICT (owner_id, run_id) DO NOTHING", connection.commands[0][0])

    async def test_stable_synchronization_does_not_advance_version_or_timestamp(self):
        connection = RecordingConnection([None, row()])
        result = await PostgresSessionDirectory(RecordingPool(connection)).synchronize(ALICE.user_id, "postgres-run", "preview", "processing")
        self.assertEqual(result.version, 2)
        self.assertIn("preview IS DISTINCT FROM %s OR run_status IS DISTINCT FROM %s", connection.commands[0][0])
        self.assertIn("deleted_at IS NULL", connection.commands[0][0])

    async def test_rename_and_delete_enforce_cas_and_delete_is_idempotent(self):
        connection = RecordingConnection([None, row(version=3), None, row(deleted=datetime.now(timezone.utc))])
        directory = PostgresSessionDirectory(RecordingPool(connection))
        with self.assertRaises(SessionVersionConflict):
            await directory.rename(ALICE.user_id, "postgres-run", "changed", 2)
        deleted = await directory.delete(ALICE.user_id, "postgres-run", 1)
        self.assertIsNotNone(deleted.deleted_at)
        for sql, _ in (connection.commands[0], connection.commands[2]):
            self.assertIn("owner_id = %s AND run_id = %s AND version = %s AND deleted_at IS NULL", sql)

    async def test_schema_is_explicit_and_directory_pool_is_separate_from_advisory_locks(self):
        for setup in (False, True):
            created = []

            def factory(*args, **kwargs):
                pool = RecordingPool()
                created.append((pool, args, kwargs))
                return pool

            settings = Settings(checkpointer_backend="postgres", postgres_checkpointer_uri="postgresql://unit.invalid/never-connect",
                                postgres_checkpoint_setup=setup)
            with patch.dict(sys.modules, {"psycopg_pool": SimpleNamespace(AsyncConnectionPool=factory)}):
                async with open_run_coordinator(settings) as coordinator, open_session_directory(settings) as directory:
                    self.assertIsNot(coordinator.pool, directory.pool)
                    async with coordinator.acquire("test-run-lock"):
                        await directory.find(ALICE.user_id, "test-run")
                        self.assertEqual(coordinator.pool.active, 1)
                        self.assertEqual(directory.pool.active, 0)
            self.assertTrue(all(item[0].closed for item in created))
            self.assertEqual(created[1][2]["kwargs"], {"autocommit": True})
            ddl = [sql for sql, _ in created[1][0].value.commands if sql.lstrip().startswith("CREATE")]
            self.assertEqual(len(ddl), 2 if setup else 0)
            if setup:
                self.assertIn("(owner_id, updated_at DESC, run_id DESC)", ddl[1])
                self.assertIn("WHERE deleted_at IS NULL", ddl[1])


class SessionEventTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.agent, self.tools = application()
        self.verifier = Verifier()
        await self.agent.start("events", REQUEST, identity=ALICE)
        self.calls_before = list(self.tools.calls)
        self.request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(agent=self.agent, identity_verifier=self.verifier)),
            state=SimpleNamespace(identity=ALICE),
        )
        self.enterContext(patch("flmh_agent.adapters.inbound.sessions.SSE_POLL_SECONDS", 0.001))
        self.enterContext(patch("flmh_agent.adapters.inbound.sessions.SSE_HEARTBEAT_SECONDS", 0.002))
        self.enterContext(patch("flmh_agent.adapters.inbound.sessions.SSE_MAX_SECONDS", 2.0))

    async def test_stream_reports_changes_heartbeats_and_bounded_reconnect_without_submitting(self):
        stream = _events(self.request, "events")
        with patch("flmh_agent.adapters.inbound.sessions.SSE_MAX_SECONDS", 0.3):
            messages = [message async for message in stream]
        self.assertEqual(sum(message.startswith("event: status") for message in messages), 1)
        self.assertTrue(any(message.startswith("event: heartbeat") for message in messages))
        self.assertIn('"reason":"reconnect_required"', messages[-1])
        self.assertGreater(self.verifier.calls, 1)
        self.assertEqual(self.tools.calls, self.calls_before)
        self.assertNotIn(ALICE.authorization, "".join(messages))
        self.assertNotIn("products", "".join(messages))

    async def test_last_event_id_skips_unchanged_status_and_reconnect_is_read_only(self):
        current = await self.agent.observe("events", identity=ALICE)
        fingerprint = hashlib.sha256(json.dumps(current, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        with patch("flmh_agent.adapters.inbound.sessions.SSE_MAX_SECONDS", 0.3):
            messages = [message async for message in _events(self.request, "events", fingerprint)]
        self.assertFalse(any(message.startswith("event: status") for message in messages))
        self.assertEqual(self.tools.calls, self.calls_before)

    async def test_timeout_does_not_cancel_consumer_while_generator_is_suspended(self):
        stream = _events(self.request, "events")
        with patch("flmh_agent.adapters.inbound.sessions.SSE_MAX_SECONDS", 0.1):
            first = await anext(stream)
            self.assertTrue(first.startswith("event: status"))
            await asyncio.sleep(0.15)
            self.assertIn('"reason":"reconnect_required"', await anext(stream))
        await stream.aclose()

    async def test_revocation_identity_switch_and_unavailability_end_stream(self):
        for condition, reason in [("revoked", "unauthorized"), ("changed", "unauthorized"), ("unavailable", "identity_service_unavailable")]:
            self.verifier = Verifier()
            self.request.app.state.identity_verifier = self.verifier
            stream = _events(self.request, "events")
            self.assertTrue((await anext(stream)).startswith("event: status"))
            setattr(self.verifier, condition, True)
            self.assertIn(reason, await anext(stream))
            with self.assertRaises(StopAsyncIteration):
                await anext(stream)
        self.assertEqual(self.tools.calls, self.calls_before)

    async def test_soft_delete_ends_subscription_and_does_not_cancel_supplier(self):
        stream = _events(self.request, "events")
        await anext(stream)
        record = (await self.agent.get_session("events", identity=ALICE))["session"]
        await self.agent.delete_session("events", record["version"], identity=ALICE)
        self.assertIn('"reason":"session_not_found"', await anext(stream))
        await stream.aclose()
        self.assertEqual(self.tools.calls, self.calls_before)

    async def test_subscription_emits_changed_snapshot_then_returns_to_heartbeat(self):
        stream = _events(self.request, "events")
        first = await anext(stream)
        await self.agent.revise("events", {"expected_revision": 0, "slot_updates": {"region": "CA"}}, identity=ALICE)
        second = await anext(stream)
        self.assertTrue(second.startswith("event: status"))
        self.assertIn('"revision":1', second)
        self.assertNotEqual(first, second)
        await stream.aclose()
        self.assertNotIn("submit", self.tools.calls)

    async def test_asgi_disconnect_cancels_subscription_and_leaves_no_background_tasks(self):
        web = Starlette(routes=session_routes(_json_body), middleware=[Middleware(IdentityMiddleware, verifier=self.verifier)])
        web.state.agent = self.agent
        web.state.identity_verifier = self.verifier
        disconnect = asyncio.Event()
        messages = []

        async def receive():
            await disconnect.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            messages.append(message)
            if message["type"] == "http.response.body" and b"event: status" in message.get("body", b""):
                disconnect.set()

        before = asyncio.all_tasks()
        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "method": "GET",
                 "path": "/v1/agent/runs/events/events", "raw_path": b"/v1/agent/runs/events/events",
                 "query_string": b"", "headers": [(b"authorization", ALICE.authorization.encode())],
                 "http_version": "1.1", "scheme": "http", "server": ("unit", 80), "client": ("unit", 1)}
        await asyncio.wait_for(web(scope, receive, send), timeout=1)
        await asyncio.sleep(0)
        self.assertTrue(disconnect.is_set())
        self.assertEqual(asyncio.all_tasks(), before)
        self.assertEqual(self.tools.calls, self.calls_before)
        self.assertEqual((await self.agent.observe("events", identity=ALICE))["status"], "awaiting_confirmation")

    async def test_slow_client_send_is_bounded_and_closes_generator_for_both_asgi_versions(self):
        for spec_version in ("2.3", "2.4"):
            with self.subTest(spec_version=spec_version):
                web = Starlette(routes=session_routes(_json_body), middleware=[Middleware(IdentityMiddleware, verifier=self.verifier)])
                web.state.agent = self.agent
                web.state.identity_verifier = self.verifier
                blocked = asyncio.Event()
                send_started = asyncio.Event()
                send_cancelled = asyncio.Event()
                generator_closed = asyncio.Event()

                async def receive():
                    await blocked.wait()
                    return {"type": "http.disconnect"}

                async def send(message):
                    if message["type"] == "http.response.body":
                        send_started.set()
                        try:
                            await blocked.wait()
                        finally:
                            send_cancelled.set()

                async def tracked_events(*args):
                    try:
                        yield 'event: status\ndata: {"runId":"events","status":"processing"}\n\n'
                    finally:
                        generator_closed.set()

                before = asyncio.all_tasks()
                scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": spec_version}, "method": "GET",
                         "path": "/v1/agent/runs/events/events", "raw_path": b"/v1/agent/runs/events/events",
                         "query_string": b"", "headers": [(b"authorization", ALICE.authorization.encode())],
                         "http_version": "1.1", "scheme": "http", "server": ("unit", 80), "client": ("unit", 1)}
                with patch("flmh_agent.adapters.inbound.sessions.SSE_MAX_SECONDS", 0.03), \
                        patch("flmh_agent.adapters.inbound.sessions.SSE_SEND_GRACE_SECONDS", 0.02), \
                        patch("flmh_agent.adapters.inbound.sessions._events", tracked_events):
                    with self.assertRaises(ClientDisconnect):
                        await asyncio.wait_for(web(scope, receive, send), timeout=1)
                self.assertTrue(send_started.is_set())
                self.assertTrue(send_cancelled.is_set())
                self.assertTrue(generator_closed.is_set())
                await asyncio.sleep(0)
                self.assertEqual(asyncio.all_tasks(), before)
                self.assertEqual(self.tools.calls, self.calls_before)


if __name__ == "__main__":
    unittest.main()

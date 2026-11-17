import asyncio
import json
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from langgraph.checkpoint.memory import InMemorySaver
from starlette.testclient import TestClient

from flmh_agent.adapters.inbound.mcp_tools import create_mcp_server
from flmh_agent.adapters.outbound.coordination import InMemoryRunCoordinator
from flmh_agent.adapters.outbound.sessions import InMemorySessionDirectory
from flmh_agent.adapters.outbound.identity import JavaIdentityVerifier
from flmh_agent.adapters.outbound.java_gateway import JavaGatewayClient
from flmh_agent.adapters.outbound.model_gateway import ModelGatewayClient
from flmh_agent.api import create_app
from flmh_agent.application.service import AgentApplication, RunConflictError, RunNotFoundError
from flmh_agent.config import Settings
from flmh_agent.domain.identity import AuthenticationError, IdentityUnavailableError, UserIdentity, bind_identity, current_identity, run_resource_key
from flmh_agent.domain.planning import RuleBasedPlanner


ALICE = UserIdentity("alice", "USER", "LOCAL_MOCK", "Bearer alice-unit-token")
BOB = UserIdentity("bob", "ADMIN", "LOCAL_MOCK", "Bearer bob-unit-token")


class TwoUserVerifier:
    def __init__(self):
        self.disabled = set()
        self.calls = []

    async def authenticate(self, authorization):
        self.calls.append(authorization)
        user = {ALICE.authorization: ALICE, BOB.authorization: BOB}.get(authorization)
        if user is None or user.user_id in self.disabled:
            raise AuthenticationError("user is missing or disabled")
        return user


class UserTools:
    def __init__(self):
        self.calls = []

    async def search_products(self, query):
        user = current_identity()
        await asyncio.sleep(0)
        self.calls.append(("search", user.user_id))
        return {"products": [{"id": "product-" + user.user_id, "title": user.user_id, "region": "US"}]}

    async def create_quote(self, request):
        return {"quoteId": "quote-" + current_identity().user_id, "amount": 1, "currency": "USD"}

    async def confirm_quote(self, quote_id):
        self.calls.append(("confirm", current_identity().user_id))
        return {"quote_id": quote_id, "confirmed": True}

    async def submit_task(self, request, idempotency_key):
        self.calls.append(("submit", current_identity().user_id))
        return {"taskId": "task-" + current_identity().user_id, "status": "QUEUED"}

    async def get_task(self, task_id):
        return {"taskId": task_id, "status": "SUCCEEDED"}


def application():
    tools = UserTools()

    @asynccontextmanager
    async def session():
        yield tools

    app = AgentApplication(settings=Settings(), planner=RuleBasedPlanner(), checkpointer=InMemorySaver(),
                           tool_session_factory=session, trace_factory=SimpleNamespace(create=lambda: None),
                           run_coordinator=InMemoryRunCoordinator(), session_directory=InMemorySessionDirectory())
    return app, tools


class IdentityVerifierTests(unittest.IsolatedAsyncioTestCase):
    async def test_verifier_forwards_original_token_and_uses_current_database_role(self):
        seen = []

        async def handler(request):
            seen.append(request)
            return httpx.Response(200, json={"id": "database-user", "role": "USER", "status": "ACTIVE", "source": "LEGACY_JDBC"})

        async with httpx.AsyncClient(base_url="http://identity", transport=httpx.MockTransport(handler)) as client:
            verifier = JavaIdentityVerifier(Settings(gateway_api_key="service-only"), client)
            user = await verifier.authenticate("Bearer jwt-with-stale-admin-claims")
        self.assertEqual(user.role, "USER")
        self.assertEqual(user.user_id, "database-user")
        self.assertEqual(seen[0].url.path, "/api/v1/identity/me")
        self.assertEqual(seen[0].headers["authorization"], "Bearer jwt-with-stale-admin-claims")
        self.assertEqual(seen[0].headers["x-service-key"], "service-only")
        self.assertNotIn("jwt-with-stale", repr(user))

    async def test_missing_disabled_deleted_or_invalid_identity_is_rejected(self):
        cases = [(401, {}), (403, {}), (200, {"id": "a", "role": "USER", "status": "DISABLED", "source": "LEGACY_JDBC"}),
                 (200, {"role": "ADMIN", "status": "ACTIVE", "source": "LEGACY_JDBC"})]
        for status, body in cases:
            with self.subTest(status=status, body=body):
                async with httpx.AsyncClient(base_url="http://identity", transport=httpx.MockTransport(lambda _: httpx.Response(status, json=body))) as client:
                    with self.assertRaises(AuthenticationError):
                        await JavaIdentityVerifier(Settings(), client).authenticate("Bearer user-token")
        for header in ("", "service-key", "Bearer ", "Bearer user other", "Bearer bad\nvalue"):
            with self.subTest(header=header), self.assertRaises(AuthenticationError):
                await JavaIdentityVerifier(Settings()).authenticate(header)

    async def test_unavailable_identity_does_not_fall_back_and_production_rejects_mock(self):
        settings = Settings(environment="production", agent_api_key="service", planner_backend="model_gateway",
                            checkpointer_backend="postgres", postgres_checkpointer_uri="postgresql://localhost/test")
        async with httpx.AsyncClient(base_url="http://identity", transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json={"id": "alice", "role": "USER", "status": "ACTIVE", "source": "LOCAL_MOCK"}))) as client:
            with self.assertRaises(AuthenticationError):
                await JavaIdentityVerifier(settings, client).authenticate(ALICE.authorization)
        async with httpx.AsyncClient(base_url="http://identity", transport=httpx.MockTransport(lambda _: httpx.Response(503))) as client:
            with self.assertRaises(IdentityUnavailableError):
                await JavaIdentityVerifier(Settings(), client).authenticate(ALICE.authorization)


class OwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_bi_role_comes_from_verified_user_and_cannot_be_supplied_in_slots(self):
        app, _ = application()
        roles = []

        class BiQuery:
            async def query(self, question, principal):
                roles.append(principal)
                return {"source": "UNIT_BI", "rows": []}

        app.bi_query = BiQuery()
        request = {"user_request": "统计美国最近销量和 GMV 增长排行"}
        await app.start("bi-shared", request, identity=ALICE)
        await app.start("bi-shared", request, identity=BOB)
        self.assertEqual(roles, ["USER", "ADMIN"])
        with self.assertRaises(ValueError):
            await app.start("forged-role", {**request, "slot_updates": {"role": "SUPER_ADMIN"}}, identity=ALICE)

    async def test_same_run_id_is_isolated_and_replay_remains_idempotent_for_each_owner(self):
        app, tools = application()
        request = {"user_request": "找美国商品写口播，预算10美元"}
        alice, bob = await asyncio.gather(app.start("shared", request, identity=ALICE), app.start("shared", request, identity=BOB))
        self.assertEqual(alice["confirmationRequest"]["quoteId"], "quote-alice")
        self.assertEqual(bob["confirmationRequest"]["quoteId"], "quote-bob")
        self.assertEqual(await app.start("shared", request, identity=ALICE), alice)
        self.assertEqual(await app.get("shared", identity=BOB), bob)
        self.assertCountEqual(tools.calls, [("search", "alice"), ("search", "bob")])
        with self.assertRaises(RunConflictError):
            await app.confirm("shared", {"quote_id": "quote-alice", "confirmed": True}, identity=BOB)
        await app.confirm("shared", {"quote_id": "quote-alice", "confirmed": True}, identity=ALICE)
        self.assertEqual((await app.get("shared", identity=BOB))["status"], "awaiting_confirmation")
        self.assertIn(("submit", "alice"), tools.calls)
        self.assertNotIn(("submit", "bob"), tools.calls)
        self.assertNotEqual(run_resource_key(ALICE, "shared"), run_resource_key(BOB, "shared"))
        self.assertEqual(set(app.checkpointer.storage), {run_resource_key(ALICE, "shared"), run_resource_key(BOB, "shared")})
        serialized = repr(app.checkpointer.storage)
        self.assertNotIn("alice-unit-token", serialized)
        self.assertNotIn("bob-unit-token", serialized)
        with self.assertRaises(AuthenticationError):
            current_identity()

    async def test_foreign_run_cannot_be_read_confirmed_revised_retried_or_advanced(self):
        app, _ = application()
        await app.start("alice-only", {"user_request": "找美国商品写口播，预算10美元"}, identity=ALICE)
        self.assertIsNone(await app.get("alice-only", identity=BOB))
        actions = [lambda: app.confirm("alice-only", {"confirmed": True, "quote_id": "quote-alice"}, identity=BOB),
                   lambda: app.revise("alice-only", {"expected_revision": 0, "slot_updates": {"region": "CA"}}, identity=BOB),
                   lambda: app.retry("alice-only", identity=BOB), lambda: app.advance_media("alice-only", identity=BOB)]
        for action in actions:
            with self.assertRaises(RunNotFoundError):
                await action()
        with self.assertRaises(AuthenticationError):
            await app.get("alice-only")

    async def test_gateway_and_model_headers_are_request_scoped_under_concurrency(self):
        seen = []

        async def handler(request):
            await asyncio.sleep(0)
            seen.append((request.url.path, request.headers["authorization"], request.headers.get("x-client-id"), json.loads(request.content) if request.content else {}))
            return httpx.Response(200, json={"content": "ok", "products": []})

        async with httpx.AsyncClient(base_url="http://services", transport=httpx.MockTransport(handler)) as client:
            gateway = JavaGatewayClient(Settings(gateway_api_key="service"), client)
            model = ModelGatewayClient(Settings(model_gateway_api_key="service"), client)

            async def calls(identity):
                with bind_identity(identity):
                    await gateway.search_products({"region": "US", "owner": "forged"})
                    await model.complete(purpose="agent_plan", messages=[{"role": "user", "content": "plan"}])

            await asyncio.gather(calls(ALICE), calls(BOB))
        self.assertCountEqual([header for _, header, _, _ in seen], [ALICE.authorization, ALICE.authorization, BOB.authorization, BOB.authorization])
        self.assertTrue(all(client == "flmh-agent" for _, _, client, _ in seen))
        self.assertTrue(all("owner" not in body and "authorization" not in body for _, _, _, body in seen))


class IdentityHttpTests(unittest.TestCase):
    def test_authentication_service_failure_is_fail_closed_with_503(self):
        class Unavailable:
            async def authenticate(self, authorization):
                raise IdentityUnavailableError("unavailable")

        with TestClient(create_app(Settings(), identity_verifier=Unavailable())) as client:
            response = client.get("/v1/agent/runs/missing", headers={"Authorization": ALICE.authorization})
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json(), {"error": "identity_service_unavailable"})
            self.assertEqual(client.get("/health/live").status_code, 200)

    def test_api_revalidates_revocation_and_never_treats_service_key_as_user(self):
        verifier = TwoUserVerifier()
        app, _ = application()
        with TestClient(create_app(Settings(agent_api_key="service-secret"), identity_verifier=verifier)) as client:
            client.app.state.agent = app
            request = {"run_id": "owned", "user_request": "找美国商品写口播，预算10美元"}
            alice = {"Authorization": ALICE.authorization, "X-Agent-Key": "service-secret"}
            bob = {"Authorization": BOB.authorization, "X-Agent-Key": "service-secret"}
            self.assertEqual(client.post("/v1/agent/runs", json=request, headers=alice).status_code, 202)
            forged_headers = {**alice, "X-User-Id": "bob", "X-Owner-Id": "bob", "X-Client-Id": "bob"}
            owned = client.get("/v1/agent/runs/owned", headers=forged_headers)
            self.assertEqual(owned.status_code, 200)
            self.assertEqual(owned.json()["confirmationRequest"]["quoteId"], "quote-alice")
            self.assertEqual(client.get("/v1/agent/runs/owned", headers=bob).status_code, 404)
            self.assertEqual(client.post("/v1/agent/runs/owned/retry", headers=bob).status_code, 404)
            self.assertEqual(client.post("/v1/agent/runs", json={**request, "owner": "bob"}, headers=alice).status_code, 422)
            self.assertEqual(client.get("/v1/agent/runs/owned", headers={"Authorization": "Bearer service-secret", "X-Agent-Key": "service-secret"}).status_code, 401)
            self.assertEqual(client.get("/v1/agent/runs/owned", headers={"X-Agent-Key": "service-secret"}).status_code, 401)
            duplicate = [("Authorization", ALICE.authorization), ("authorization", BOB.authorization), ("X-Agent-Key", "service-secret")]
            self.assertEqual(client.get("/v1/agent/runs/owned", headers=duplicate).status_code, 401)
            verifier.disabled.add("alice")
            self.assertEqual(client.get("/v1/agent/runs/owned", headers=alice).status_code, 401)
            self.assertEqual(client.post("/v1/agent/runs/owned/confirmation", json={"quote_id": "quote-alice", "confirmed": True}, headers=alice).status_code, 401)

    def test_mcp_http_context_is_verified_forwarded_and_not_overridden_by_arguments(self):
        verifier = TwoUserVerifier()
        seen = []

        async def search(_gateway, query):
            seen.append(current_identity())
            return {"products": [], "identitySource": current_identity().source}

        server = create_mcp_server(Settings(), identity_verifier=verifier)
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "product_search", "arguments": {"region": "US"}}}
        headers = {"Authorization": ALICE.authorization, "Accept": "application/json, text/event-stream"}
        with patch("flmh_agent.adapters.inbound.mcp_tools.JavaGatewayClient.search_products", search), TestClient(server.streamable_http_app(), base_url="http://127.0.0.1:8001") as client:
            self.assertEqual(client.post("/mcp", json=body).status_code, 401)
            response = client.post("/mcp", json=body, headers=headers)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertFalse(response.json().get("result", {}).get("isError"), response.text)
            self.assertEqual(seen, [ALICE])
            forged = {**body, "params": {"name": "product_search", "arguments": {"region": "US", "owner": "bob"}}}
            response = client.post("/mcp", json=forged, headers=headers)
            self.assertTrue(response.json().get("result", {}).get("isError"), response.text)
            self.assertEqual(seen, [ALICE])
            verifier.disabled.add("alice")
            self.assertEqual(client.post("/mcp", json=body, headers=headers).status_code, 401)

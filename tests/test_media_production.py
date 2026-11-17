import json
import unittest
from identity_support import IdentityTestCase, FixtureIdentityVerifier
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from langgraph.checkpoint.memory import InMemorySaver
from starlette.testclient import TestClient

from flmh_agent.adapters.inbound.mcp_tools import create_mcp_server
from flmh_agent.adapters.outbound.coordination import InMemoryRunCoordinator
from flmh_agent.adapters.outbound.java_gateway import JavaGatewayClient
from flmh_agent.adapters.outbound.mcp_client import McpProductContentAdapter
from flmh_agent.api import create_app
from flmh_agent.application.service import AgentApplication, RunConflictError
from flmh_agent.config import Settings
from flmh_agent.domain.media_production import media_status
from flmh_agent.domain.planning import RuleBasedPlanner


class MediaTools:
    def __init__(self):
        self.calls = []
        self.state = "QUEUED"
        self.quote_request = None
        self.key = None

    def run(self):
        return {"runId": "media-1", "status": self.state, "executionMode": "MOCK_ONLY",
                "artifacts": [], "nodes": [{"nodeKey": name, "status": "MOCKED" if self.state == "MOCKED" else "PENDING",
                                           "outputUrl": None} for name in ("image", "video")]}

    async def search_products(self, query):
        self.calls.append("search")
        return {"products": [{"id": "p-1", "title": "杯子", "region": "US", "source": "LOCAL_FIXTURE"}]}

    async def create_media_quote(self, request):
        self.calls.append("quote")
        self.quote_request = request
        return {"quoteId": "q-media", "amount": 0, "currency": "USD", "executionMode": "MOCK_ONLY"}

    async def confirm_quote(self, quote_id):
        self.calls.append("confirm")
        return {"quoteId": quote_id, "confirmed": True}

    async def submit_media_run(self, quote_id, idempotency_key):
        self.calls.append("submit")
        self.key = idempotency_key
        return self.run()

    async def advance_media_run(self, run_id):
        self.calls.append("advance")
        self.state = "MOCKED" if self.state == "RUNNING" else "RUNNING"
        return self.run()

    async def get_media_run(self, run_id):
        self.calls.append("get")
        return self.run()

    async def verify_media_run(self, run_id):
        self.calls.append("verify")
        return self.run()


class MediaProductionTests(IdentityTestCase):
    def setUp(self):
        self.tools = MediaTools()

        @asynccontextmanager
        async def session():
            yield self.tools

        self.application = AgentApplication(
            settings=Settings(), planner=RuleBasedPlanner(), checkpointer=InMemorySaver(),
            tool_session_factory=session, trace_factory=SimpleNamespace(create=lambda: None),
            run_coordinator=InMemoryRunCoordinator(),
        )
        self.request = {"user_request": "找一款美国杯子，制作商品图片和视频，预算10"}

    async def test_media_plan_keeps_script_requests_separate_and_extracts_product(self):
        planner = RuleBasedPlanner()
        media = await planner.plan(self.request["user_request"], {})
        self.assertEqual(media.content_type, "product_media")
        self.assertEqual(media.queries[0].keyword, "杯子")
        self.assertEqual(media.budget_max, 10)
        self.assertIsNone(media.queries[0].max_price)
        script = await planner.plan("为美国杯子写视频脚本，预算10元", {})
        self.assertEqual(script.content_type, "product_script")

    async def test_product_price_and_production_budget_are_independent(self):
        planner = RuleBasedPlanner()
        plan = await planner.plan("查美国旅行杯，商品价格不超过100元，制作图片和视频，预算10美元", {})
        self.assertEqual(plan.budget_max, 10)
        self.assertEqual(plan.budget_currency, "USD")
        self.assertEqual(plan.queries[0].max_price, 100)
        self.assertEqual(plan.queries[0].keyword, "旅行杯")
        missing = await planner.plan("查美国旅行杯，商品价格不超过100，制作图片和视频", {})
        self.assertIsNone(missing.budget_max)
        self.assertIsNone(missing.budget_currency)
        self.assertEqual(missing.queries[0].max_price, 100)

    async def test_zero_cost_quote_still_interrupts_and_binds_visible_specification(self):
        result = await self.application.start("media", self.request)
        self.assertEqual(result["status"], "awaiting_confirmation")
        self.assertEqual(self.tools.calls, ["search", "quote"])
        self.assertEqual(result["confirmationRequest"]["mediaRequest"]["aspect_ratio"], "9:16")
        self.assertEqual(result["confirmationRequest"]["mediaRequest"]["duration_seconds"], 6)
        self.assertEqual(result["quote"]["amount"], 0)
        self.assertEqual(await self.application.start("media", self.request), result)
        with self.assertRaises(RunConflictError):
            await self.application.confirm("media", {"confirmed": True, "quote_id": "stale"})
        self.assertNotIn("submit", self.tools.calls)

    async def test_decline_and_missing_budget_never_submit(self):
        await self.application.start("declined", self.request)
        result = await self.application.confirm("declined", {"confirmed": False, "quote_id": "q-media"})
        self.assertIn("拒绝", result["summary"])
        self.assertNotIn("submit", self.tools.calls)
        self.tools.calls.clear()
        missing = await self.application.start("missing", {"user_request": "制作美国杯子视频"})
        self.assertEqual(missing["status"], "needs_input")
        self.assertEqual(self.tools.calls, ["search"])

    async def test_explicit_currency_mismatch_cannot_be_silently_reinterpreted(self):
        result = await self.application.start("currency", {"user_request": "美国杯子制作视频，预算10元"})
        self.assertIn("CNY", result["error"])
        self.assertIn("USD", result["error"])
        self.assertFalse(result["confirmationRequest"])
        self.assertNotIn("submit", self.tools.calls)
        self.assertNotIn("confirm", self.tools.calls)

    async def test_long_media_brief_returns_revisable_error_without_quote(self):
        result = await self.application.start("long", {"user_request": "美国杯子制作视频，预算10。" + "要求" * 1100})
        self.assertIn("超过2000", result["error"])
        self.assertEqual(result["quote"], {})
        self.assertNotIn("quote", self.tools.calls)
        fixed = await self.application.revise("long", {"expected_revision": 0, "user_request": "美国杯子制作视频，预算10"})
        self.assertEqual(fixed["status"], "awaiting_confirmation")

    async def test_confirm_is_idempotent_get_is_read_only_and_mock_terminal_has_no_assets(self):
        await self.application.start("run", self.request)
        first = await self.application.confirm("run", {"confirmed": True, "quote_id": "q-media"})
        self.assertEqual(first["status"], "processing")
        self.assertEqual(self.tools.calls.count("advance"), 0)
        await self.application.confirm("run", {"confirmed": True, "quote_id": "q-media"})
        await self.application.get("run")
        await self.application.retry("run")
        self.assertEqual(self.tools.calls.count("submit"), 1)
        self.assertEqual(self.tools.calls.count("advance"), 0)
        pending = await self.application.advance_media("run")
        self.assertEqual(pending["mediaStatus"]["status"], "RUNNING")
        result = await self.application.advance_media("run")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["mediaStatus"]["status"], "MOCKED")
        self.assertEqual(result["mediaStatus"]["artifacts"], [])
        self.assertIn("未生成文件或 URL", result["summary"])
        await self.application.advance_media("run")
        self.assertEqual(self.tools.calls.count("advance"), 2)
        self.assertTrue(self.tools.key)

    async def test_unknown_submission_blocks_advance_and_retry_only_reads(self):
        await self.application.start("run", self.request)
        await self.application.confirm("run", {"confirmed": True, "quote_id": "q-media"})
        self.tools.state = "SUBMISSION_UNKNOWN"
        result = await self.application.get("run")
        self.assertEqual(result["status"], "needs_verification")
        self.assertIn("未重新提交", result["summary"])
        with self.assertRaisesRegex(RunConflictError, "verify"):
            await self.application.advance_media("run")
        await self.application.retry("run")
        verified = await self.application.advance_media("run", verify=True)
        self.assertEqual(verified["status"], "needs_verification")
        self.assertEqual(self.tools.calls.count("submit"), 1)
        self.assertEqual(self.tools.calls.count("advance"), 0)
        self.assertEqual(self.tools.calls.count("verify"), 1)

    async def test_lost_advance_response_recovers_by_read_without_resubmitting(self):
        await self.application.start("lost", self.request)
        await self.application.confirm("lost", {"confirmed": True, "quote_id": "q-media"})
        async def lost_response(run_id):
            self.tools.calls.append("advance")
            self.tools.state = "SUBMISSION_UNKNOWN"
            raise TimeoutError("response lost")
        self.tools.advance_media_run = lost_response
        with self.assertRaises(TimeoutError):
            await self.application.advance_media("lost")
        recovered = await self.application.get("lost")
        self.assertEqual(recovered["status"], "needs_verification")
        await self.application.advance_media("lost", verify=True)
        self.assertEqual(self.tools.calls.count("advance"), 1)
        self.assertEqual(self.tools.calls.count("submit"), 1)

    async def test_failed_initial_status_read_recovers_with_get_only(self):
        await self.application.start("lost-get", self.request)
        original = self.tools.get_media_run
        self.tools.get_media_run = AsyncMock(side_effect=TimeoutError("status unavailable"))
        with self.assertRaises(TimeoutError):
            await self.application.confirm("lost-get", {"confirmed": True, "quote_id": "q-media"})
        self.tools.get_media_run = original
        recovered = await self.application.get("lost-get")
        self.assertEqual(recovered["mediaStatus"]["status"], "QUEUED")
        self.assertEqual(recovered["status"], "processing")
        self.assertEqual(self.tools.calls.count("submit"), 1)
        self.assertEqual(self.tools.calls.count("advance"), 0)

    async def test_media_revision_invalidates_quote_before_confirmation_only(self):
        first = await self.application.start("revision", self.request)
        revised = await self.application.revise("revision", {"expected_revision": 0, "slot_updates": {"budget_max": 12}})
        self.assertIn("media_request", revised["invalidated"])
        self.assertEqual(revised["mediaRequest"]["budget_max"], 12)
        self.assertEqual(first["mediaRun"], {})
        await self.application.confirm("revision", {"confirmed": True, "quote_id": "q-media"})
        with self.assertRaisesRegex(RunConflictError, "cannot be rewritten"):
            await self.application.revise("revision", {"expected_revision": 1, "slot_updates": {"budget_max": 13}})

    async def test_gateway_media_contract_uses_encoded_ids_and_idempotency_header(self):
        seen = []

        async def handler(request):
            seen.append(request)
            return httpx.Response(200, json={"runId": "r/1", "quoteId": "q-1"})

        async with httpx.AsyncClient(base_url="http://gateway", transport=httpx.MockTransport(handler)) as client:
            gateway = JavaGatewayClient(Settings(), client)
            await gateway.create_media_quote({"product_id": "p-1", "image_prompt": "image", "video_prompt": "video",
                "aspect_ratio": "9:16", "duration_seconds": 6, "budget_max": 10})
            await gateway.submit_media_run("q-1", "stable-key")
            await gateway.get_media_run("r/1")
            await gateway.advance_media_run("r/1")
            await gateway.verify_media_run("r/1")
            await gateway.revise_media_run("r/1", "q-revision", "revision-key")
        self.assertEqual(json.loads(seen[0].content), {"productId": "p-1", "imagePrompt": "image", "videoPrompt": "video",
                         "aspectRatio": "9:16", "durationSeconds": 6, "budgetMax": 10})
        self.assertEqual(seen[1].headers["Idempotency-Key"], "stable-key")
        self.assertEqual([r.url.raw_path for r in seen[2:5]], [b"/api/v1/content/media-productions/runs/r%2F1",
                         b"/api/v1/content/media-productions/runs/r%2F1/advance", b"/api/v1/content/media-productions/runs/r%2F1/verify"])
        self.assertEqual([r.method for r in seen[2:5]], ["GET", "POST", "POST"])
        self.assertEqual(seen[5].url.raw_path, b"/api/v1/content/media-productions/runs/r%2F1/revisions")
        self.assertEqual(json.loads(seen[5].content), {"quoteId": "q-revision"})
        self.assertEqual(seen[5].headers["Idempotency-Key"], "revision-key")

    async def test_mcp_media_tool_validation_and_client_mapping(self):
        with patch("flmh_agent.adapters.inbound.mcp_tools.JavaGatewayClient.create_media_quote", new_callable=AsyncMock) as quote:
            quote.return_value = {"quoteId": "q-1"}
            server = create_mcp_server(Settings())
            request = {"product_id": "p-1", "image_prompt": "image", "video_prompt": "video", "aspect_ratio": "9:16",
                       "duration_seconds": 6, "budget_max": 1}
            await server.call_tool("media_production_quote", request)
            self.assertEqual(quote.await_count, 1)
            for invalid in ({"duration_seconds": True}, {"duration_seconds": 16}, {"aspect_ratio": "unknown"}):
                with self.subTest(invalid=invalid), self.assertRaises(Exception):
                    await server.call_tool("media_production_quote", {**request, **invalid})
            self.assertEqual(quote.await_count, 1)
        adapter = McpProductContentAdapter(Settings())
        adapter._session = SimpleNamespace(call_tool=AsyncMock(return_value=SimpleNamespace(
            isError=False, structuredContent={"runId": "m-1"})))
        await adapter.submit_media_run("q-1", "k-1")
        adapter._session.call_tool.assert_awaited_once_with("media_production_submit", {"quote_id": "q-1", "idempotency_key": "k-1"})

    async def test_shared_quote_confirmation_requires_literal_boolean(self):
        with patch("flmh_agent.adapters.inbound.mcp_tools.JavaGatewayClient.confirm_quote", new_callable=AsyncMock) as confirm:
            confirm.return_value = {"confirmed": True}
            server = create_mcp_server(Settings())
            for value in ("true", 1, False):
                with self.subTest(value=value), self.assertRaises(Exception):
                    await server.call_tool("content_quote_confirm", {"quote_id": "q", "confirmed": value})
            confirm.assert_not_awaited()
            await server.call_tool("content_quote_confirm", {"quote_id": "q", "confirmed": True})
            confirm.assert_awaited_once_with("q")

    async def test_media_submit_limits_match_java_before_network(self):
        with patch("flmh_agent.adapters.inbound.mcp_tools.JavaGatewayClient.submit_media_run", new_callable=AsyncMock) as submit:
            submit.return_value = {"runId": "r"}
            server = create_mcp_server(Settings())
            for invalid in ({"quote_id": "q" * 97}, {"idempotency_key": "k" * 141},
                            {"idempotency_key": " key"}, {"idempotency_key": " "}, {"quote_id": " "}):
                with self.subTest(invalid=invalid), self.assertRaises(Exception):
                    await server.call_tool("media_production_submit", {"quote_id": "q", "idempotency_key": "k", **invalid})
            submit.assert_not_awaited()
            await server.call_tool("media_production_submit", {"quote_id": "q" * 96, "idempotency_key": "k" * 140})
            self.assertEqual(submit.await_count, 1)

    async def test_media_revision_tool_passes_only_confirmed_quote_reference(self):
        with patch("flmh_agent.adapters.inbound.mcp_tools.JavaGatewayClient.revise_media_run", new_callable=AsyncMock) as revise:
            revise.return_value = {"runId": "new-run"}
            await create_mcp_server(Settings()).call_tool("media_production_revise", {
                "run_id": "old-run", "quote_id": "confirmed-quote", "idempotency_key": "revision-key"})
            revise.assert_awaited_once_with("old-run", "confirmed-quote", "revision-key")

    def test_mock_output_and_unknown_status_cannot_be_reported_as_completion(self):
        base = {"runId": "r", "status": "MOCKED", "executionMode": "MOCK_ONLY", "artifacts": [], "nodes": []}
        for changed in ({"runId": "other"}, {"status": "NOT_A_STATE"}, {"executionMode": "HTTP"},
                        {"artifacts": ["fake"]}, {"nodes": [{"outputUrl": "https://fake.invalid"}]}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                media_status({**base, **changed}, "r")


class MediaApiTests(unittest.TestCase):
    def test_media_actions_are_authenticated_allowlisted_and_map_conflicts(self):
        with TestClient(create_app(Settings(agent_api_key="secret"), identity_verifier=FixtureIdentityVerifier())) as client:
            action = AsyncMock(return_value={"status": "processing"})
            client.app.state.agent = SimpleNamespace(advance_media=action)
            self.assertEqual(client.post("/v1/agent/runs/r/media/advance").status_code, 401)
            headers = {"authorization": "Bearer unit-user", "x-agent-key": "secret"}
            self.assertEqual(client.post("/v1/agent/runs/r/media/advance", headers=headers).status_code, 200)
            self.assertEqual(client.post("/v1/agent/runs/r/media/verify", headers=headers).status_code, 200)
            self.assertTrue(action.await_args.kwargs["verify"])
            self.assertEqual(action.await_args.kwargs["identity"].user_id, "fixture-user")
            self.assertEqual(client.post("/v1/agent/runs/r/media/submit", headers=headers).status_code, 404)
            self.assertEqual(action.await_count, 2)
            action.side_effect = RunConflictError("unknown")
            self.assertEqual(client.post("/v1/agent/runs/r/media/advance", headers=headers).status_code, 409)

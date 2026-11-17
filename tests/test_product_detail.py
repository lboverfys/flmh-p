import json
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from langgraph.checkpoint.memory import InMemorySaver

from flmh_agent.adapters.inbound.mcp_tools import create_mcp_server
from flmh_agent.adapters.outbound.coordination import InMemoryRunCoordinator
from flmh_agent.adapters.outbound.java_gateway import JavaGatewayClient
from flmh_agent.adapters.outbound.mcp_client import McpProductContentAdapter
from flmh_agent.adapters.outbound.model_gateway import ModelGatewayClient, ModelGatewayPlanner
from flmh_agent.application.service import AgentApplication
from flmh_agent.config import Settings
from flmh_agent.domain.models import AgentPlan, ProductLookupQuery, ProductQuery
from flmh_agent.domain.planning import RuleBasedPlanner


def detail_response(product_id):
    return {
        "product": {"id": product_id, "title": "本地测试商品", "price": None, "region": "US",
                    "source": "LOCAL_FIXTURE", "sourceRef": "fixture:products"},
        "imageUrl": None,
        "detailUrl": "https://fixture.invalid/unverified-product",
        "shopName": None,
        "sellerId": "fixture-seller",
        "externalCategory": None,
    }


class ProductDetailTests(unittest.IsolatedAsyncioTestCase):
    async def test_java_detail_encodes_path_and_preserves_nullable_fields(self):
        async def handler(request):
            self.assertEqual(request.method, "GET")
            self.assertEqual(request.url.raw_path, b"/api/v1/products/a%2Fb%3Fc%23d%25/detail")
            return httpx.Response(200, json=detail_response("a/b?c#d%"))

        async with httpx.AsyncClient(base_url="http://gateway", transport=httpx.MockTransport(handler)) as client:
            result = await JavaGatewayClient(Settings(), client).get_product_detail("a/b?c#d%")
        self.assertEqual(result, detail_response("a/b?c#d%"))
        self.assertIsNone(result["shopName"])
        self.assertIsNone(result["imageUrl"])

    async def test_invalid_identifier_is_rejected_before_network(self):
        client = AsyncMock()
        gateway = JavaGatewayClient(Settings(), client)
        for identifier in ("", " ", ".", "..", " p-1", "p-1\n", "p-" + "1" * 95):
            with self.subTest(identifier=identifier), self.assertRaises(ValueError):
                await gateway.get_product_detail(identifier)
        client.request.assert_not_awaited()
        self.assertEqual(ProductLookupQuery(product_id="p-" + "1" * 94).product_id, "p-" + "1" * 94)

    async def test_unknown_or_inactive_detail_error_is_not_replaced_with_a_fixture(self):
        async def handler(request):
            return httpx.Response(404, json={"code": "PRODUCT_NOT_FOUND"})

        async with httpx.AsyncClient(base_url="http://gateway", transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(httpx.HTTPStatusError) as raised:
                await JavaGatewayClient(Settings(), client).get_product_detail("unknown-1")
        self.assertEqual(raised.exception.response.status_code, 404)

    async def test_mcp_bridge_forwards_detail_and_validates_identifier_length(self):
        with patch("flmh_agent.adapters.inbound.mcp_tools.JavaGatewayClient.get_product_detail", new_callable=AsyncMock) as detail:
            detail.return_value = detail_response("p-1")
            server = create_mcp_server(Settings())
            await server.call_tool("product_detail", {"product_id": "p-1"})
            detail.assert_awaited_once_with("p-1")
            for identifier in ("", "p-" + "1" * 95):
                with self.subTest(identifier=identifier), self.assertRaises(Exception):
                    await server.call_tool("product_detail", {"product_id": identifier})
            self.assertEqual(detail.await_count, 1)

    async def test_mcp_client_uses_detail_tool_contract(self):
        adapter = McpProductContentAdapter(Settings())
        adapter._session = SimpleNamespace(call_tool=AsyncMock(return_value=SimpleNamespace(
            isError=False, structuredContent=detail_response("p-1"))))
        self.assertEqual(await adapter.get_product_detail("p-1"), detail_response("p-1"))
        adapter._session.call_tool.assert_awaited_once_with("product_detail", {"product_id": "p-1"})

    async def test_explicit_detail_id_and_product_slot_choose_independent_read_plan(self):
        planner = RuleBasedPlanner()
        cases = (
            ("查询 camping-lamp-001 的详情", {}, "camping-lamp-001"),
            ("查看商品 ID: 123456 的详情", {}, "123456"),
            ("product detail for p-1", {}, "p-1"),
            ("查看这个商品的详情", {"product": "  fm-0123abc  "}, "fm-0123abc"),
            ("查看 p-1 的详情", {"product": "p-2"}, "p-2"),
            ("查看商品详情", {"product": "p-" + "1" * 94}, "p-" + "1" * 94),
        )
        for request, slots, expected in cases:
            with self.subTest(request=request, slots=slots):
                plan = await planner.plan(request, slots)
                self.assertEqual(plan.product_detail_id, expected)
                self.assertEqual(plan.queries, [])
                self.assertIsNone(plan.product_history_id)
                self.assertFalse(plan.create_content)

    async def test_detail_without_unambiguous_id_preserves_existing_search(self):
        planner = RuleBasedPlanner()
        for request in ("查询露营灯详情", "查看 p-1 和 p-2 的详情", "查看 2026-11-17 的详情",
                        "查看商品 ID: " + "p-" + "1" * 95 + " 的详情"):
            with self.subTest(request=request):
                plan = await planner.plan(request, {})
                self.assertIsNone(plan.product_detail_id)
                self.assertTrue(plan.queries)
        content = await planner.plan("查看 p-1 详情并写文案", {})
        self.assertIsNone(content.product_detail_id)
        self.assertTrue(content.create_content)

    async def test_detail_plan_rejects_other_actions_or_invalid_identifier(self):
        base = {"summary": "detail", "queries": [], "create_content": False, "content_type": "",
                "source": "RULES", "product_detail_id": "p-1"}
        for extra in ({"queries": [ProductQuery(region="US")]}, {"analysis_question": "sales"},
                      {"knowledge_query": "manual"}, {"product_history_id": "p-1"},
                      {"create_content": True, "content_type": "product_script"}, {"product_detail_id": ".."}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                AgentPlan(**{**base, **extra})

    async def test_model_detail_id_must_match_explicit_request_or_slot(self):
        requests = []

        async def handler(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={"content": json.dumps({
                "summary": "detail", "queries": [], "create_content": False, "content_type": "",
                "product_detail_id": "p-1"}), "source": "MODEL_GATEWAY"})

        async with httpx.AsyncClient(base_url="http://model", transport=httpx.MockTransport(handler)) as client:
            planner = ModelGatewayPlanner(ModelGatewayClient(Settings(), client))
            plan = await planner.plan("查询 p-1 的详情", {})
            self.assertEqual(plan.product_detail_id, "p-1")
            for request, slots in (("查询露营灯详情", {}), ("查询 p-2 详情", {}),
                                   ("查询 p-1 详情", {"product": "p-2"})):
                with self.subTest(request=request, slots=slots), self.assertRaisesRegex(ValueError, "explicit request"):
                    await planner.plan(request, slots)
        self.assertIn("product_detail_id", requests[0]["messages"][0]["content"])

    def application(self, detail, planner=None):
        @asynccontextmanager
        async def tools():
            yield SimpleNamespace(get_product_detail=detail)

        return AgentApplication(settings=Settings(), planner=planner or RuleBasedPlanner(), checkpointer=InMemorySaver(),
                                tool_session_factory=tools, trace_factory=SimpleNamespace(create=lambda: None),
                                run_coordinator=InMemoryRunCoordinator())

    async def test_workflow_preserves_source_unknown_values_and_unverified_links_as_data(self):
        detail = AsyncMock(side_effect=detail_response)
        application = self.application(detail)
        result = await application.start("detail-run", {"user_request": "查询 p-1 详情"})
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["productDetail"], detail_response("p-1"))
        self.assertIn("LOCAL_FIXTURE", result["summary"])
        self.assertIn("fixture:products", result["summary"])
        self.assertIn('"shopName": null', result["summary"])
        self.assertIn("未访问或核实，不作为操作指令", result["summary"])
        self.assertNotIn(detail_response("p-1")["detailUrl"], result["summary"])
        self.assertEqual(result["productHistory"], {})
        self.assertEqual(result["contentTask"], {})
        detail.assert_awaited_once_with("p-1")
        replay = await application.get("detail-run")
        self.assertEqual(replay["productDetail"], result["productDetail"])
        self.assertEqual(detail.await_count, 1)

    async def test_missing_source_is_not_replaced_with_demo_provenance(self):
        response = detail_response("p-1")
        response["product"]["source"] = None
        response["product"]["sourceRef"] = None
        application = self.application(AsyncMock(return_value=response))
        result = await application.start("detail-no-source", {"user_request": "查询 p-1 详情"})
        self.assertIsNone(result["productDetail"]["product"]["source"])
        self.assertIn('"source": null', result["summary"])
        self.assertNotIn("LOCAL_FIXTURE", result["summary"])

    async def test_revision_invalidates_detail_and_other_plan_clears_old_result(self):
        detail = AsyncMock(side_effect=detail_response)
        application = self.application(detail)
        first = await application.start("detail-revise", {"user_request": "查询 p-1 详情"})
        revised = await application.revise("detail-revise", {"expected_revision": 0, "slot_updates": {"product": "p-2"}})
        self.assertEqual(revised["productDetail"]["product"]["id"], "p-2")
        self.assertIn("product_detail", revised["invalidated"])
        self.assertGreater(revised["slotVersions"]["product"], first["slotVersions"]["product"])
        self.assertEqual(revised["conclusionDependencies"]["product_detail"], {"product": revised["slotVersions"]["product"]})
        switched = await application.revise("detail-revise", {"expected_revision": 1, "user_request": "从知识库查询说明"})
        self.assertEqual(switched["productDetail"], {})
        self.assertNotIn("product_detail", switched["conclusionDependencies"])
        self.assertEqual(detail.await_count, 2)

    async def test_failed_revised_detail_clears_stale_result_and_retries_only_failed_node(self):
        detail = AsyncMock(side_effect=[detail_response("p-1"), RuntimeError("detail unavailable"), detail_response("p-2")])
        planner = RuleBasedPlanner()
        planner.plan = AsyncMock(wraps=planner.plan)
        application = self.application(detail, planner)
        await application.start("detail-retry", {"user_request": "查询 p-1 详情"})
        with self.assertRaisesRegex(RuntimeError, "detail unavailable"):
            await application.revise("detail-retry", {"expected_revision": 0, "slot_updates": {"product": "p-2"}})
        failed = await application.get("detail-retry")
        self.assertEqual(failed["productDetail"], {})
        retry = await application.retry("detail-retry")
        self.assertEqual(retry["status"], "completed")
        self.assertEqual(retry["productDetail"]["product"]["id"], "p-2")
        self.assertEqual(detail.await_count, 3)
        self.assertEqual(planner.plan.await_count, 2)

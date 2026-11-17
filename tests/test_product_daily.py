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
from flmh_agent.domain.models import AgentPlan, ProductQuery
from flmh_agent.domain.planning import RuleBasedPlanner


class ProductDailyTests(unittest.IsolatedAsyncioTestCase):
    async def test_daily_plan_requires_explicit_product_id_and_cannot_write(self):
        plan = await RuleBasedPlanner().plan("查看 p-1 的日报", {})
        self.assertEqual(plan.product_daily_id, "p-1")
        self.assertFalse(plan.create_content)
        self.assertEqual(plan.queries, [])
        unknown = await RuleBasedPlanner().plan("查看露营灯日报", {})
        self.assertIsNone(unknown.product_daily_id)
        for extra in ({"queries": [ProductQuery(region="US")]}, {"product_history_id": "p-1"},
                      {"product_detail_id": "p-1"}, {"analysis_question": "sales"}, {"create_content": True}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                AgentPlan.model_validate({**plan.model_dump(), **extra})

    async def test_daily_gateway_passes_bounded_cursor_source_and_validates_date(self):
        seen = []
        async def handler(request):
            seen.append(request)
            return httpx.Response(200, json={"items": [], "nextBeforeDate": None})
        async with httpx.AsyncClient(base_url="http://gateway", transport=httpx.MockTransport(handler)) as client:
            gateway = JavaGatewayClient(Settings(), client)
            await gateway.get_product_daily("p/1", "LOCAL_FIXTURE", limit=2, before_date="2026-11-17")
            for invalid in ({"limit": 101}, {"limit": True}, {"before_date": "not-date"}):
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    await gateway.get_product_daily("p-1", "LOCAL_FIXTURE", **invalid)
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0].url.raw_path.startswith(b"/api/v1/products/p%2F1/daily-statistics?"))
        self.assertEqual(dict(seen[0].url.params), {"source": "LOCAL_FIXTURE", "limit": "2", "beforeDate": "2026-11-17"})

    async def test_mcp_daily_contract_preserves_source_and_pagination(self):
        with patch("flmh_agent.adapters.inbound.mcp_tools.JavaGatewayClient.get_product_daily", new_callable=AsyncMock) as daily:
            daily.return_value = {"items": []}
            await create_mcp_server(Settings()).call_tool("product_daily_statistics", {"product_id": "p-1", "source": "LOCAL_FIXTURE"})
            daily.assert_awaited_once_with("p-1", "LOCAL_FIXTURE", limit=30, before_date=None)
        adapter = McpProductContentAdapter(Settings())
        adapter._session = SimpleNamespace(call_tool=AsyncMock(return_value=SimpleNamespace(isError=False, structuredContent={"items": []})))
        await adapter.get_product_daily("p-1", "LOCAL_FIXTURE", limit=5, before_date="2026-11-17")
        adapter._session.call_tool.assert_awaited_once_with("product_daily_statistics", {"product_id": "p-1", "source": "LOCAL_FIXTURE", "limit": 5, "before_date": "2026-11-17"})

    async def test_daily_graph_resolves_trusted_source_once_and_revises_only_product(self):
        detail = AsyncMock(side_effect=lambda product_id: {"product": {"id": product_id, "source": "LOCAL_FIXTURE"}})
        daily = AsyncMock(return_value={"items": [{"statDate": "2026-11-17", "scoreVersion": "radar-v2-exact-date",
                         "dataKind": "LOCAL_FIXTURE", "scores": {"opportunityScore": 20}, "trend": {"growthRate7d": None},
                         "limitations": ["MISSING_7D_BASELINE"]}], "nextBeforeDate": "2026-11-17"})
        @asynccontextmanager
        async def tools():
            yield SimpleNamespace(get_product_detail=detail, get_product_daily=daily)
        app = AgentApplication(settings=Settings(), planner=RuleBasedPlanner(), checkpointer=InMemorySaver(),
                               tool_session_factory=tools, trace_factory=SimpleNamespace(create=lambda: None),
                               run_coordinator=InMemoryRunCoordinator())
        result = await app.start("daily", {"user_request": "查看 p-1 的日报"})
        self.assertEqual(result["status"], "completed")
        self.assertIn("radar-v2-exact-date", result["summary"])
        self.assertIn("MISSING_7D_BASELINE", result["summary"])
        self.assertIn("未自动加载", result["summary"])
        self.assertIsNone(result["productDaily"]["items"][0]["trend"]["growthRate7d"])
        daily.assert_awaited_once_with("p-1", "LOCAL_FIXTURE", limit=30, before_date=None)
        daily.return_value = {"items": [], "nextBeforeDate": None}
        changed = await app.revise("daily", {"expected_revision": 0, "slot_updates": {"product": "p-2"}})
        self.assertIn("product_daily", changed["invalidated"])
        self.assertEqual(changed["productDaily"]["productId"], "p-2")
        self.assertIn("没有已保存日统计", changed["summary"])
        self.assertEqual(detail.await_count, 2)
        self.assertEqual(daily.await_count, 2)

    async def test_model_cannot_invent_daily_product_id(self):
        async def handler(request):
            return httpx.Response(200, json={"content": json.dumps({"summary": "daily", "queries": [], "create_content": False,
                "content_type": "", "product_daily_id": "p-1"})})
        async with httpx.AsyncClient(base_url="http://model", transport=httpx.MockTransport(handler)) as client:
            planner = ModelGatewayPlanner(ModelGatewayClient(Settings(), client))
            self.assertEqual((await planner.plan("查看 p-1 的日报", {})).product_daily_id, "p-1")
            with self.assertRaisesRegex(ValueError, "explicit request"):
                await planner.plan("查看露营灯日报", {})

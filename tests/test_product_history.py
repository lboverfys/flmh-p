import json
import unittest
from identity_support import IdentityTestCase
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
from flmh_agent.domain.models import AgentPlan, ProductHistoryQuery, ProductQuery
from flmh_agent.domain.planning import RuleBasedPlanner
from flmh_agent.domain.product_trend import compare_product_snapshots


def history_response(product_id, *, limit=30, before_version=None):
    return {
        "product": {"id": product_id, "source": "LOCAL_FIXTURE", "sourceRef": "fixture:products"},
        "metrics": [{"metricVersion": 2, "capturedAt": "2026-11-17T00:00:00Z", "price": None,
                     "sales7d": 15, "salesAmount7d": None, "trendScore": None,
                     "source": None, "sourceRef": None}],
        "nextBeforeVersion": 2,
        "ordering": "METRIC_VERSION_DESC",
    }


class ProductHistoryTests(IdentityTestCase):
    async def test_java_history_encodes_path_preserves_nullable_values_and_cursor(self):
        async def handler(request):
            self.assertEqual(request.url.raw_path, b"/api/v1/products/a%2Fb%3Fc%23d%25/history?limit=7&beforeVersion=9")
            return httpx.Response(200, json=history_response("a/b?c#d%"))

        async with httpx.AsyncClient(base_url="http://gateway", transport=httpx.MockTransport(handler)) as client:
            result = await JavaGatewayClient(Settings(), client).get_product_history("a/b?c#d%", limit=7, before_version=9)
        self.assertIsNone(result["metrics"][0]["price"])
        self.assertIsNone(result["metrics"][0]["source"])
        self.assertEqual(result["nextBeforeVersion"], 2)

    async def test_history_rejects_invalid_bounds_before_network(self):
        client = AsyncMock()
        gateway = JavaGatewayClient(Settings(), client)
        for invalid in ({"limit": 0}, {"limit": 101}, {"limit": True}, {"limit": 1.5},
                        {"before_version": 0}, {"before_version": True}, {"before_version": 1.5},
                        {"before_version": 9223372036854775808}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                await gateway.get_product_history("p-1", **invalid)
        for invalid_id in ("", " ", "..", " x", "x\n"):
            with self.subTest(invalid_id=invalid_id), self.assertRaises(ValueError):
                ProductHistoryQuery(product_id=invalid_id)
        client.request.assert_not_awaited()

    async def test_mcp_bridge_validates_and_forwards_pagination(self):
        with patch("flmh_agent.adapters.inbound.mcp_tools.JavaGatewayClient.get_product_history", new_callable=AsyncMock) as history:
            history.return_value = history_response("p-1")
            server = create_mcp_server(Settings())
            await server.call_tool("product_history", {"product_id": "p-1", "limit": 8, "before_version": 4})
            history.assert_awaited_once_with("p-1", limit=8, before_version=4)
            for invalid in (0, 101, True):
                with self.subTest(invalid=invalid), self.assertRaises(Exception):
                    await server.call_tool("product_history", {"product_id": "p-1", "limit": invalid})
            self.assertEqual(history.await_count, 1)

    async def test_mcp_client_uses_history_tool_contract(self):
        adapter = McpProductContentAdapter(Settings())
        adapter._session = SimpleNamespace(call_tool=AsyncMock(return_value=SimpleNamespace(
            isError=False, structuredContent=history_response("p-1"))))
        result = await adapter.get_product_history("p-1", limit=10, before_version=3)
        adapter._session.call_tool.assert_awaited_once_with("product_history", {"product_id": "p-1", "limit": 10, "before_version": 3})
        self.assertEqual(result, history_response("p-1"))

    async def test_explicit_history_id_and_product_slot_choose_history_plan(self):
        planner = RuleBasedPlanner()
        cases = (
            ("查询 camping-lamp-001 的历史趋势", {}, "camping-lamp-001"),
            ("查看商品 ID: 123456 的历史", {}, "123456"),
            ("查看这个商品的趋势", {"product": "fm-0123abc"}, "fm-0123abc"),
            ("查看这个商品的趋势", {"product": "  p-1  "}, "p-1"),
            ("查看 camping-lamp-001 的历史", {"product": "camping-lamp-002"}, "camping-lamp-002"),
        )
        for request, slots, expected in cases:
            with self.subTest(request=request, slots=slots):
                plan = await planner.plan(request, slots)
                self.assertEqual(plan.product_history_id, expected)
                self.assertEqual(plan.queries, [])
                self.assertIsNone(plan.analysis_question)
                self.assertFalse(plan.create_content)

    async def test_no_explicit_id_preserves_existing_bi_or_search_behavior(self):
        planner = RuleBasedPlanner()
        bi = await planner.plan("分析美国露营灯销量趋势", {})
        search = await planner.plan("查询露营灯历史", {})
        ambiguous = await planner.plan("比较 p-1 和 p-2 的趋势", {})
        dated = await planner.plan("查询 2026-11-17 的销量趋势", {})
        self.assertIsNone(bi.product_history_id)
        self.assertIsNotNone(bi.analysis_question)
        self.assertIsNone(search.product_history_id)
        self.assertTrue(search.queries)
        self.assertIsNone(ambiguous.product_history_id)
        self.assertIsNone(dated.product_history_id)
        self.assertIsNotNone(dated.analysis_question)

    async def test_oversized_explicit_identifier_is_not_truncated_to_a_different_product(self):
        planner = RuleBasedPlanner()
        identifier = "p-" + "1" * 95
        for request in (f"查询商品 ID: {identifier} 的趋势", f"查询 {identifier} 的趋势"):
            with self.subTest(request=request):
                plan = await planner.plan(request, {})
                self.assertIsNone(plan.product_history_id)
                self.assertEqual(plan.analysis_question, request)
        with self.assertRaises(ValueError):
            ProductHistoryQuery(product_id=identifier)

    async def test_history_cannot_be_mixed_with_other_plan_actions(self):
        base = {"summary": "history", "queries": [], "create_content": False, "content_type": "",
                "source": "RULES", "product_history_id": "p-1"}
        for extra in ({"queries": [ProductQuery(region="US")]}, {"analysis_question": "sales"},
                      {"knowledge_query": "manual"}, {"create_content": True, "content_type": "product_script"}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                AgentPlan(**{**base, **extra})

    async def test_model_history_plan_validates_explicit_id_and_prompt(self):
        requests = []
        async def handler(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={"content": json.dumps({
                "summary": "history", "queries": [], "create_content": False, "content_type": "",
                "product_history_id": "p-1"}), "source": "MODEL_GATEWAY"})

        async with httpx.AsyncClient(base_url="http://model", transport=httpx.MockTransport(handler)) as client:
            planner = ModelGatewayPlanner(ModelGatewayClient(Settings(), client))
            plan = await planner.plan("查询 p-1 的历史", {})
            self.assertEqual(plan.product_history_id, "p-1")
            with self.assertRaisesRegex(ValueError, "explicit request"):
                await planner.plan("查询露营灯历史", {})
        self.assertIn("product_history_id", requests[0]["messages"][0]["content"])

    def application(self, history):
        @asynccontextmanager
        async def tools():
            yield SimpleNamespace(get_product_history=history)
        return AgentApplication(settings=Settings(), planner=RuleBasedPlanner(), checkpointer=InMemorySaver(),
                                tool_session_factory=tools, trace_factory=SimpleNamespace(create=lambda: None),
                                run_coordinator=InMemoryRunCoordinator())

    async def test_workflow_returns_sourced_snapshots_without_inventing_daily_values(self):
        history = AsyncMock(side_effect=history_response)
        application = self.application(history)
        result = await application.start("history-run", {"user_request": "查 p-1 的历史趋势"})
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["productHistory"], history_response("p-1"))
        self.assertIn("采集时间：2026-11-17T00:00:00Z", result["summary"])
        self.assertIn('"price": null', result["summary"])
        self.assertIn("不是逐日统计", result["summary"])
        self.assertIn("历史样本不足两次", result["summary"])
        self.assertEqual(result["products"], [])
        self.assertEqual(result["contentTask"], {})
        history.assert_awaited_once_with("p-1", limit=30, before_version=None)
        await application.get("history-run")
        self.assertEqual(history.await_count, 1)

    async def test_workflow_reports_observed_snapshot_change_with_real_elapsed_time(self):
        response = history_response("p-1")
        response["metrics"] = [
            {"metricVersion": 2, "capturedAt": "2026-11-17T12:00:00Z", "sales7d": 24,
             "salesAmount7d": 200, "relatedAuthorCount": 5, "source": "LOCAL_FIXTURE", "sourceRef": "fixture:second"},
            {"metricVersion": 1, "capturedAt": "2026-11-17T06:00:00Z", "sales7d": 20,
             "salesAmount7d": 100, "relatedAuthorCount": None, "source": "LOCAL_FIXTURE", "sourceRef": "fixture:first"},
        ]
        response["nextBeforeVersion"] = None
        history = AsyncMock(return_value=response)
        application = self.application(history)
        result = await application.start("history-observed-change", {"user_request": "查 p-1 的历史趋势"})
        self.assertIn("实际间隔 6.00 小时", result["summary"])
        self.assertIn("sales7d：20.0 → 24.0，变化 4.0（20.00%）", result["summary"])
        self.assertIn("不外推日增长率", result["summary"])
        self.assertIn("逐快照未保存币种，不计算金额变化", result["summary"])
        self.assertNotIn("salesAmount7d：100", result["summary"])
        self.assertIn("fixture:first", result["summary"])
        self.assertIn("fixture:second", result["summary"])
        self.assertEqual(result["productHistory"], response)

    async def test_workflow_refuses_changes_when_snapshot_sources_are_missing_or_different(self):
        for source, message in ((None, "快照来源缺失"), ("ANOTHER_SOURCE", "快照来源不同")):
            with self.subTest(source=source):
                response = history_response("p-1")
                response["metrics"] = [
                    {"metricVersion": 2, "capturedAt": "2026-11-17T12:00:00Z", "sales7d": 24,
                     "source": source, "sourceRef": "fixture:second"},
                    {"metricVersion": 1, "capturedAt": "2026-11-17T06:00:00Z", "sales7d": 20,
                     "source": "LOCAL_FIXTURE", "sourceRef": "fixture:first"},
                ]
                application = self.application(AsyncMock(return_value=response))
                result = await application.start("history-source", {"user_request": "查 p-1 的历史趋势"})
                self.assertIn(message, result["summary"])
                self.assertNotIn("sales7d：20.0 → 24.0", result["summary"])
                self.assertEqual(result["productHistory"], response)

    async def test_product_revision_invalidates_history_and_switching_plan_clears_it(self):
        history = AsyncMock(side_effect=history_response)
        application = self.application(history)
        first = await application.start("history-revise", {"user_request": "查 p-1 的历史趋势"})
        revised = await application.revise("history-revise", {"expected_revision": 0, "slot_updates": {"product": "p-2"}})
        self.assertEqual(revised["productHistory"]["product"]["id"], "p-2")
        self.assertIn("product_history", revised["invalidated"])
        self.assertGreater(revised["slotVersions"]["product"], first["slotVersions"]["product"])
        self.assertEqual(revised["conclusionDependencies"]["product_history"], {"product": revised["slotVersions"]["product"]})
        changed = await application.revise("history-revise", {"expected_revision": 1, "user_request": "从知识库查询说明"})
        self.assertEqual(changed["productHistory"], {})
        self.assertEqual(history.await_count, 2)

    async def test_history_accepts_full_product_identifier_length_without_relaxing_keyword_limit(self):
        identifier = "p-" + "1" * 94
        history = AsyncMock(side_effect=history_response)
        application = self.application(history)
        result = await application.start("long-id", {"user_request": f"查 {identifier} 的历史"})
        self.assertEqual(result["productHistory"]["product"]["id"], identifier)
        with self.assertRaises(ValueError):
            ProductQuery(region="US", keyword=identifier)

    async def test_failed_history_step_retries_without_replanning(self):
        history = AsyncMock(side_effect=[RuntimeError("history unavailable"), history_response("p-1")])
        application = self.application(history)
        with self.assertRaisesRegex(RuntimeError, "history unavailable"):
            await application.start("history-retry", {"user_request": "查 p-1 历史"})
        retry = await application.retry("history-retry")
        self.assertEqual(retry["status"], "completed")
        self.assertEqual(retry["productHistory"], history_response("p-1"))
        self.assertEqual(history.await_count, 2)

    def test_snapshot_comparison_uses_actual_capture_interval_and_only_supplied_values(self):
        trend = compare_product_snapshots([
            {"metricVersion": 3, "capturedAt": "2026-11-17T12:00:00Z", "sales7d": 24,
             "salesAmount7d": 120, "relatedAuthorCount": 5, "source": "LOCAL_FIXTURE", "sourceRef": "fixture:3"},
            {"metricVersion": 1, "capturedAt": "2026-11-17T00:00:00Z", "sales7d": 20,
             "salesAmount7d": 100, "relatedAuthorCount": 4, "source": "LOCAL_FIXTURE", "sourceRef": "fixture:1"},
            {"metricVersion": 2, "capturedAt": "2026-11-17T06:00:00Z", "sales7d": 22,
             "salesAmount7d": 110, "relatedAuthorCount": None, "source": "LOCAL_FIXTURE", "sourceRef": "fixture:2"},
        ])
        self.assertEqual(trend["status"], "AVAILABLE")
        self.assertEqual(trend["elapsedHours"], 6.0)
        self.assertEqual(trend["changes"]["sales7d"], {
            "previous": 22.0, "current": 24.0, "delta": 2.0, "relativeChange": 2 / 22,
        })
        self.assertNotIn("salesAmount7d", trend["changes"])
        self.assertIn("relatedAuthorCount", trend["unavailableMetrics"])
        self.assertEqual(trend["previousSourceRef"], "fixture:2")
        self.assertEqual(trend["currentSourceRef"], "fixture:3")

    def test_snapshot_comparison_marks_sparse_invalid_and_zero_baseline_without_fabrication(self):
        one = [{"metricVersion": 1, "capturedAt": "2026-11-17T00:00:00Z", "sales7d": 10}]
        self.assertEqual(compare_product_snapshots(one)["status"], "INSUFFICIENT_SNAPSHOTS")
        same_time = [dict(one[0]), {**one[0], "metricVersion": 2, "sales7d": 20}]
        self.assertEqual(compare_product_snapshots(same_time)["status"], "NO_ELAPSED_INTERVAL")
        invalid_time = [dict(one[0]), {**one[0], "metricVersion": 2, "capturedAt": "unknown"}]
        self.assertEqual(compare_product_snapshots(invalid_time)["status"], "INVALID_TIMESTAMP")
        zero_baseline = compare_product_snapshots([
            {"metricVersion": 1, "capturedAt": "2026-11-16T00:00:00Z", "sales7d": 0, "source": "LOCAL_FIXTURE"},
            {"metricVersion": 2, "capturedAt": "2026-11-17T00:00:00Z", "sales7d": 4, "source": "LOCAL_FIXTURE"},
        ])
        self.assertIsNone(zero_baseline["changes"]["sales7d"]["relativeChange"])

    def test_snapshot_comparison_rejects_missing_or_different_sources(self):
        previous = {"metricVersion": 1, "capturedAt": "2026-11-16T00:00:00Z", "sales7d": 10,
                    "source": "LOCAL_FIXTURE", "sourceRef": "fixture:old"}
        current = {"metricVersion": 2, "capturedAt": "2026-11-17T00:00:00Z", "sales7d": 20,
                   "sourceRef": "fixture:new"}
        for source in (None, "", " "):
            with self.subTest(source=source):
                trend = compare_product_snapshots([previous, {**current, "source": source}])
                self.assertEqual(trend["status"], "MISSING_SOURCE")
                self.assertEqual(trend["changes"], {})
        changed = compare_product_snapshots([previous, {**current, "source": "FAST_MOSS"}])
        self.assertEqual(changed["status"], "SOURCE_CHANGED")
        self.assertEqual(changed["changes"], {})
        self.assertEqual(changed["previousSourceRef"], "fixture:old")
        self.assertEqual(changed["currentSourceRef"], "fixture:new")

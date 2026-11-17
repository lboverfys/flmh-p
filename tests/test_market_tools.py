from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from pydantic import ValidationError

from identity_support import IdentityTestCase
from flmh_agent.adapters.inbound.mcp_tools import create_mcp_server
from flmh_agent.adapters.outbound.java_gateway import JavaGatewayClient
from flmh_agent.adapters.outbound.mcp_client import McpProductContentAdapter
from flmh_agent.application.workflow import build_workflow
from flmh_agent.config import Settings
from flmh_agent.domain.models import ProductQuery
from flmh_agent.domain.planning import RuleBasedPlanner


class MarketToolsTests(IdentityTestCase):
    async def test_ranking_reads_the_index_once_and_preserves_published_order_and_provenance(self):
        requests = []
        async def respond(req):
            requests.append(req)
            return httpx.Response(200, json={"adapter": "LOCAL_MOCK_INDEX", "products": [
                {"productId": "p-9", "sourceRef": "fixture:9"}, {"productId": "p-1", "sourceRef": "fixture:1"}]})
        async with httpx.AsyncClient(base_url="http://gateway", transport=httpx.MockTransport(respond)) as http:
            result = await JavaGatewayClient(Settings(), http).product_ranking("US", 2)
        self.assertEqual([r["id"] for r in result["products"]], ["p-9", "p-1"])
        self.assertEqual(result["products"][0]["sourceRef"], "fixture:9")
        self.assertEqual([r.url.path for r in requests], ["/api/v1/rankings"])
        self.assertEqual(dict(requests[0].url.params), {"region": "US", "limit": "2"})

    async def test_regional_ranking_plan_calls_the_ranking_tool_and_rejects_unsupported_filters(self):
        adapter = McpProductContentAdapter(Settings())
        adapter._call = AsyncMock(return_value={"adapter": "LOCAL_MOCK_INDEX", "products": [{"id": "p-1", "title": "灯"}]})
        graph = build_workflow(planner=RuleBasedPlanner(), tools=adapter)
        result = await graph.ainvoke({"user_request": "查美国商品榜单前2名"}, {"configurable": {"thread_id": "market"}})
        adapter._call.assert_awaited_once_with("product_ranking", {"region": "US", "limit": 2})
        self.assertIn("LOCAL_MOCK_INDEX", result["summary"])
        for invalid in ({"ranking_limit": 51}, {"ranking_limit": 1, "keyword": "ignored"}, {"ranking_limit": 1, "max_price": 5}):
            with self.assertRaises(ValidationError):
                ProductQuery(region="US", **invalid)

    async def test_creator_projection_queries_once_preserves_unknown_counts_and_has_no_individual_data(self):
        history = {"product": {"id": "p-1"}, "metrics": [{"metricVersion": 2, "capturedAt": "2026-11-17T00:00:00Z",
                   "source": "LOCAL_FIXTURE", "sourceRef": "row:2", "relatedAuthorCount": None, "sales7d": 150}]}
        with patch.object(JavaGatewayClient, "get_product_history", new_callable=AsyncMock, return_value=history) as fetch:
            _, result = await create_mcp_server(Settings()).call_tool("product_creator_observations", {"product_id": "p-1"})
        fetch.assert_awaited_once_with("p-1", limit=30)
        self.assertEqual(result["scope"], "PRODUCT_RELATED_AUTHOR_COUNTS")
        self.assertEqual(result["metrics"], [{k: v for k, v in history["metrics"][0].items() if k != "sales7d"}])
        tools = SimpleNamespace(get_creator_observations=AsyncMock(return_value=result))
        graph = build_workflow(planner=RuleBasedPlanner(), tools=tools)
        state = await graph.ainvoke({"user_request": "查 p-1 的关联达人数"}, {"configurable": {"thread_id": "authors"}})
        tools.get_creator_observations.assert_awaited_once_with("p-1")
        self.assertIn("未提供", state["summary"])

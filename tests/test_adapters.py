import json
import unittest
from unittest.mock import patch

import httpx

from flmh_agent.adapters.outbound.bge_http import BgeM3HttpAdapter, BgeRerankerHttpAdapter, ElasticsearchHybridStore
from flmh_agent.adapters.inbound.mcp_tools import create_mcp_server
from flmh_agent.adapters.outbound.mcp_client import McpProductContentAdapter
from flmh_agent.adapters.outbound.model_gateway import ModelGatewayClient, ModelGatewayPlanner
from flmh_agent.config import Settings
from flmh_agent.domain.rag import Chunk
from flmh_agent.domain.models import ProductQuery


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_mcp_bridge_accepts_nullable_plan_search_fields(self):
        requests = []

        async def search_products(_gateway, query):
            requests.append(query)
            return {"products": [], "source": "CONTRACT_FIXTURE"}

        with patch("flmh_agent.adapters.inbound.mcp_tools.JavaGatewayClient.search_products", search_products):
            server = create_mcp_server(Settings())
            await server.call_tool("product_search", ProductQuery(region="US", keyword="lamp").model_dump())
            await server.call_tool("product_search", ProductQuery(region="US").model_dump())
        self.assertEqual(requests[0]["keyword"], "lamp")
        self.assertIsNone(requests[0]["category"])
        self.assertIsNone(requests[1]["keyword"])

    async def test_model_gateway_contract_and_plan_schema(self):
        requests = []

        async def handler(request):
            requests.append(request)
            return httpx.Response(200, json={
                "content": json.dumps({
                    "summary": "search products",
                    "queries": [{"region": "US", "keyword": "lamp"}],
                    "create_content": False,
                    "content_type": "",
                    "budget_max": None,
                }),
                "provider": "configured-provider",
                "model": "configured-model",
                "source": "MODEL_GATEWAY",
            })

        settings = Settings(planner_backend="model_gateway")
        async with httpx.AsyncClient(base_url="http://model-gateway", transport=httpx.MockTransport(handler)) as http:
            planner = ModelGatewayPlanner(ModelGatewayClient(settings, client=http))
            plan = await planner.plan("find a lamp", {})
        self.assertEqual(plan.source, "MODEL_GATEWAY")
        self.assertEqual(plan.queries[0].keyword, "lamp")
        self.assertEqual(requests[0].url.path, "/api/v1/models/completions")
        body = json.loads(requests[0].content)
        self.assertEqual(body["purpose"], "agent_plan")
        self.assertEqual(body["responseFormat"], "json_object")

    async def test_bge_m3_validates_dense_sparse_response(self):
        async def handler(request):
            payload = json.loads(request.content)
            self.assertTrue(payload["returnDense"])
            self.assertTrue(payload["returnSparse"])
            return httpx.Response(200, json={"data": [{"denseVector": [0.1, 0.2], "sparseVector": {"token": 0.6}}]})

        settings = Settings(
            embedding_backend="bge_m3_http", bge_m3_url="https://embed.test/v1/encode", bge_m3_model="bge-m3",
            bge_reranker_url="https://rerank.test/v1/rerank", bge_reranker_model="bge-reranker",
            elasticsearch_url="https://search.test", elasticsearch_index="chunks", embedding_dimension=2,
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            dense, sparse = await BgeM3HttpAdapter(settings, client=http).encode("query")
        self.assertEqual(dense, [0.1, 0.2])
        self.assertEqual(sparse, {"token": 0.6})

    async def test_reranker_and_elasticsearch_use_batch_contracts(self):
        calls = []

        async def handler(request):
            calls.append((request.method, request.url.path, json.loads(request.content) if request.content else None))
            if request.url.path.endswith("_mget"):
                return httpx.Response(200, json={"docs": [
                    {"_id": "parent-1", "found": True, "_source": {"text": "Parent text", "parent_id": ""}}
                ]})
            if request.url.path.endswith("_search"):
                return httpx.Response(200, json={"hits": {"hits": [
                    {"_id": "child-1", "_score": 0.8, "_source": {"text": "Child", "parent_id": "parent-1"}}
                ]}})
            return httpx.Response(200, json={"results": [{"index": 0, "relevanceScore": 0.99}]})

        settings = Settings(
            embedding_backend="bge_m3_http", bge_m3_url="https://embed.test", bge_m3_model="bge-m3",
            bge_reranker_url="https://rerank.test", bge_reranker_model="reranker",
            elasticsearch_url="https://search.test", elasticsearch_index="chunks",
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            reranked = await BgeRerankerHttpAdapter(settings, client=http).rerank("query", [Chunk("c", "p", "text")], 1)
            store = ElasticsearchHybridStore(settings, client=http)
            dense = await store.search_dense([0.1], 3)
            sparse = await store.search_sparse({"word": 0.3}, 3)
            parents = await store.get_parents(["parent-1"])
        self.assertEqual(reranked[0].score, 0.99)
        self.assertEqual(dense[0].chunk_id, "child-1")
        self.assertEqual(sparse[0].parent_id, "parent-1")
        self.assertEqual(parents[0].text, "Parent text")
        self.assertTrue(any(path.endswith("_mget") for _, path, _ in calls))
        self.assertIn("knn", calls[1][2])
        self.assertIn("sparse_vector", calls[2][2]["query"])

    async def test_mcp_product_content_tool_names_are_stable(self):
        adapter = object.__new__(McpProductContentAdapter)
        adapter._session = None
        adapter._stack = None

        class FakeSession:
            async def call_tool(self, name, arguments):
                self.name = name
                self.arguments = arguments
                return type("Result", (), {"isError": False, "structuredContent": {"ok": True}})()

        adapter._session = FakeSession()
        response = await adapter.confirm_quote("q-1")
        self.assertEqual(adapter._session.name, "content_quote_confirm")
        self.assertEqual(adapter._session.arguments, {"quote_id": "q-1", "confirmed": True})
        self.assertEqual(response, {"ok": True})

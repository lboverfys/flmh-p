import asyncio
import json
import unittest
from identity_support import IdentityTestCase
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
from langgraph.checkpoint.memory import InMemorySaver

from flmh_agent.adapters.outbound.coordination import InMemoryRunCoordinator
from flmh_agent.adapters.outbound.model_gateway import ModelGatewayClient, ModelGatewayPlanner
from flmh_agent.application.service import AgentApplication
from flmh_agent.application.workflow import _layered_search, build_workflow
from flmh_agent.config import Settings
from flmh_agent.domain.models import AgentPlan, ProductQuery


def query_plan(*queries):
    return AgentPlan(summary="测试跨地区同类商品查询", queries=list(queries), create_content=False, content_type="", source="RULES")


def binding_plan():
    return query_plan(
        ProductQuery(region="US", keyword="lamp", query_id="origin"),
        ProductQuery(region="ID", keyword="gift", max_price=20, query_id="similar",
                     depends_on=["origin"], category_from_query="origin"),
    )


def fixture_products(query, category="camping", *, revision=1):
    return {"source": "LOCAL_FIXTURE", "revision": revision, "products": [
        {"id": "p-1", "title": query.get("keyword") or "Fixture product", "region": query["region"], "category": category},
    ]}


class QueryBindingTests(IdentityTestCase):
    async def test_binding_accepts_static_filters_and_passes_only_resolved_query_arguments(self):
        calls = []

        async def search(query):
            calls.append(query)
            for metadata in ("query_id", "depends_on", "category_from_query"):
                self.assertNotIn(metadata, query)
            if query["region"] == "US":
                result = fixture_products(query)
                result["products"].append({"category": "electronics"})
                return result
            self.assertEqual(len(calls), 2)
            self.assertEqual(query, {"region": "ID", "keyword": "gift", "category": "camping", "max_price": 20})
            return fixture_products(query)

        graph = build_workflow(planner=SimpleNamespace(plan=AsyncMock(return_value=binding_plan())),
                               tools=SimpleNamespace(search_products=search))
        result = await graph.ainvoke({"user_request": "跨地区同类选品"}, {"configurable": {"thread_id": "binding"}})
        self.assertEqual(result["query_failures"], {})
        self.assertEqual(len(result["product_results"]), 2)
        self.assertEqual(len(calls), 2)

    async def test_binding_rejects_static_category_indirect_unknown_or_invalid_references(self):
        for arguments in (
            {"category_from_query": "origin"},
            {"category_from_query": "origin", "depends_on": ["other"]},
            {"category_from_query": "origin", "depends_on": ["origin"], "category": "camping"},
            {"category_from_query": "1origin", "depends_on": ["1origin"]},
            {"category_from_query": "a" * 33, "depends_on": ["a" * 33]},
            {"category_from_query": "origin.products[0].category", "depends_on": ["origin.products[0].category"]},
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                ProductQuery(region="ID", **arguments)
        with self.assertRaisesRegex(ValueError, "existing query"):
            query_plan(ProductQuery(region="ID", query_id="similar", depends_on=["unknown"], category_from_query="unknown"))
        with self.assertRaisesRegex(ValueError, "cycle"):
            query_plan(
                ProductQuery(region="US", query_id="origin", depends_on=["similar"], category_from_query="similar"),
                ProductQuery(region="ID", query_id="similar", depends_on=["origin"], category_from_query="origin"),
            )

    async def test_empty_missing_or_invalid_first_category_blocks_query_and_descendants(self):
        queries = [*binding_plan().queries,
                   ProductQuery(region="CA", query_id="tail", depends_on=["similar"], category_from_query="similar")]
        cases = (
            ([], "CATEGORY_BINDING_SOURCE_EMPTY"),
            ([{}], "CATEGORY_BINDING_CATEGORY_MISSING"),
            ([{"category": None}], "CATEGORY_BINDING_CATEGORY_MISSING"),
            ([None, {"category": "camping"}], "CATEGORY_BINDING_CATEGORY_MISSING"),
            ([{"category": ""}], "CATEGORY_BINDING_CATEGORY_INVALID"),
            ([{"category": "   "}], "CATEGORY_BINDING_CATEGORY_INVALID"),
            ([{"category": 17}], "CATEGORY_BINDING_CATEGORY_INVALID"),
            ([{"category": "x" * 101}], "CATEGORY_BINDING_CATEGORY_INVALID"),
            ([{}, {"category": "camping"}], "CATEGORY_BINDING_CATEGORY_MISSING"),
        )
        for products, failure in cases:
            with self.subTest(products=products):
                search = AsyncMock(return_value={"products": products})
                _, _, _, failures = await _layered_search(
                    [query.model_dump() for query in queries], SimpleNamespace(search_products=search), {})
                self.assertEqual(search.await_count, 1)
                self.assertEqual(failures, {"similar": failure, "tail": "DEPENDENCY_FAILED"})

    async def test_upstream_failure_prevents_bound_search(self):
        search = AsyncMock(side_effect=RuntimeError("source unavailable"))
        _, _, _, failures = await _layered_search(
            [query.model_dump() for query in binding_plan().queries], SimpleNamespace(search_products=search), {})
        self.assertEqual(search.await_count, 1)
        self.assertEqual(failures, {"origin": "RuntimeError", "similar": "DEPENDENCY_FAILED"})

    async def test_external_value_error_message_is_not_exposed_as_a_binding_failure(self):
        search = AsyncMock(side_effect=ValueError("private credential and internal provider details"))
        _, _, _, failures = await _layered_search(
            [query.model_dump() for query in binding_plan().queries], SimpleNamespace(search_products=search), {})
        self.assertEqual(failures, {"origin": "ValueError", "similar": "DEPENDENCY_FAILED"})
        self.assertNotIn("private credential", json.dumps(failures))

    async def test_changed_upstream_result_invalidates_child_cache_even_with_same_resolved_filters(self):
        calls = []

        async def search(query):
            calls.append(query)
            return fixture_products(query, revision=2 if query["keyword"] == "tent" else 1)

        tools = SimpleNamespace(search_products=search)
        queries = [query.model_dump() for query in binding_plan().queries]
        _, cache, reused, failures = await _layered_search(queries, tools, {})
        self.assertEqual(reused, [])
        self.assertEqual(failures, {})
        _, _, reused, _ = await _layered_search(queries, tools, cache)
        self.assertEqual(reused, ["origin", "similar"])
        self.assertEqual(len(calls), 2)
        changed = [{**queries[0], "keyword": "tent"}, queries[1]]
        _, _, reused, failures = await _layered_search(changed, tools, cache)
        self.assertEqual(failures, {})
        self.assertEqual(reused, [])
        self.assertEqual(len(calls), 4)
        self.assertEqual(calls[1], calls[3])

    async def test_independent_queries_remain_parallel_and_bound_child_waits_for_source(self):
        plan = query_plan(*binding_plan().queries, ProductQuery(region="CA", query_id="independent"))
        both_started = asyncio.Event()
        completed = set()
        active = 0
        maximum = 0

        async def search(query):
            nonlocal active, maximum
            if query["region"] == "ID":
                self.assertIn("US", completed)
                self.assertEqual(query["category"], "camping")
                return fixture_products(query)
            active += 1
            maximum = max(maximum, active)
            if active == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), timeout=2)
            active -= 1
            completed.add(query["region"])
            return fixture_products(query)

        results, _, _, failures = await _layered_search(
            [query.model_dump() for query in plan.queries], SimpleNamespace(search_products=search), {})
        self.assertEqual(failures, {})
        self.assertEqual(maximum, 2)
        self.assertEqual([result["products"][0]["region"] for result in results], ["US", "ID", "CA"])

    async def test_slot_revisions_preserve_binding_and_invalidate_cached_actual_queries(self):
        calls = []

        async def search(query):
            calls.append(query)
            category = query["category"] or query["keyword"] + "-category"
            return fixture_products(query, category)

        @asynccontextmanager
        async def tool_session():
            yield SimpleNamespace(search_products=search)

        application = AgentApplication(
            settings=Settings(), planner=SimpleNamespace(plan=AsyncMock(return_value=binding_plan())),
            checkpointer=InMemorySaver(), tool_session_factory=tool_session,
            trace_factory=SimpleNamespace(create=lambda: None), run_coordinator=InMemoryRunCoordinator())
        first = await application.start("bound-slots", {"user_request": "跨地区同类选品",
            "slot_updates": {"product": "blue", "region": "GB", "budget_max": 60}})
        self.assertEqual(first["failedQueries"], {})
        self.assertEqual(calls[1], {"region": "GB", "keyword": "blue", "category": "blue-category", "max_price": 60})
        changed = await application.revise("bound-slots", {
            "expected_revision": 0, "slot_updates": {"product": "red", "region": "TH", "budget_max": 40}})
        self.assertEqual(changed["failedQueries"], {})
        self.assertEqual(changed["reusedQueries"], [])
        self.assertEqual(changed["plan"]["queries"][1]["category_from_query"], "origin")
        self.assertEqual(calls[3], {"region": "TH", "keyword": "red", "category": "red-category", "max_price": 40})
        self.assertEqual(len(calls), 4)

    async def test_model_planner_accepts_and_describes_the_narrow_binding_contract(self):
        requests = []

        async def handler(request):
            requests.append(json.loads(request.content))
            plan = binding_plan().model_dump(exclude={"source"})
            return httpx.Response(200, json={"content": json.dumps(plan), "source": "MODEL_GATEWAY"})

        async with httpx.AsyncClient(base_url="http://model", transport=httpx.MockTransport(handler)) as client:
            planner = ModelGatewayPlanner(ModelGatewayClient(Settings(), client))
            plan = await planner.plan("先查美国露营灯，再查印尼的同类商品", {})
        self.assertEqual(plan.queries[1].category_from_query, "origin")
        prompt = requests[0]["messages"][0]["content"]
        self.assertIn("category_from_query", prompt)
        self.assertIn("第一条商品", prompt)
        self.assertIn("不得同时填静态 category", prompt)

import unittest

from langgraph.types import Command

from flmh_agent.application.workflow import build_workflow
from flmh_agent.domain.idempotency import idempotency_key
from flmh_agent.domain.planning import RuleBasedPlanner


class FakeProductContent:
    def __init__(self, quote_amount=3.99):
        self.calls = []
        self.quote_amount = quote_amount

    async def search_products(self, query):
        self.calls.append(("search", query))
        return {"source": "FIXTURE", "products": [{"id": "p-1", "title": "Fixture 耳机", "region": query["region"]}]}

    async def create_quote(self, request):
        self.calls.append(("quote", request))
        return {"quoteId": "q-1", "amount": self.quote_amount, "currency": "CNY", "source": "LOCAL"}

    async def confirm_quote(self, quote_id):
        self.calls.append(("confirm", quote_id))
        return {"quoteId": quote_id, "confirmed": True}

    async def submit_task(self, request, idempotency_key):
        self.calls.append(("submit", request, idempotency_key))
        return {"taskId": "t-1", "status": "SUBMITTED", "source": "LOCAL"}

    async def get_task(self, task_id):
        self.calls.append(("get", task_id))
        return {"taskId": task_id, "status": "SUCCEEDED", "source": "LOCAL"}


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    def build(self, tools):
        return build_workflow(
            planner=RuleBasedPlanner(),
            tools=tools,
        )

    async def test_content_task_is_gated_by_confirmation_and_status_is_read_back(self):
        tools = FakeProductContent()
        graph = self.build(tools)
        config = {"configurable": {"thread_id": "confirm-flow"}}
        request = {"user_request": "给我蓝牙耳机写口播脚本，预算不超过 50"}

        paused = await graph.ainvoke(request, config=config)
        self.assertIn("__interrupt__", paused)
        self.assertEqual([call[0] for call in tools.calls], ["search", "quote"])

        finished = await graph.ainvoke(Command(resume={"confirmed": True}), config=config)
        self.assertEqual([call[0] for call in tools.calls], ["search", "quote", "confirm", "submit", "get"])
        self.assertEqual(finished["task_status"]["status"], "SUCCEEDED")
        self.assertEqual(finished["content_brief"]["source"], "INPUT_REQUEST")

    async def test_rejection_never_confirms_or_submits(self):
        tools = FakeProductContent()
        graph = self.build(tools)
        config = {"configurable": {"thread_id": "reject-flow"}}
        await graph.ainvoke({"user_request": "写口播脚本预算不超过 50"}, config=config)
        finished = await graph.ainvoke(Command(resume={"confirmed": False}), config=config)
        self.assertIn("用户已拒绝报价", finished["summary"])
        self.assertEqual([call[0] for call in tools.calls], ["search", "quote"])

    async def test_over_budget_quote_stops_before_confirmation(self):
        tools = FakeProductContent(quote_amount=51)
        result = await self.build(tools).ainvoke(
            {"user_request": "给我耳机写口播，预算不超过 50"},
            config={"configurable": {"thread_id": "over-budget"}},
        )
        self.assertNotIn("__interrupt__", result)
        self.assertEqual([call[0] for call in tools.calls], ["search", "quote"])
        self.assertFalse(result["budget_ok"])

    async def test_independent_product_queries_run_in_parallel(self):
        class ParallelTools(FakeProductContent):
            def __init__(self):
                super().__init__()
                self.active = 0
                self.max_active = 0
                import asyncio
                self.both_started = asyncio.Event()

            async def search_products(self, query):
                import asyncio
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                if self.active == 2:
                    self.both_started.set()
                await asyncio.wait_for(self.both_started.wait(), timeout=2)
                self.active -= 1
                return {"products": [{"id": query["region"], "title": "Product", "region": query["region"]}]}

        tools = ParallelTools()
        result = await self.build(tools).ainvoke(
            {"user_request": "比较印尼和美国蓝牙耳机"},
            config={"configurable": {"thread_id": "parallel-search"}},
        )
        self.assertEqual(tools.max_active, 2)
        self.assertEqual([item["products"][0]["region"] for item in result["product_results"]], ["ID", "US"])

    async def test_changed_slot_increments_snapshot_version_and_invalidates_downstream(self):
        tools = FakeProductContent()
        result = await self.build(tools).ainvoke(
            {"user_request": "查蓝牙耳机", "slot_values": {"region": "ID"}, "slot_updates": {"region": "US"}},
            config={"configurable": {"thread_id": "slot-version"}},
        )
        self.assertEqual(result["slot_values"]["region"], "US")
        self.assertEqual(result["slot_version"], 1)
        self.assertIn("content_task", result["invalidated"])
        self.assertTrue(result["slot_snapshot"])


class IdempotencyTests(unittest.TestCase):
    def test_same_normalized_input_has_same_key_and_changed_input_does_not(self):
        values = {
            "user_request": "  写一段 口播 ", "product_id": "p-1", "content_type": "product_script",
            "budget_max": 50.0, "region": "id",
        }
        self.assertEqual(idempotency_key(**values), idempotency_key(**{**values, "user_request": "写一段 口播"}))
        self.assertNotEqual(idempotency_key(**values), idempotency_key(**{**values, "product_id": "p-2"}))

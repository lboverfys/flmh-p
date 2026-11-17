import unittest
import asyncio
from contextlib import asynccontextmanager

from langgraph.checkpoint.memory import InMemorySaver

from flmh_agent.adapters.outbound.coordination import InMemoryRunCoordinator
from flmh_agent.application.service import AgentApplication, RunConflictError
from flmh_agent.config import Settings
from flmh_agent.domain.planning import RuleBasedPlanner


class FakeTools:
    def __init__(self):
        self.calls = []

    async def search_products(self, query):
        self.calls.append("search")
        return {"products": [{"id": "p-1", "title": "Fixture product", "region": "ID"}]}

    async def create_quote(self, request):
        self.calls.append("quote")
        return {"quoteId": "q-1", "amount": 5, "currency": "IDR"}

    async def confirm_quote(self, quote_id):
        self.calls.append("confirm")
        return {"confirmed": True, "quote_id": "q-1"}

    async def submit_task(self, request, idempotency_key):
        self.calls.append("submit")
        return {"taskId": "t-1", "status": "SUBMITTED"}

    async def get_task(self, task_id):
        self.calls.append("get")
        return {"taskId": task_id, "status": "SUCCEEDED"}


class ApplicationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tools = FakeTools()

        @asynccontextmanager
        async def tool_session():
            yield self.tools

        class NoTrace:
            def create(self):
                return None

        self.application = AgentApplication(
            settings=Settings(),
            planner=RuleBasedPlanner(),
            checkpointer=InMemorySaver(),
            tool_session_factory=tool_session,
            trace_factory=NoTrace(),
            run_coordinator=InMemoryRunCoordinator(),
        )

    async def test_run_identifier_replays_identical_input_but_conflicts_on_different_input(self):
        first = await self.application.start("run-1", {"user_request": "查询印尼露营灯"})
        replay = await self.application.start("run-1", {"user_request": "查询印尼露营灯"})
        self.assertEqual(replay, first)
        self.assertEqual(self.tools.calls, ["search"])
        with self.assertRaisesRegex(RunConflictError, "different request"):
            await self.application.start("run-1", {"user_request": "查询印尼耳机"})

    async def test_missing_budget_is_explicit_and_never_requests_a_quote(self):
        result = await self.application.start("budget-missing", {"user_request": "给我露营灯写口播"})
        self.assertEqual(result["status"], "needs_input")
        self.assertEqual(result["requiredSlots"], ["budget_max"])
        self.assertEqual(self.tools.calls, ["search"])

    async def test_read_refreshes_pending_task_without_resubmission(self):
        status = "QUEUED"
        async def get_task(task_id):
            self.tools.calls.append("get")
            return {"taskId": task_id, "status": status}
        self.tools.get_task = get_task
        await self.application.start("pending", {"user_request": "给我露营灯写口播，预算 50"})
        first = await self.application.confirm("pending", {"confirmed": True, "quote_id": "q-1"})
        self.assertEqual(first["status"], "processing")
        status = "SUCCEEDED"
        result = await self.application.get("pending")
        self.assertEqual(result["taskStatus"]["status"], "SUCCEEDED")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.tools.calls.count("submit"), 1)
        self.assertEqual(self.tools.calls.count("get"), 2)

    async def test_revision_reuses_unaffected_query_and_updates_independent_slots(self):
        first = await self.application.start("revised", {"user_request": "查商品",
             "slot_updates": {"product": "Fixture", "region": "US", "budget_max": 50}})
        revised = await self.application.revise("revised", {"expected_revision": 0, "user_request": "写口播脚本"})
        self.assertEqual(revised["status"], "awaiting_confirmation")
        self.assertEqual(revised["reusedQueries"], ["query-1"])
        self.assertEqual(self.tools.calls.count("search"), 1)
        self.assertEqual(first["slotVersions"], revised["slotVersions"])
        self.assertEqual(revised["revision"], 1)
        changed = await self.application.revise("revised", {"expected_revision": 1, "slot_updates": {"region": "CA"}})
        self.assertEqual(changed["slotVersions"]["region"], first["slotVersions"]["region"] + 1)
        self.assertEqual(changed["slotVersions"]["product"], first["slotVersions"]["product"])
        self.assertEqual(changed["slotValues"]["region"], "CA")
        self.assertEqual(self.tools.calls.count("search"), 2)
        with self.assertRaisesRegex(RunConflictError, "revision changed"):
            await self.application.revise("revised", {"expected_revision": 1, "slot_updates": {"region": "GB"}})
        await self.application.confirm("revised", {"confirmed": True, "quote_id": "q-1"})
        with self.assertRaisesRegex(RunConflictError, "cannot be rewritten"):
            await self.application.revise("revised", {"expected_revision": 2, "slot_updates": {"region": "GB"}})

    async def test_retry_queries_retains_successful_independent_results(self):
        attempts = {"US": 0, "ID": 0}
        async def search(query):
            attempts[query["region"]] += 1
            if query["region"] == "ID" and attempts["ID"] == 1:
                raise RuntimeError("controlled failure")
            return {"products": [{"id": query["region"], "title": "Fixture", "region": query["region"]}]}
        self.tools.search_products = search
        first = await self.application.start("failed-query", {"user_request": "查美国和印尼商品"})
        self.assertEqual(first["status"], "retryable")
        retried = await self.application.retry("failed-query")
        self.assertEqual(retried["status"], "completed")
        self.assertEqual(attempts, {"US": 1, "ID": 2})
        self.assertEqual(retried["reusedQueries"], ["query-1"])

    async def test_confirmation_rejects_a_previous_quote_after_revision(self):
        count = 0
        async def quote(request):
            nonlocal count
            count += 1
            return {"quoteId": f"quote-{count}", "amount": 5}
        self.tools.create_quote = quote
        await self.application.start("stale-quote", {"user_request": "写口播预算 50"})
        await self.application.revise("stale-quote", {"expected_revision": 0, "slot_updates": {"budget_max": 60}})
        with self.assertRaisesRegex(RunConflictError, "stale or different quote"):
            await self.application.confirm("stale-quote", {"confirmed": True, "quote_id": "quote-1"})
        self.assertNotIn("submit", self.tools.calls)

    async def test_confirmation_resume_and_repeated_confirmation_are_idempotent(self):
        started = await self.application.start("run-2", {"user_request": "给我露营灯写口播，预算 50"})
        self.assertEqual(started["status"], "awaiting_confirmation")
        self.assertTrue(started["confirmationRequest"])
        finished = await self.application.confirm("run-2", {"confirmed": True, "quote_id": "q-1"})
        replay = await self.application.confirm("run-2", {"confirmed": True, "quote_id": "q-1"})
        self.assertEqual(finished, replay)
        self.assertEqual(self.tools.calls, ["search", "quote", "confirm", "submit", "get"])

    async def test_get_reports_interrupted_checkpoint_without_reopening_external_tools(self):
        await self.application.start("run-3", {"user_request": "给我露营灯写口播，预算 50"})
        result = await self.application.get("run-3")
        self.assertEqual(result["status"], "awaiting_confirmation")
        self.assertTrue(result["confirmationRequest"])
        self.assertEqual(self.tools.calls, ["search", "quote"])

    async def test_concurrent_identical_start_is_serialized_by_run_id(self):
        first, second = await asyncio.gather(
            self.application.start("concurrent", {"user_request": "查商品"}),
            self.application.start("concurrent", {"user_request": "查商品"}),
        )
        self.assertEqual(first, second)
        self.assertEqual(self.tools.calls, ["search"])

    async def test_knowledge_request_without_mcp_reports_missing_configuration(self):
        async def disconnected_tools():
            raise AssertionError("knowledge-only request must not open MCP")
            yield

        self.application.tool_session_factory = disconnected_tools
        result = await self.application.start("knowledge-offline", {"user_request": "从知识库查询采集说明"})
        self.assertEqual(result["status"], "needs_configuration")

    async def test_parallel_queries_share_one_session_and_close_in_its_owner_task(self):
        entered = []
        exited = []

        @asynccontextmanager
        async def tools_session():
            entered.append(asyncio.current_task())
            try:
                yield self.tools
            finally:
                exited.append(asyncio.current_task())

        self.application.tool_session_factory = tools_session
        result = await self.application.start("session-owner", {"user_request": "比较印尼和美国蓝牙耳机"})
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(entered), 1)
        self.assertEqual(entered, exited)
        self.assertEqual(self.tools.calls, ["search", "search"])

    async def test_bi_request_without_mcp_uses_bi_port(self):
        async def disconnected_tools():
            raise AssertionError("BI-only request must not open MCP")
            yield

        class BiQuery:
            async def query(self, question, principal):
                return {"source": "TEST_BI", "rows": [{"count": 1}]}

        self.application.tool_session_factory = disconnected_tools
        self.application.bi_query = BiQuery()
        self.application.bi_principal = "test-role"
        result = await self.application.start("bi-offline", {"user_request": "统计美国最近销量和 GMV 增长排行"})
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["biResult"]["source"], "TEST_BI")

    async def test_retry_resumes_failed_step_without_repeating_successful_submission(self):
        original_get = self.tools.get_task
        failed = False

        async def fail_once(task_id):
            nonlocal failed
            if not failed:
                failed = True
                raise RuntimeError("temporary status failure")
            return await original_get(task_id)

        self.tools.get_task = fail_once
        await self.application.start("retry-1", {"user_request": "给我露营灯写口播，预算 50"})
        with self.assertRaisesRegex(RuntimeError, "temporary status failure"):
            await self.application.confirm("retry-1", {"confirmed": True, "quote_id": "q-1"})
        result = await self.application.retry("retry-1")
        self.assertEqual(result["taskStatus"]["status"], "SUCCEEDED")
        self.assertEqual(self.tools.calls.count("submit"), 1)

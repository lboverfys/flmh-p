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
        return {"confirmed": True}

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
        first = await self.application.confirm("pending", {"confirmed": True})
        self.assertEqual(first["status"], "processing")
        status = "SUCCEEDED"
        result = await self.application.get("pending")
        self.assertEqual(result["taskStatus"]["status"], "SUCCEEDED")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.tools.calls.count("submit"), 1)
        self.assertEqual(self.tools.calls.count("get"), 2)

    async def test_confirmation_resume_and_repeated_confirmation_are_idempotent(self):
        started = await self.application.start("run-2", {"user_request": "给我露营灯写口播，预算 50"})
        self.assertEqual(started["status"], "awaiting_confirmation")
        self.assertTrue(started["confirmationRequest"])
        finished = await self.application.confirm("run-2", {"confirmed": True})
        replay = await self.application.confirm("run-2", {"confirmed": True})
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
            await self.application.confirm("retry-1", {"confirmed": True})
        result = await self.application.retry("retry-1")
        self.assertEqual(result["taskStatus"]["status"], "SUCCEEDED")
        self.assertEqual(self.tools.calls.count("submit"), 1)

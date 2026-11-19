import unittest
from identity_support import IdentityTestCase
import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

from langgraph.checkpoint.memory import InMemorySaver

from flmh_agent.adapters.outbound.coordination import InMemoryRunCoordinator
from flmh_agent.adapters.outbound.sessions import InMemorySessionDirectory
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


class ApplicationTests(IdentityTestCase):
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
            session_directory=InMemorySessionDirectory(),
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
        first = await self.application.start("revised", {"user_request": "查商品并写文案",
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

    async def test_product_price_filter_is_not_reused_as_content_production_budget(self):
        seen = []
        search = self.tools.search_products
        async def record(query):
            seen.append(query)
            return await search(query)
        self.tools.search_products = record
        await self.application.start("price-scope", {"user_request": "查商品",
             "slot_updates": {"product": "Fixture", "region": "US", "budget_max": 50}})
        result = await self.application.revise("price-scope", {"expected_revision": 0, "user_request": "写口播脚本"})
        self.assertEqual(seen[0]["max_price"], 50)
        self.assertIsNone(seen[1]["max_price"])
        self.assertEqual(result["reusedQueries"], [])
        self.assertEqual(result["plan"]["budget_max"], 50)

    async def test_budget_edit_keeps_products_selection_and_brief_then_price_edit_reselects(self):
        self.tools.search_products = AsyncMock(side_effect=lambda q: {"products": [
            {"id": "basic" if q["max_price"] == 8 else "premium", "title": "灯"}]})
        self.tools.create_quote = AsyncMock(side_effect=[{"quoteId": f"q-{i}", "amount": 5, "currency": "USD"}
                                                       for i in range(3)])
        first = await self.application.start("limits", {"user_request": "查美国灯写口播，商品单价100元，创作预算10美元"})
        revised = await self.application.revise("limits", {"expected_revision": 0, "user_request": "预算改为20美元"})
        self.assertEqual(revised["products"], first["products"])
        self.assertEqual(revised["confirmationRequest"]["product"], first["confirmationRequest"]["product"])
        self.assertEqual(revised["contentBrief"], first["contentBrief"])
        for key in ("product_results", "selected_product", "content_brief"):
            self.assertNotIn(key, revised["invalidated"])
            deps = revised["conclusionDependencies"][key]
            self.assertEqual(deps, first["conclusionDependencies"][key])
            self.assertIn("product_max_price", deps)
            self.assertNotIn("budget_max", deps)
        self.assertIn("quote", revised["invalidated"])
        self.assertEqual(revised["slotVersions"], {**first["slotVersions"], "budget_max": first["slotVersions"]["budget_max"] + 1})
        self.assertEqual(revised["reusedQueries"], ["query-1"])
        self.tools.search_products.assert_awaited_once()
        cheaper = await self.application.revise("limits", {"expected_revision": 1,
            "user_request": "商品价格上限改为9元，创作预算改为30美元", "slot_updates": {"product_max_price": 8}})
        self.assertEqual(cheaper["slotValues"], {"product": "灯", "region": "US", "product_max_price": 8, "budget_max": 30})
        self.assertTrue({"product_results", "selected_product", "quote", "confirmation"} <= set(cheaper["invalidated"]))
        self.assertEqual(cheaper["slotVersions"]["product_max_price"], first["slotVersions"]["product_max_price"] + 1)
        self.assertEqual([call.args[0]["max_price"] for call in self.tools.search_products.await_args_list], [100, 8])
        self.assertEqual([(c.args[0]["product_id"], c.args[0]["budget_max"]) for c in self.tools.create_quote.await_args_list],
                         [("premium", 10), ("premium", 20), ("basic", 30)])
        with self.assertRaisesRegex(RunConflictError, "stale"):
            await self.application.confirm("limits", {"quote_id": revised["quote"]["quoteId"], "confirmed": True})

    async def test_selection_only_and_explicit_null_remain_independent(self):
        first = await self.application.start("purchase", {"user_request": "查露营灯，预算50元"})
        self.assertIsNone(first["slotValues"]["budget_max"])
        revised = await self.application.revise("purchase", {"expected_revision": 0, "slot_updates": {"budget_max": 7}})
        self.assertEqual(revised["slotValues"]["product_max_price"], 50)
        self.assertNotIn("selected_product", revised["invalidated"])
        cleared = await self.application.revise("purchase", {"expected_revision": 1, "slot_updates": {"product_max_price": None}})
        self.assertIsNone(cleared["plan"]["queries"][0]["max_price"])
        self.assertEqual(cleared["slotValues"]["budget_max"], 7)
        self.assertEqual(self.tools.calls, ["search", "search"])

    async def test_replanning_a_lookup_does_not_turn_a_later_budget_into_a_price(self):
        await self.application.start("new-lookup", {"user_request": "查美国露营灯"})
        await self.application.revise("new-lookup", {"expected_revision": 0, "slot_updates": {"budget_max": 12}})
        revised = await self.application.revise("new-lookup", {"expected_revision": 1, "user_request": "查印尼背包"})
        self.assertIsNone(revised["plan"]["queries"][0]["max_price"])
        self.assertEqual(revised["slotValues"]["budget_max"], 12)

        form = {"product": "Fixture", "region": "US", "budget_max": 50}
        first = await self.application.start("priced-form", {"user_request": "选品，商品单价30元", "slot_updates": form})
        changed = await self.application.revise("priced-form", {"expected_revision": 0,
            "slot_updates": {**form, "region": "CA", "budget_max": 7}})
        self.assertEqual([r["plan"]["queries"][0]["max_price"] for r in (first, changed)], [30, 30])
        self.assertEqual(changed["slotValues"]["budget_max"], 7)

    async def test_three_slot_checkpoint_replays_revises_and_resumes_old_confirmation(self):
        payload = {"user_request": "查美国灯写口播，商品单价30，预算10"}
        for action in ("confirm", "revise"):
            with self.subTest(action=action):
                first = await self.application.start(action, payload)
                graph = self.application._graph(self.tools)
                config = self.application._config(action)
                old_slots = {k: v for k, v in first["slotValues"].items() if k != "product_max_price"}
                old_versions = {k: v for k, v in first["slotVersions"].items() if k != "product_max_price"}
                old_dependencies = {"product_results": old_versions, "selected_product": old_versions, "quote": old_versions}
                await graph.aupdate_state(config, {"slot_values": old_slots, "slot_versions": old_versions,
                    "conclusion_dependencies": old_dependencies, "product_results_snapshot": ""}, as_node="create_quote")
                await graph.ainvoke(None, config)
                replay = await self.application.start(action, payload)
                self.assertEqual(replay["slotValues"]["product_max_price"], 30)
                self.assertEqual(replay["slotVersions"]["product_max_price"], 0)
                self.assertNotIn("budget_max", replay["conclusionDependencies"]["selected_product"])
                if action == "confirm":
                    done = await self.application.confirm(action, {"quote_id": first["quote"]["quoteId"], "confirmed": True})
                    self.assertEqual(done["conclusionDependencies"]["content_task"]["product_max_price"], 0)
                else:
                    edit = await self.application.revise(action, {"expected_revision": 0, "slot_updates": {"budget_max": 12}})
                    self.assertEqual(edit["reusedQueries"], ["query-1"])
                    self.assertEqual(edit["slotVersions"]["product_max_price"], 0)
                    self.assertNotIn("selected_product", edit["invalidated"])

    async def test_old_checkpoint_without_slot_values_can_retry_submission_with_the_saved_key(self):
        run = "old-submission"
        first = await self.application.start(run, {"user_request": "查美国灯写口播，商品单价30，预算10"})
        quote = first["quote"]["quoteId"]
        submit = AsyncMock(side_effect=[TimeoutError("lost receipt"), {"taskId": "t-1"}])
        with patch.object(self.tools, "submit_task", submit):
            with self.assertRaisesRegex(TimeoutError, "lost receipt"):
                await self.application.confirm(run, {"quote_id": quote, "confirmed": True})
            graph = self.application._graph(self.tools)
            await graph.aupdate_state(self.application._config(run), {"slot_values": {},
                "slot_versions": {k: v for k, v in first["slotVersions"].items() if k != "product_max_price"},
                "conclusion_dependencies": {}}, as_node="confirm_quote")
            retried = await self.application.retry(run)
        self.assertEqual(retried["slotValues"], first["slotValues"])
        self.assertEqual(retried["conclusionDependencies"]["content_task"]["product_max_price"], 0)
        self.assertEqual(submit.await_args_list[0], submit.await_args_list[1])
        self.assertEqual(submit.await_args.args[1], quote)
        self.assertEqual(self.tools.calls.count("confirm"), 1)

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

    async def test_confirm_and_retry_choose_durability_at_the_side_effect_boundary(self):
        # Record the actual ainvoke arguments with a local saver; no database needed for this policy test.
        graph_type = type(self.application._graph(self.tools))
        invoke = graph_type.ainvoke
        modes = []

        async def record(graph, *args, **kwargs):
            modes.append(kwargs.get("durability"))
            return await invoke(graph, *args, **kwargs)

        with patch.object(graph_type, "ainvoke", record):
            for backend in ("postgres", "memory"):
                with self.subTest(backend=backend):
                    self.application.settings = Settings(checkpointer_backend=backend, postgres_checkpointer_uri="postgresql://localhost/test")
                    self.tools.submit_task = AsyncMock(side_effect=[TimeoutError("submit receipt"), {"taskId": "t-1"}])
                    self.tools.get_task = AsyncMock(side_effect=[RuntimeError("read status"), {"status": "SUCCEEDED"}])
                    modes.clear()
                    await self.application.start(backend, {"user_request": "查灯写口播，预算10"})
                    with self.assertRaisesRegex(TimeoutError, "submit receipt"):
                        await self.application.confirm(backend, {"quote_id": "q-1", "confirmed": True})
                    with self.assertRaisesRegex(RuntimeError, "read status"):
                        await self.application.retry(backend)
                    await self.application.retry(backend)
                    sync = "sync" if backend == "postgres" else None
                    self.assertEqual(modes, [None, sync, sync, None])
                    modes.clear()
                    with patch.object(self.tools, "search_products", AsyncMock(side_effect=[TimeoutError(), {"products": []}])):
                        lookup = backend + "-lookup"
                        await self.application.start(lookup, {"user_request": "查露营灯"})
                        await self.application.retry(lookup)
                    self.assertEqual(modes, [None, None])

            self.application.settings = Settings(checkpointer_backend="postgres", postgres_checkpointer_uri="postgresql://localhost/test")
            for run, text, expected in (("plan-lookup", "查灯", [None, None]),
                                        ("plan-production", "查灯写口播，预算10", [None, None, "sync"])):
                modes.clear()
                with patch.object(self.application.planner, "plan", AsyncMock(side_effect=TimeoutError("planner"))):
                    with self.assertRaisesRegex(TimeoutError, "planner"):
                        await self.application.start(run, {"user_request": text})
                result = await self.application.retry(run)
                self.assertEqual(modes, expected)
                self.assertEqual(result["status"], "awaiting_confirmation" if run == "plan-production" else "completed")

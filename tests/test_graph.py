import unittest

from langgraph.types import Command

from flmh_agent.graph import build_agent_graph


class FakeToolClient:
    def __init__(self, quote_amount=20):
        self.calls = []
        self.quote_amount = quote_amount

    async def call(self, name, arguments):
        self.calls.append((name, arguments))
        if name == "search_products":
            return {"source": "DEMO", "products": [{"id": "p-1", "title": "Demo 耳机", "region": "ID"}]}
        if name == "create_content_quote":
            return {"quoteId": "q-1", "amount": self.quote_amount, "currency": "CNY", "source": "MOCK"}
        if name == "confirm_content_quote":
            return {"quoteId": "q-1", "confirmed": True}
        if name == "create_content_task":
            return {"taskId": "t-1", "status": "submitted", "source": "MOCK"}
        if name == "get_content_task":
            return {"taskId": "t-1", "status": "submitted", "source": "MOCK"}
        raise AssertionError(f"unexpected MCP tool: {name}")


class GraphFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_task_creation_waits_for_explicit_confirmation(self):
        client = FakeToolClient()
        graph = build_agent_graph(tool_client=client)
        config = {"configurable": {"thread_id": "confirm-flow"}}
        request = {"user_request": "给我蓝牙耳机写口播脚本，预算不超过 50"}

        paused = await graph.ainvoke(request, config=config)
        self.assertIn("__interrupt__", paused)
        self.assertIsInstance(client.calls[1][1]["brief"], str)
        self.assertIsInstance(paused["content_brief"], dict)
        self.assertIsInstance(paused["__interrupt__"][0].value["contentBrief"], dict)
        self.assertEqual(
            [name for name, _ in client.calls],
            ["search_products", "create_content_quote"],
        )

        finished = await graph.ainvoke(Command(resume={"confirmed": False}), config=config)
        self.assertEqual(
            [name for name, _ in client.calls],
            ["search_products", "create_content_quote"],
        )
        self.assertIn("未创建", finished["summary"])

    async def test_confirmed_flow_confirms_then_creates_and_reads_status(self):
        client = FakeToolClient()
        graph = build_agent_graph(tool_client=client)
        config = {"configurable": {"thread_id": "confirmed-flow"}}
        request = {"user_request": "给我蓝牙耳机写口播脚本，预算不超过 50"}

        paused = await graph.ainvoke(request, config=config)
        self.assertIn("__interrupt__", paused)
        finished = await graph.ainvoke(Command(resume={"confirmed": True}), config=config)

        self.assertEqual(
            [name for name, _ in client.calls],
            [
                "search_products",
                "create_content_quote",
                "confirm_content_quote",
                "create_content_task",
                "get_content_task",
            ],
        )
        self.assertEqual(finished["content_task"]["taskId"], "t-1")
        self.assertEqual(finished["task_status"]["status"], "submitted")
        self.assertIn("MOCK", finished["summary"])
        create_args = next(args for name, args in client.calls if name == "create_content_task")
        self.assertIsInstance(create_args["brief"], str)
        first_key = create_args["idempotency_key"]

        repeated = FakeToolClient()
        repeated_graph = build_agent_graph(tool_client=repeated)
        repeated_config = {"configurable": {"thread_id": "same-request-retry"}}
        await repeated_graph.ainvoke(request, config=repeated_config)
        await repeated_graph.ainvoke(Command(resume={"confirmed": True}), config=repeated_config)
        second_key = next(args["idempotency_key"] for name, args in repeated.calls if name == "create_content_task")
        self.assertEqual(first_key, second_key)

    async def test_missing_budget_is_passed_as_null(self):
        client = FakeToolClient()
        graph = build_agent_graph(tool_client=client)
        config = {"configurable": {"thread_id": "no-budget"}}
        result = await graph.ainvoke(
            {"user_request": "给我蓝牙耳机写口播脚本"},
            config=config,
        )
        quote_args = next(args for name, args in client.calls if name == "create_content_quote")
        self.assertIsNone(quote_args["budget_max"])
        self.assertIn("__interrupt__", result)

    async def test_quote_over_budget_stops_before_confirmation(self):
        client = FakeToolClient(quote_amount=51)
        graph = build_agent_graph(tool_client=client)
        config = {"configurable": {"thread_id": "over-budget"}}
        result = await graph.ainvoke(
            {"user_request": "给我蓝牙耳机写口播脚本，预算不超过 50"},
            config=config,
        )
        self.assertNotIn("__interrupt__", result)
        self.assertIn("超出预算", result["summary"])
        self.assertEqual(
            [name for name, _ in client.calls],
            ["search_products", "create_content_quote"],
        )

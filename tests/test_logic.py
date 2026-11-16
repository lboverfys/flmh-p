import asyncio
import unittest

from flmh_agent.logic import (
    confirmation_is_explicit,
    execute_product_queries,
    quote_within_budget,
    stable_idempotency_key,
)


class LogicTests(unittest.TestCase):
    def test_independent_product_queries_execute_concurrently(self):
        async def scenario():
            active = 0
            maximum_active = 0
            both_started = asyncio.Event()

            async def call_tool(name, arguments):
                nonlocal active, maximum_active
                self.assertEqual(name, "search_products")
                active += 1
                maximum_active = max(maximum_active, active)
                if active == 2:
                    both_started.set()
                await asyncio.wait_for(both_started.wait(), timeout=1)
                active -= 1
                return {"region": arguments["region"]}

            results = await execute_product_queries(
                [{"region": "ID"}, {"region": "US"}],
                call_tool,
            )
            self.assertEqual({item["region"] for item in results}, {"ID", "US"})
            self.assertEqual(maximum_active, 2)

        asyncio.run(scenario())

    def test_idempotency_key_is_stable_and_changes_with_input(self):
        common = {
            "user_request": "  写一段   口播 ",
            "product_id": " p-1 ",
            "content_type": "product_script",
            "budget_max": 50.0,
            "region": "id",
        }
        first = stable_idempotency_key(**common)
        self.assertEqual(first, stable_idempotency_key(**{**common, "user_request": "写一段 口播"}))
        self.assertNotEqual(first, stable_idempotency_key(**{**common, "product_id": "p-2"}))

    def test_quote_budget_requires_a_numeric_amount_when_limited(self):
        self.assertTrue(quote_within_budget({"amount": 20}, 20))
        self.assertFalse(quote_within_budget({"amount": 20.01}, 20))
        with self.assertRaisesRegex(ValueError, "numeric amount"):
            quote_within_budget({}, 20)
        with self.assertRaisesRegex(ValueError, "numeric amount"):
            quote_within_budget({}, None)

    def test_confirmation_requires_a_boolean_true_response(self):
        self.assertTrue(confirmation_is_explicit({"confirmed": True}))
        self.assertFalse(confirmation_is_explicit({"confirmed": "yes"}))
        self.assertFalse(confirmation_is_explicit({"confirmed": 1}))


if __name__ == "__main__":
    unittest.main()

import unittest
import math

from flmh_agent.planner import build_demo_plan, validate_plan


class PlannerTests(unittest.TestCase):
    def test_demo_plans_independent_region_queries_and_content(self):
        plan = build_demo_plan("比较印尼和美国蓝牙耳机，预算不超过 200，写口播脚本")
        self.assertEqual(plan["mode"], "MOCK_RULES")
        self.assertEqual([query["region"] for query in plan["queries"]], ["ID", "US"])
        self.assertEqual(plan["queries"][0]["keyword"], "蓝牙耳机")
        self.assertEqual(plan["budget_max"], 200.0)
        self.assertTrue(plan["create_content"])
        validate_plan(plan)

    def test_plan_does_not_claim_an_llm(self):
        plan = build_demo_plan("查印尼收纳用品")
        self.assertFalse(plan["create_content"])
        self.assertIn("未调用 LLM", plan["summary"])

    def test_plan_rejects_unknown_mode_and_unbounded_region_count(self):
        plan = build_demo_plan("查印尼商品")
        plan["mode"] = "REAL_LLM"
        with self.assertRaisesRegex(ValueError, "MOCK_RULES"):
            validate_plan(plan)

        with self.assertRaisesRegex(ValueError, "at most"):
            build_demo_plan("比较印尼、美国、越南、泰国和英国商品")

    def test_plan_rejects_unknown_top_level_and_query_fields(self):
        plan = build_demo_plan("查印尼耳机")
        plan["unexpected"] = "value"
        with self.assertRaisesRegex(ValueError, "unknown plan fields"):
            validate_plan(plan)

        plan = build_demo_plan("查印尼耳机")
        plan["queries"][0]["limit"] = 1000
        with self.assertRaisesRegex(ValueError, "unknown product query fields"):
            validate_plan(plan)

    def test_query_types_are_bounded_and_region_must_be_uppercase_iso_shape(self):
        invalid_queries = [
            {"region": "id"},
            {"region": 12},
            {"region": "ID", "keyword": 123},
            {"region": "ID", "keyword": "x" * 81},
            {"region": "ID", "category": "x" * 101},
            {"region": "ID", "max_price": True},
            {"region": "ID", "max_price": math.inf},
            {"region": "ID", "max_price": -1},
        ]
        for query in invalid_queries:
            with self.subTest(query=query):
                plan = build_demo_plan("查印尼耳机")
                plan["queries"] = [query]
                with self.assertRaises(ValueError):
                    validate_plan(plan)

    def test_budget_content_fields_require_exact_types_and_demo_content_type(self):
        for budget in (True, "20", math.nan, math.inf, -1):
            with self.subTest(budget=budget):
                plan = build_demo_plan("查印尼耳机")
                plan["budget_max"] = budget
                with self.assertRaisesRegex(ValueError, "budget_max"):
                    validate_plan(plan)

        plan = build_demo_plan("查印尼耳机")
        plan["create_content"] = 1
        with self.assertRaisesRegex(ValueError, "create_content"):
            validate_plan(plan)

        plan = build_demo_plan("写口播脚本")
        plan["content_type"] = "other"
        with self.assertRaisesRegex(ValueError, "content type"):
            validate_plan(plan)

    def test_product_slot_is_passed_as_keyword_for_id_or_name_lookup(self):
        plan = build_demo_plan("查印尼商品", {"product": "product-123"})
        self.assertEqual(plan["queries"][0]["keyword"], "product-123")


if __name__ == "__main__":
    unittest.main()

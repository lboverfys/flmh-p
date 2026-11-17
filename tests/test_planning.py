import unittest

from flmh_agent.domain.planning import RuleBasedPlanner
from flmh_agent.domain.slots import update_slots


class PlanningTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_planner_parses_product_content_budget_and_region(self):
        plan = await RuleBasedPlanner().plan(
            "请帮我在美国查露营灯并写口播脚本，预算不超过 30", {}
        )
        self.assertEqual(plan.source, "RULES")
        self.assertEqual(plan.queries[0].region, "US")
        self.assertEqual(plan.queries[0].keyword, "露营灯")
        self.assertEqual(plan.budget_max, 30)
        self.assertTrue(plan.create_content)

    async def test_independent_region_queries_and_bi_intent(self):
        planner = RuleBasedPlanner()
        products = await planner.plan("比较印尼和美国蓝牙耳机", {})
        self.assertEqual([query.region for query in products.queries], ["ID", "US"])
        bi = await planner.plan("统计美国最近销量和 GMV 增长排行", {})
        self.assertEqual(bi.analysis_question, "统计美国最近销量和 GMV 增长排行")
        self.assertEqual(bi.queries, [])

    async def test_slots_reject_unknown_names_and_normalize_region(self):
        slots, invalidated = update_slots({"region": "ID"}, {"region": "us"})
        self.assertEqual(slots["region"], "US")
        self.assertIn("quote", invalidated)
        with self.assertRaisesRegex(ValueError, "unknown slot"):
            update_slots({}, {"tenant": "x"})

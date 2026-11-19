import unittest

from flmh_agent.domain.planning import RuleBasedPlanner
from flmh_agent.domain.slots import update_slots
from flmh_agent.domain.models import AgentPlan, ProductQuery


class PlanningTests(unittest.IsolatedAsyncioTestCase):
    async def test_query_dependencies_reject_cycles_unknown_and_duplicate_ids(self):
        base = {"summary": "DAG", "create_content": False, "content_type": "", "source": "RULES"}
        for queries in (
            [ProductQuery(region="US", query_id="a", depends_on=["b"]), ProductQuery(region="CA", query_id="b", depends_on=["a"])],
            [ProductQuery(region="US", query_id="a", depends_on=["missing"])],
            [ProductQuery(region="US", query_id="a"), ProductQuery(region="CA", query_id="a")],
        ):
            with self.assertRaises(ValueError):
                AgentPlan(**base, queries=queries)
    async def test_local_planner_parses_product_content_budget_and_region(self):
        plan = await RuleBasedPlanner().plan(
            "请帮我在美国查露营灯并写口播脚本，预算不超过 30", {}
        )
        self.assertEqual(plan.source, "RULES")
        self.assertEqual(plan.queries[0].region, "US")
        self.assertEqual(plan.queries[0].keyword, "露营灯")
        self.assertEqual(plan.budget_max, 30)
        self.assertTrue(plan.create_content)

    async def test_purchase_and_creation_limits_are_scoped_by_intent_and_label(self):
        planner = RuleBasedPlanner()
        for text in ("查美国露营灯，预算不超过50元", "查美国露营灯，不高于50", "查美国露营灯，商品限价50"):
            with self.subTest(text=text):
                plan = await planner.plan(text, {})
                self.assertFalse(plan.create_content)
                self.assertIsNone(plan.budget_max)
                self.assertIsNone(plan.budget_currency)
                self.assertEqual((plan.queries[0].keyword, plan.queries[0].max_price), ("露营灯", 50))
        for fields in ("商品价格不超过100元，创作预算上限10美元", "内容预算10美元，商品单价最多100元",
                       "商品预算100元，制作预算10美元"):
            with self.subTest(fields=fields):
                plan = await planner.plan("查美国露营灯并写口播，" + fields, {})
                self.assertEqual((plan.budget_max, plan.budget_currency), (10, "USD"))
                self.assertEqual((plan.queries[0].keyword, plan.queries[0].max_price), ("露营灯", 100))
        plan = await planner.plan("查露营灯写口播，口播不超过100字", {})
        self.assertIsNone(plan.queries[0].max_price)
        self.assertIsNone(plan.budget_max)

    async def test_regional_price_limits_and_explicit_null(self):
        request = "查美国露营灯单价10元，印尼帐篷单价20元，写口播，创作预算30美元"
        plan = await RuleBasedPlanner().plan(request, {})
        self.assertEqual([(q.region, q.keyword, q.max_price) for q in plan.queries], [("US", "露营灯", 10), ("ID", "帐篷", 20)])
        self.assertEqual((plan.budget_max, plan.budget_currency), (30, "USD"))
        cleared = await RuleBasedPlanner().plan(request, {"product_max_price": None, "budget_max": 5})
        self.assertEqual([q.max_price for q in cleared.queries], [None, None])
        self.assertEqual(cleared.budget_max, 5)
        joined = await RuleBasedPlanner().plan("查美国灯限价10元、印尼帐篷限价20元", {})
        self.assertEqual([(q.region, q.keyword, q.max_price) for q in joined.queries], [("US", "灯", 10), ("ID", "帐篷", 20)])
        mixed = await RuleBasedPlanner().plan("查美国商品，预算10元、印尼帐篷限价20元，创作预算30美元", {})
        self.assertEqual([(q.keyword, q.max_price) for q in mixed.queries], [(None, 10), ("帐篷", 20)])
        self.assertEqual((mixed.budget_max, mixed.budget_currency), (30, "USD"))
        shared = await RuleBasedPlanner().plan("查美国和印尼露营灯，限价15元", {})
        self.assertEqual([(q.keyword, q.max_price) for q in shared.queries], [("露营灯", 15)] * 2)
        narrowed = await RuleBasedPlanner().plan(request, {"region": "ID"})
        self.assertEqual([(q.keyword, q.max_price) for q in narrowed.queries], [("帐篷", 20)])

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
        with self.assertRaisesRegex(ValueError, "at most 80"):
            update_slots({}, {"product": "x" * 81})

    def test_only_price_invalidates_selection_and_both_amounts_validate(self):
        slots, affected = update_slots({"product_max_price": 0}, {"budget_max": 10})
        self.assertEqual(slots["product_max_price"], 0)
        for name in ("product_results", "selected_product", "content_brief", "product_history"):
            self.assertNotIn(name, affected)
        self.assertTrue({"quote", "confirmation", "content_task"} <= set(affected))
        _, affected = update_slots(slots, {"product_max_price": None})
        self.assertTrue({"product_results", "selected_product", "quote"} <= set(affected))
        self.assertNotIn("product_history", affected)
        for field in ("product_max_price", "budget_max"):
            for invalid in (True, -1, "10", float("inf"), float("nan")):
                with self.subTest(field=field, invalid=invalid), self.assertRaisesRegex(ValueError, field):
                    update_slots({}, {field: invalid})

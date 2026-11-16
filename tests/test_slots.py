import unittest
import math

from flmh_agent.slots import update_slots


class SlotTests(unittest.TestCase):
    def test_region_update_invalidates_results_and_downstream_task_state(self):
        slots, invalidated = update_slots(
            {"product": "耳机", "region": "ID", "budget_max": 200},
            {"region": "US"},
        )
        self.assertEqual(slots["region"], "US")
        self.assertIn("product_results", invalidated)
        self.assertIn("quote", invalidated)
        self.assertIn("content_task", invalidated)

    def test_unchanged_slot_does_not_invalidate(self):
        slots, invalidated = update_slots({"region": "ID"}, {"region": "id"})
        self.assertEqual(slots["region"], "ID")
        self.assertEqual(invalidated, [])

    def test_negative_budget_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "negative"):
            update_slots({}, {"budget_max": -1})

    def test_budget_slot_rejects_booleans_strings_and_non_finite_values(self):
        for value in (True, "20", math.nan, math.inf):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "finite non-negative number"):
                    update_slots({}, {"budget_max": value})


if __name__ == "__main__":
    unittest.main()

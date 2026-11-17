import unittest

from flmh_agent.evaluate import Case, Judgments, Rankings, score


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.gold = Judgments(dataset="v1", cases=[Case(id="q1", question="first", relevant=["a", "b"]),
                                                   Case(id="q2", question="second", relevant=["d"])])

    def run_data(self, rankings, dataset="v1"):
        return Rankings(dataset=dataset, name="sample", source="SCRIPTED_FIXTURE", rankings=rankings)

    def test_partial_relevance_and_empty_answer_stay_in_macro_denominator(self):
        result = score(self.gold, self.run_data({"q1": ["c", "b", "a"], "q2": []}), 2)
        self.assertEqual((result["recall@2"], result["mrr"], result["queries"]), (0.25, 0.25, 2))
        self.assertEqual(result["source"], "SCRIPTED_FIXTURE")

    def test_mrr_uses_full_supplied_list_even_if_hit_is_below_k(self):
        result = score(self.gold, self.run_data({"q1": [], "q2": ["x", "d"]}), 1)
        self.assertEqual((result["recall@1"], result["mrr"]), (0, 0.25))

    def test_rankings_cannot_drop_queries_or_double_count_hits(self):
        for rows, version in [({"q1": []}, "v1"), ({"q1": [], "x": []}, "v1"),
                              ({"q1": ["a", "a"], "q2": []}, "v1"), ({"q1": [], "q2": []}, "v2")]:
            with self.subTest(rankings=rows, dataset=version), self.assertRaises(ValueError):
                score(self.gold, self.run_data(rows, version))

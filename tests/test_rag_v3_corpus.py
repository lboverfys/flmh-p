import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from flmh_agent.application.rag import LocalCorpusFallbackKnowledgeService
from flmh_agent.evaluate import Case, Judgments, Rankings, RefusalRecording, RefusalRun, UnanswerableJudgments, score, score_refusals
from flmh_agent.rag_corpus import (LAYOUT, Manifest, Review, UnanswerableReview, freeze_review,
                                   freeze_unanswerable_review, load_corpus, review_cases,
                                   review_unanswerable_cases, sha256)

ROOT = Path(__file__).resolve().parents[1]
V3_DIR = ROOT / "examples" / "rag-evaluation" / "operations-v3"


class RagV3CorpusTests(unittest.TestCase):
    def test_v3_manifest_chunks_and_pending_candidates_satisfy_invariants(self) -> None:
        corpus = load_corpus(ROOT, V3_DIR / "sources.json")
        self.assertEqual(len(corpus.documents), 10)
        self.assertGreaterEqual(len(corpus.parents), 300)
        self.assertGreaterEqual(len(corpus.children), 500)

        review = Review.model_validate_json((V3_DIR / "candidates.json").read_text(encoding="utf-8"))
        self.assertEqual(review.dataset, "flmh-radar-ops-v3")
        self.assertEqual(review.reviewer, "")
        self.assertEqual(len(review.cases), 300)
        self.assertTrue(all(case.status == "pending" for case in review.cases))

        per_doc: dict[str, int] = {}
        for case in review.cases:
            doc = case.evidence[0].document
            per_doc[doc] = per_doc.get(doc, 0) + 1
        self.assertEqual(len(per_doc), 10)
        self.assertTrue(all(count == 30 for count in per_doc.values()))

        cases = review_cases(corpus, review, freeze=False)
        self.assertEqual(len(cases), 300)

        rows = [
            json.loads(line)
            for line in (V3_DIR / "chunks.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        chunk_ids = {row["id"] for row in rows}
        self.assertEqual(chunk_ids, set(corpus.parents) | set(corpus.children))

    def test_questions_do_not_reveal_titles_and_cross_section_evidence_is_required(self):
        corpus = load_corpus(ROOT, V3_DIR / "sources.json")
        review = Review.model_validate_json((V3_DIR / "candidates.json").read_bytes())
        titles = {re.sub(r"^###\s*\d+(?:\.\d+)*\s*", "", p.text.splitlines()[0]).strip() for p in corpus.parents.values()}
        books = {doc.lines[0].lstrip("# ").split("（")[0].strip() for doc in corpus.documents.values()}
        labels = review_cases(corpus, review, freeze=False)
        for item in review.cases:
            self.assertFalse(any(t in item.question for t in titles | books if t), item.id)
            self.assertNotRegex(item.question, "[《》「」]")
        self.assertGreaterEqual(sum(len(case.required_groups()) >= 2 for case in labels), 2)
        self.assertTrue(any("FBT" in case.question for case in labels))
        self.assertTrue(any("COD" in case.question for case in labels))
        self.assertTrue(any(re.search(r"\d", case.question) for case in labels))

    def test_unanswerable_candidates_remain_pending_until_human_review(self):
        corpus = load_corpus(ROOT, V3_DIR / "sources.json")
        review = UnanswerableReview.model_validate_json((V3_DIR / "unanswerable-candidates.json").read_bytes())
        self.assertEqual(review.dataset, "flmh-radar-ops-v3-unanswerable")
        self.assertEqual(review.corpus_sha256, corpus.version)
        self.assertEqual(review.reviewer, "")
        self.assertGreaterEqual(len(review.cases), 20)
        self.assertLessEqual(len(review.cases), 30)
        self.assertTrue(all(case.status == "pending" and case.reason.strip() for case in review.cases))
        self.assertEqual(review_unanswerable_cases(corpus, review, freeze=False), [])
        with self.assertRaisesRegex(ValueError, "named reviewer and a decision"):
            freeze_unanswerable_review(corpus, review)
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "unanswerable-judgments.json"
            command = [sys.executable, "-B", "-m", "flmh_agent.rag_benchmark", "freeze-unanswerable",
                       "--source-root", str(ROOT), "--manifest", str(V3_DIR / "sources.json"),
                       "--review", str(V3_DIR / "unanswerable-candidates.json"), "--out", str(output)]
            result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("named reviewer and a decision", result.stderr)
            self.assertFalse(output.exists())
        approved = review.model_copy(update={
            "reviewer": "human-reviewer",
            "cases": [case.model_copy(update={"status": "approved"}) for case in review.cases],
        })
        frozen = freeze_unanswerable_review(corpus, approved)
        self.assertEqual(len(frozen.cases), len(review.cases))
        self.assertEqual(frozen.reviewer, "human-reviewer")

    def test_refusal_metrics_separate_correct_refusal_false_answer_and_false_refusal(self):
        corpus = load_corpus(ROOT, V3_DIR / "sources.json")
        unanswerable = UnanswerableJudgments.model_validate({
            "dataset": "unit-unanswerable", "corpus_sha256": corpus.version, "reviewer": "reviewer",
            "cases": [
                {"id": "u1", "question": "unsupported one", "reason": "outside corpus"},
                {"id": "u2", "question": "unsupported two", "reason": "outside corpus"},
            ],
        })
        positive = Judgments.model_validate({
            "dataset": "unit-positive", "corpus_sha256": corpus.version, "reviewer": "reviewer",
            "granularity": "parent", "cases": [{"id": "p1", "question": "supported", "relevant": ["parent-1"]}],
        })
        run = RefusalRun.model_validate({
            "name": "fixture", "source": "SCRIPTED_FIXTURE", "unanswerable_dataset": unanswerable.dataset,
            "unanswerable_refused": {"u1": True, "u2": False},
            "positive_dataset": positive.dataset, "positive_refused": {"p1": True},
        })
        with self.assertRaises(ValidationError):
            RefusalRun.model_validate({
                "name": "unrecorded", "source": "RECORDED_REFUSAL_EVALUATION",
                "unanswerable_dataset": unanswerable.dataset,
                "unanswerable_refused": {"u1": True, "u2": True},
            })
        recording = RefusalRecording.model_validate({
            "corpus_sha256": corpus.version,
            "unanswerable_judgments_sha256": unanswerable.fingerprint(),
            "positive_judgments_sha256": positive.fingerprint(),
            "raw_evidence_sha256": "a" * 64,
            "recorded_at_utc": "2026-11-25T00:00:00Z",
        })
        recorded = run.model_copy(update={"source": "RECORDED_REFUSAL_EVALUATION", "recording": recording})
        report = score_refusals(unanswerable, recorded, positive)
        self.assertEqual(report["recording"]["raw_evidence_sha256"], "a" * 64)
        mismatched_recording = recording.model_copy(update={"unanswerable_judgments_sha256": "b" * 64})
        with self.assertRaisesRegex(ValueError, "does not match the corpus or unanswerable judgments"):
            score_refusals(unanswerable, recorded.model_copy(update={"recording": mismatched_recording}), positive)
        self.assertEqual((report["refusal_rate"], report["false_positive_rate"]), (0.5, 0.5))
        self.assertEqual([case["outcome"] for case in report["cases"]], ["CORRECT_REFUSAL", "FALSE_ANSWER"])
        self.assertEqual(report["positive"]["false_refusal_rate"], 1)
        self.assertEqual(report["positive"]["cases"][0]["outcome"], "FALSE_REFUSAL")
        with tempfile.TemporaryDirectory() as folder:
            labels_path = Path(folder) / "unanswerable.json"
            run_path = Path(folder) / "refusal-run.json"
            positive_path = Path(folder) / "positive.json"
            labels_path.write_text(unanswerable.model_dump_json(), encoding="utf-8")
            run_path.write_text(run.model_dump_json(), encoding="utf-8")
            positive_path.write_text(positive.model_dump_json(), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, "-B", "-m", "flmh_agent.evaluate", "--unanswerable-judgments", str(labels_path),
                 "--refusal-run", str(run_path), "--positive-judgments", str(positive_path)], cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["unanswerable_refusal"]["false_positive_rate"], 0.5)
        with self.assertRaisesRegex(ValueError, "exactly one refusal decision"):
            score_refusals(unanswerable, run.model_copy(update={"unanswerable_refused": {"u1": True}}))

    def test_v3_benchmark_reports_at_least_ten_cross_parent_questions(self):
        corpus = load_corpus(ROOT, V3_DIR / "sources.json")
        review = Review.model_validate_json((V3_DIR / "candidates.json").read_bytes())
        labels = review_cases(corpus, review, freeze=False)
        self.assertEqual(len(labels), 300)
        self.assertGreaterEqual(sum(len(case.required_groups()) >= 2 for case in labels), 10)
        self.assertEqual(review.reviewer, "")
        self.assertTrue(all(case.status == "pending" for case in review.cases))
    def test_min_approved_checks_count_before_emitting_any_frozen_file(self):
        corpus = load_corpus(ROOT, V3_DIR / "sources.json")
        review = Review.model_validate_json((V3_DIR / "candidates.json").read_bytes())
        one = review.cases[0].model_copy(update={"status": "approved"})
        with self.assertRaisesRegex(ValueError, "300 approved cases; found 1"):
            freeze_review(corpus, review.model_copy(update={"cases": [one]}))
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "judgments.json"
            command = [sys.executable, "-B", "-m", "flmh_agent.rag_benchmark", "freeze", "--source-root", str(ROOT),
                       "--manifest", str(V3_DIR / "sources.json"), "--review", str(V3_DIR / "candidates.json"),
                       "--min-approved", "300", "--out", str(output)]
            result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("300 approved cases; found 0", result.stderr)
            self.assertFalse(output.exists())

    def test_equivalent_parents_required_evidence_and_mrr_cutoffs(self):
        run = Rankings(dataset="unit", name="local", source="SCRIPTED_FIXTURE", rankings={"q": ["unrelated", "b", "c"]})
        case = Case(id="q", question="需哪些证据？", relevant=["a", "b", "c"], equivalent_groups=[["a", "b"]])
        labels = Judgments(dataset="unit", cases=[case])
        first, second, third = [score(labels, run, k) for k in (1, 2, 3)]
        self.assertEqual((first["mrr_depth"], first["mrr"], first["mrr_at_k"]), (3, 0.5, 0))
        self.assertEqual((second["recall@2"], second["mrr_at_k"], second["all_required_at_k"]), (0.5, 0.5, 0))
        self.assertEqual((third["recall@3"], third["all_required_at_k"]), (1, 1))
        must = Judgments(dataset="unit", cases=[case.model_copy(update={"equivalent_groups": []})])
        self.assertEqual(score(must, run, 3)["recall@3"], 2 / 3)

    def test_historical_manifest_serialization_matches_pinned_v1_and_v2(self):
        for release, expected in [("operations-v1", "a6a267ff1a0ffc5c037a6a48a0a631409682507513fc677071ea3889b08092dd"),
                                  ("operations-v2", "a0e8e6d011dd82ad5e4f288bfc7d536c281da7d77c568b2d345b36de2749e3fc")]:
            manifest = Manifest.model_validate_json((V3_DIR.parent / release / "sources.json").read_bytes())
            self.assertEqual(sha256(LAYOUT + "\n" + manifest.model_dump_json(exclude_none=True)), expected)

    def test_local_corpus_fallback_resolves_v3_and_recalls_domain_queries(self) -> None:
        service = LocalCorpusFallbackKnowledgeService(limit=3)
        resolved = service._resolve_corpus_path()
        self.assertEqual(resolved, V3_DIR / "chunks.jsonl")

        for query in ("选品雷达", "美区 TikTok Shop CPC", "图生视频 9:16"):
            chunks = service.search_chunks(query, limit=3)
            self.assertGreaterEqual(len(chunks), 1, f"expected recall for query: {query}")
            self.assertTrue(all(not chunk.parent_id for chunk in chunks))


if __name__ == "__main__":
    unittest.main()

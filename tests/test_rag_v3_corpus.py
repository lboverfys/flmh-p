import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from flmh_agent.application.rag import LocalCorpusFallbackKnowledgeService
from flmh_agent.evaluate import Case, Judgments, Rankings, score
from flmh_agent.rag_corpus import LAYOUT, Manifest, Review, freeze_review, load_corpus, review_cases, sha256

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

import json
import unittest
from pathlib import Path

from flmh_agent.application.rag import LocalCorpusFallbackKnowledgeService
from flmh_agent.rag_corpus import Review, load_corpus, review_cases

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

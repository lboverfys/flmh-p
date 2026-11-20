"""Local contracts using invented unit-test text. No recorded benchmark files are fixtures."""
import io
import json
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock

import httpx

from flmh_agent.adapters.outbound.rag_benchmark_store import BenchmarkStore
from flmh_agent.config import Settings
from flmh_agent.evaluate import paired_report
from flmh_agent.rag_benchmark import new_bundle, prepare, record_pair
from flmh_agent.rag_corpus import LAYOUT, Candidate, Evidence, Manifest, Review, freeze_review, load_corpus, sha256


class BenchmarkTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / "docs").mkdir()
        self.source = self.root / "docs/unit.md"
        text = "# one\n" + "x" * 800 + "\n证据甲\n# two\n证据乙\n"
        self.source.write_bytes(text.replace("\n", "\r\n").encode())
        self.manifest = self.root / "manifest.json"
        self.manifest.write_text(json.dumps({"dataset": "unit", "revision": "0" * 40, "documents": [
            {"id": "unit", "path": "docs/unit.md", "sha256": sha256(text), "sections": [[1, 3], [4, 5]]}]}), encoding="utf-8")
        self.corpus = load_corpus(self.root, self.manifest)
        (self.a, children_a), (self.b, children_b) = self.corpus.documents["unit"].groups
        self.child_a, self.child_b = children_a[1], children_b[0]
        evidence = Evidence(document="unit", line=3, quote="证据甲", parent=self.a.chunk_id, child=self.child_a.chunk_id)
        self.review = Review(dataset="unit", corpus_sha256=self.corpus.version, reviewer="unit-reviewer", cases=[
            Candidate(id=str(i), question=f"unit {i}", status="approved", evidence=[evidence]) for i in range(50)])

    def test_freeze_requires_decisions_and_exact_evidence_inside_the_cited_child(self):
        self.review.cases[0].status = "pending"
        with self.assertRaisesRegex(ValueError, "decision"):
            freeze_review(self.corpus, self.review)
        self.review.cases[0].status = "rejected"
        self.assertEqual(len(freeze_review(self.corpus, self.review).cases), 49)
        self.review.cases[0].status = "approved"
        self.assertEqual(freeze_review(self.corpus, self.review).cases[0].relevant, [self.a.chunk_id])
        self.review.cases[0].evidence[0].child = self.a.chunk_id + "-c0"
        with self.assertRaisesRegex(ValueError, "does not cover"):
            freeze_review(self.corpus, self.review)

    def test_rejects_source_drift_and_keeps_existing_or_failed_outputs_separate(self):
        directory = self.root / "review"
        prepare(self.corpus, self.review, directory)
        rows = [json.loads(line) for line in (directory / "chunks.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertIn(self.child_a.chunk_id, [row["id"] for row in rows if row["parent_id"] == self.a.chunk_id])
        with self.assertRaises(FileExistsError):
            prepare(self.corpus, self.review, directory)
        with self.assertRaisesRegex(RuntimeError, "partial"):
            with new_bundle(self.root / "aborted") as stage:
                (stage / "trace.jsonl").touch()
                raise RuntimeError("partial")
        self.assertFalse((self.root / "aborted").exists())
        self.source.write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "digest mismatch"):
            load_corpus(self.root, self.manifest)

    async def test_one_snapshot_shared_vectors_parent_rankings_and_paired_case_extraction(self):
        gold = freeze_review(self.corpus, self.review)
        published, embedder, reranker = AsyncMock(), AsyncMock(), AsyncMock()
        embedder.encode_many.side_effect = lambda texts: [([float(q.split()[1])], {"term": 1.0}) for q in texts]
        a, b = self.child_a, self.child_b

        def recall(vectors, k, depth):
            self.assertEqual((k, depth), (1, 2))
            rankings = {0: ([b], [b], [a]), 2: ([a], [b, a], [b])}
            return [rankings.get(vector[0], ([b], [b], [b])) for vector, _ in vectors]

        published.search_comparison_batch.side_effect = recall
        published.get_parents.side_effect = lambda ids: [self.corpus.parents[key] for key in reversed(ids)]
        reranker.rerank.side_effect = lambda q, chunks, limit: (sorted(chunks, key=lambda c: c.chunk_id != a.chunk_id)
                                                                if q == "unit 0" else chunks)[:limit]

        @asynccontextmanager
        async def snapshot(corpus):
            yield published, ["unit-index"], "1" * 64

        store = Mock(settings=Settings(bge_m3_model="unit-encoder", embedding_dimension=1, bge_reranker_model="unit-reranker"))
        store.corpus_snapshot.side_effect = snapshot
        trace = io.StringIO()
        dense, dense_rerank, hybrid = await record_pair(
            self.corpus, gold, store, embedder, reranker, trace, k=1, multiplier=2, batch_size=8)
        store.corpus_snapshot.assert_called_once_with(self.corpus)
        self.assertEqual([len(call.args[0]) for call in embedder.encode_many.await_args_list], [8] * 6 + [2])
        self.assertEqual((published.search_comparison_batch.await_count, published.get_parents.await_count), (7, 7))
        self.assertEqual([run.name for run in (dense, dense_rerank, hybrid)], ["dense-only", "dense-rerank", "hybrid"])
        self.assertEqual(dense.recording, dense_rerank.recording)
        self.assertEqual(dense_rerank.recording, hybrid.recording)
        self.assertEqual(dense_rerank.rankings["2"], [b.parent_id])
        self.assertEqual(reranker.rerank.await_count, 2 * len(gold.cases))
        calls = [call.args for call in reranker.rerank.await_args_list if call.args[0] == "unit 0"]
        self.assertEqual(calls[0], ("unit 0", [b], 2))
        first = json.loads(trace.getvalue().splitlines()[0])
        self.assertEqual(first["hybrid_sparse"], [a.chunk_id])
        self.assertEqual(first["dense_reranked"], [b.chunk_id])
        self.assertEqual(first["reranked"][0], a.chunk_id)
        report = paired_report(gold, [hybrid, dense], 1)
        self.assertEqual([row["id"] for row in report["recovered"]], ["0"])
        self.assertEqual([row["id"] for row in report["regressed"]], ["2"])
        self.assertEqual(len(report["still_missed"]), 48)
        hybrid.recording = hybrid.recording.model_copy(update={"pit_sha256": "2" * 64})
        with self.assertRaisesRegex(ValueError, "same recording"):
            paired_report(gold, [dense, hybrid], 1)

    async def test_incomplete_dense_rerank_aborts_before_parent_hydration(self):
        self.review.cases = self.review.cases[:1]
        gold = freeze_review(self.corpus, self.review)
        store, published, embedder, reranker = MagicMock(), AsyncMock(), AsyncMock(), AsyncMock()
        store.corpus_snapshot.return_value.__aenter__.return_value = (published, ["index"], "a" * 64)
        published.search_comparison_batch.return_value = [([self.child_b], [self.child_b, self.child_a], [])]
        reranker.rerank.side_effect = [[self.child_b], [self.child_b, self.child_a]]
        with self.assertRaisesRegex(ValueError, "incomplete dense-rerank response for 0"):
            await record_pair(self.corpus, gold, store, embedder, reranker, io.StringIO(), k=1, multiplier=2, batch_size=1)
        published.get_parents.assert_not_awaited()

    def test_distributed_questions_are_pending_and_pinned_to_the_shipped_manifest(self):
        data = Path(__file__).resolve().parents[1] / "examples/rag-evaluation/operations-v1"
        manifest = Manifest.model_validate_json((data / "sources.json").read_bytes())
        review = Review.model_validate_json((data / "candidates.json").read_bytes())
        self.assertEqual(review.corpus_sha256, sha256(LAYOUT + "\n" + manifest.model_dump_json()))
        self.assertEqual(review.dataset, manifest.dataset)
        self.assertEqual({c.status for c in review.cases}, {"pending"})
        self.assertEqual(len({c.id for c in review.cases}), len(review.cases))
        chunks = {row["id"]: row for row in map(json.loads, (data / "chunks.jsonl").read_text(encoding="utf-8").splitlines())}
        for case in review.cases:
            for evidence in case.evidence:
                child, parent = chunks[evidence.child], chunks[evidence.parent]
                self.assertEqual((child["document"], child["parent_id"]), (evidence.document, parent["id"]))
                self.assertEqual((parent["version"], parent["parent_id"]), (review.corpus_sha256, ""))
                self.assertLessEqual(child["lines"][0], evidence.line)
                self.assertLessEqual(evidence.line, child["lines"][1])
                self.assertIn(evidence.quote, child["text"])


class BenchmarkStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_prefix_rejected_before_any_write(self):
        requests = []

        def respond(request):
            requests.append((request.method, request.url.path))
            return httpx.Response(200, json={"indices": [{"name": "occupied"}], "aliases": [], "data_streams": []})

        settings = Settings(elasticsearch_url="https://unit.invalid", elasticsearch_index="rag-eval-unit")
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with self.assertRaisesRegex(ValueError, "already in use"):
                await BenchmarkStore(settings, client).require_unused_prefix()
        self.assertEqual(requests, [("GET", "/_resolve/index/rag-eval-unit-*")])

    async def test_batch_uses_exact_dense_k_and_aborts_partial_pit_results(self):
        closed = []

        def respond(request):
            if request.url.path.endswith("/_pit") and request.method == "POST":
                return httpx.Response(200, json={"id": "unit-pit"})
            if request.url.path == "/_msearch":
                bodies = [json.loads(line) for line in request.content.splitlines()[1::2]]
                self.assertEqual([body["size"] for body in bodies], [5, 20, 20])
                self.assertEqual([body["knn"]["k"] for body in bodies[:2]], [5, 20])
                self.assertEqual({b["pit"]["id"] for b in bodies}, {"unit-pit"})
                return httpx.Response(200, json={"responses": [{"error": "failed"},
                    {"pit_id": "rotated", "hits": {"hits": []}}, {"hits": {"hits": []}}]})
            self.assertEqual((request.method, request.url.path), ("DELETE", "/_pit"))
            closed.append(json.loads(request.content)["id"])
            return httpx.Response(200, json={"succeeded": True})

        settings = Settings(elasticsearch_url="https://unit.invalid", elasticsearch_index="rag-eval-unit")
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            store = BenchmarkStore(settings, client)
            with self.assertRaisesRegex(RuntimeError, "subsearch 0"):
                async with store._open_snapshot("rag-eval-unit-doc-a", keep_alive="10m", allow_missing=False) as published:
                    await published.search_comparison_batch([([1.0], {"t": 1.0})], 5, 20)
        self.assertCountEqual(closed, ["unit-pit", "rotated"])

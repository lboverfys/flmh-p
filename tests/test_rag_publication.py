import json
import unittest
from unittest.mock import AsyncMock

import httpx

from flmh_agent.adapters.outbound.bge_http import ElasticsearchHybridStore
from flmh_agent.application.ingestion import KnowledgeIngestion
from flmh_agent.application.rag import hybrid_retrieve
from flmh_agent.config import Settings


class KnowledgePublicationTests(unittest.IsolatedAsyncioTestCase):
    async def test_old_version_cannot_be_republished_after_another_version_replaces_its_baseline(self):
        calls = []

        async def handler(request):
            calls.append(request.method)
            if request.url.path.startswith("/_alias/"):
                return httpx.Response(200, json={"knowledge-doc-newer": {"aliases": {}}})
            return httpx.Response(200, json={"_source": {"document_id": "manual", "version": "v1",
                "source_ref": "local:manual", "content_hash": "hash", "embedding_model": "",
                "embedding_dimension": 1024, "kind": "manifest", "base_index": "knowledge-anchor-old"}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            store = ElasticsearchHybridStore(Settings(elasticsearch_url="https://fixture", elasticsearch_index="knowledge"), client)
            with self.assertRaisesRegex(ValueError, "baseline changed"):
                await store.reserve_document("manual", "v1", "local:manual", "hash")
        self.assertEqual(calls, ["GET", "GET"])

    async def test_failed_embedding_batch_never_publishes_partial_document(self):
        store = AsyncMock()
        store.reserve_document.return_value = "STAGING"
        embedder = AsyncMock()
        embedder.encode_many.side_effect = RuntimeError("embedding unavailable")
        with self.assertRaisesRegex(RuntimeError, "embedding unavailable"):
            await KnowledgeIngestion(embedder, store).ingest("manual", "v1", "local:manual", "正文")
        store.upsert_chunks.assert_awaited_once()
        store.publish_document.assert_not_awaited()

    async def test_sealed_retry_publishes_without_rewriting_vectors_and_published_replay_is_read_only(self):
        store, embedder = AsyncMock(), AsyncMock()
        store.reserve_document.return_value = "SEALED"
        result = await KnowledgeIngestion(embedder, store).ingest("manual", "v1", "local:manual", "正文")
        self.assertEqual(result["embeddingBatches"], 0)
        store.publish_document.assert_awaited_once_with("manual", "v1", 1, 1)
        store.upsert_chunks.assert_not_awaited()
        embedder.encode_many.assert_not_awaited()
        store.reset_mock()
        store.reserve_document.return_value = "PUBLISHED"
        result = await KnowledgeIngestion(embedder, store).ingest("manual", "v1", "local:manual", "正文")
        self.assertEqual(result["mode"], "ALREADY_PUBLISHED")
        store.upsert_chunks.assert_not_awaited()
        store.publish_document.assert_not_awaited()

    async def test_publication_seals_refreshes_validates_then_swaps_one_document_alias(self):
        calls, selected = [], {}

        async def handler(request):
            payload = json.loads(request.content) if request.content else None
            calls.append((request.method, request.url.path, payload))
            if "/_doc/manifest-" in request.url.path:
                return httpx.Response(200, json={"_source": {"base_index": "knowledge-doc-old"}})
            if request.url.path.endswith("/_count"):
                return httpx.Response(200, json={"count": 2 if payload["query"]["term"]["kind"] == "parent" else 5})
            if request.url.path == "/_aliases":
                self.assertEqual(len(payload["actions"]), 2)
                remove, add = payload["actions"]
                self.assertEqual(remove["remove"]["alias"], add["add"]["alias"])
                self.assertEqual(remove["remove"]["index"], "knowledge-doc-old")
                self.assertTrue(remove["remove"]["must_exist"])
                selected[add["add"]["index"]] = {"aliases": {add["add"]["alias"]: {}}}
            if request.url.path.startswith("/_alias/"):
                return httpx.Response(200, json=selected)
            return httpx.Response(200, json={"acknowledged": True})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            store = ElasticsearchHybridStore(Settings(elasticsearch_url="https://fixture", elasticsearch_index="knowledge"), client)
            await store.publish_document("manual", "v2", 2, 5)
        self.assertTrue(calls[1][1].endswith("/_block/write"))
        self.assertTrue(calls[2][1].endswith("/_refresh"))
        self.assertTrue(calls[3][1].endswith("/_count"))
        self.assertTrue(calls[4][1].endswith("/_count"))
        self.assertEqual(calls[5][1], "/_aliases")

    async def test_wrong_chunk_count_preserves_previously_published_alias(self):
        calls = []

        async def handler(request):
            calls.append(request.url.path)
            if "/_doc/manifest-" in request.url.path:
                return httpx.Response(200, json={"_source": {"base_index": "knowledge-doc-old"}})
            if request.url.path.endswith("/_count"):
                return httpx.Response(200, json={"count": 0})
            return httpx.Response(200, json={"acknowledged": True})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            store = ElasticsearchHybridStore(Settings(elasticsearch_url="https://fixture", elasticsearch_index="knowledge"), client)
            with self.assertRaisesRegex(RuntimeError, "count differs"):
                await store.publish_document("manual", "v2", 1, 1)
        self.assertNotIn("/_aliases", calls)

    async def test_publication_conflict_is_not_reported_as_success(self):
        async def handler(request):
            if "/_doc/manifest-" in request.url.path:
                return httpx.Response(200, json={"_source": {"base_index": "old-index"}})
            if request.url.path.endswith("/_count"):
                return httpx.Response(200, json={"count": 1})
            if request.url.path == "/_aliases":
                return httpx.Response(404, json={"error": {"type": "aliases_not_found_exception"}})
            return httpx.Response(200, json={"acknowledged": True})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            store = ElasticsearchHybridStore(Settings(elasticsearch_url="https://fixture", elasticsearch_index="knowledge"), client)
            with self.assertRaises(httpx.HTTPStatusError):
                await store.publish_document("manual", "v2", 1, 1)

    async def test_retrieval_pins_both_recalls_and_fetches_parents_from_original_physical_index(self):
        calls = []

        async def handler(request):
            body = json.loads(request.content) if request.content else None
            calls.append((request.method, request.url.path, body))
            if request.method == "POST" and request.url.path.endswith("/_pit"):
                self.assertIn("knowledge-active-*", request.url.path)
                return httpx.Response(200, json={"id": "snapshot-before-v2"})
            if request.url.path == "/_search":
                self.assertEqual(body["pit"]["id"], "snapshot-before-v2")
                return httpx.Response(200, json={"hits": {"hits": [{"_id": "child-v1", "_index": "knowledge-doc-v1", "_source": {
                    "parent_id": "parent-v1", "text": "版本一", "document_id": "manual", "version": "v1"}}]}})
            if request.url.path == "/_mget":
                self.assertEqual(body["docs"], [{"_index": "knowledge-doc-v1", "_id": "parent-v1"}])
                return httpx.Response(200, json={"docs": [{"_id": "parent-v1", "found": True,
                    "_source": {"text": "版本一父块", "version": "v1"}}]})
            return httpx.Response(200, json={"succeeded": True})

        embedder, reranker = AsyncMock(), AsyncMock()
        embedder.encode.return_value = ([0.1], {"token": 1.0})
        reranker.rerank.side_effect = lambda query, chunks, limit: chunks
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            store = ElasticsearchHybridStore(Settings(elasticsearch_url="https://fixture", elasticsearch_index="knowledge"), client)
            result = await hybrid_retrieve("查询", embedder, store, reranker)
        self.assertEqual(result[0].version, "v1")
        self.assertEqual(calls[-1], ("DELETE", "/_pit", {"id": "snapshot-before-v2"}))

    async def test_pit_is_closed_when_search_fails(self):
        calls = []

        async def handler(request):
            calls.append((request.method, request.url.path))
            if request.method == "POST" and request.url.path.endswith("/_pit"):
                return httpx.Response(200, json={"id": "snapshot"})
            if request.url.path == "/_search":
                return httpx.Response(503, json={"error": "search unavailable"})
            return httpx.Response(200, json={"succeeded": True})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            store = ElasticsearchHybridStore(Settings(elasticsearch_url="https://fixture", elasticsearch_index="knowledge"), client)
            with self.assertRaises(httpx.HTTPStatusError):
                async with store.snapshot() as snapshot:
                    await snapshot.search_dense([0.1], 3)
        self.assertEqual(calls[-1], ("DELETE", "/_pit"))

import unittest
import json

import httpx

from flmh_agent.application.ingestion import KnowledgeIngestion
from flmh_agent.application.workflow import build_workflow
from flmh_agent.adapters.outbound.bge_http import BgeM3HttpAdapter, ElasticsearchHybridStore
from flmh_agent.config import Settings
from flmh_agent.domain.planning import RuleBasedPlanner


class IngestionTests(unittest.IsolatedAsyncioTestCase):
    async def test_unconfigured_dimension_prevents_embedding_and_index_writes(self):
        calls = []
        async def handler(request):
            calls.append(request)
            raise AssertionError("unconfigured adapters must not make a request")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaisesRegex(ValueError, "BGE_M3_DIMENSION"):
                await BgeM3HttpAdapter(Settings(), client).encode("test")
            store = ElasticsearchHybridStore(Settings(), client)
            with self.assertRaisesRegex(ValueError, "BGE_M3_DIMENSION"):
                await store.ensure_index("doc", "v1")
            with self.assertRaisesRegex(ValueError, "BGE_M3_DIMENSION"):
                await store.reserve_document("doc", "v1", "fixture", "hash")
        self.assertEqual(calls, [])

    async def test_ingestion_batches_embeddings_and_keeps_sources_and_stable_ids(self):
        class Embedder:
            def __init__(self):
                self.batches = []
            async def encode_many(self, texts):
                self.batches.append(len(texts))
                return [([0.1, 0.2], {"token": 1.0}) for _ in texts]
        class Store:
            def __init__(self):
                self.batches = []
            async def ensure_index(self, document_id, version):
                pass
            async def reserve_document(self, *arguments):
                self.manifest = arguments
                return "STAGING"
            async def upsert_chunks(self, chunks, vectors=None):
                self.batches.append((list(chunks), vectors))
            async def publish_document(self, document_id, version, parents, children):
                self.publication = (document_id, version, parents, children)
        embedder, store = Embedder(), Store()
        service = KnowledgeIngestion(embedder, store)
        result = await service.ingest("manual", "v1", "local:manual", "资料正文" * 7000)
        self.assertGreater(result["embeddingBatches"], 1)
        self.assertEqual(store.publication[2:], (result["parents"], result["children"]))
        self.assertTrue(all(size <= 32 for size in embedder.batches))
        first_ids = [chunk.chunk_id for batch, _ in store.batches for chunk in batch]
        self.assertTrue(all(chunk.source_ref == "local:manual" for batch, _ in store.batches for chunk in batch))
        store.batches.clear()
        await service.ingest("manual", "v1", "local:manual", "资料正文" * 7000)
        self.assertEqual(first_ids, [chunk.chunk_id for batch, _ in store.batches for chunk in batch])

    async def test_adapters_use_batch_embedding_and_bulk_rejects_partial_failure(self):
        calls = []
        async def handler(request):
            calls.append(request)
            if request.url.path == "/encode":
                self.assertEqual(len(json.loads(request.content)["input"]), 2)
                return httpx.Response(200, json={"data": [{"denseVector": [0.1, 0.2], "sparseVector": {"w": 0.3}}] * 2})
            return httpx.Response(200, json={"items": [{"index": {"status": 201}}, {"index": {"status": 429}}]})
        settings = Settings(bge_m3_url="https://fixture/encode", embedding_dimension=2, elasticsearch_url="https://fixture", elasticsearch_index="chunks")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            vectors = await BgeM3HttpAdapter(settings, client).encode_many(["a", "b"])
            from flmh_agent.domain.rag import Chunk
            with self.assertRaisesRegex(RuntimeError, "not fully acknowledged"):
                await ElasticsearchHybridStore(settings, client).upsert_chunks([
                    Chunk("a", "p", "a", document_id="doc", version="v1"),
                    Chunk("b", "p", "b", document_id="doc", version="v1")], vectors)
        self.assertEqual(calls[1].headers["content-type"], "application/x-ndjson")
        self.assertEqual(len(calls[1].content.decode().splitlines()), 4)

    async def test_document_version_rejects_different_content_without_overwriting(self):
        calls = []
        async def handler(request):
            calls.append(request.method)
            if "/_alias/" in request.url.path:
                return httpx.Response(200, json={"anchor": {"aliases": {}}})
            if request.url.path.endswith("/_settings"):
                return httpx.Response(200, json={})
            return httpx.Response(200, json={"_source": {"document_id": "doc", "version": "v1", "source_ref": "local:doc", "content_hash": "original", "kind": "manifest", "embedding_model": "", "embedding_dimension": 1024, "base_index": "anchor"}})
        settings = Settings(embedding_dimension=1024, elasticsearch_url="https://fixture", elasticsearch_index="chunks")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            store = ElasticsearchHybridStore(settings, client)
            await store.reserve_document("doc", "v1", "local:doc", "original")
            with self.assertRaisesRegex(ValueError, "different content"):
                await store.reserve_document("doc", "v1", "local:doc", "changed")
        self.assertEqual(calls, ["GET", "GET", "GET", "GET", "GET"])

    async def test_knowledge_request_uses_retriever_and_has_explicit_unconfigured_result(self):
        class Knowledge:
            async def retrieve(self, question):
                return {"source": "LOCAL_CONTRACT_FIXTURE", "chunks": [{"id": "p1", "text": "已知资料", "sourceRef": "fixture:manual"}]}
        graph = build_workflow(planner=RuleBasedPlanner(), tools=object(), knowledge_query=Knowledge())
        result = await graph.ainvoke({"user_request": "从知识库查询采集说明"}, {"configurable": {"thread_id": "rag"}})
        self.assertIn("fixture:manual", result["summary"])
        graph = build_workflow(planner=RuleBasedPlanner(), tools=object())
        missing = await graph.ainvoke({"user_request": "从知识库查询采集说明"}, {"configurable": {"thread_id": "rag-missing"}})
        self.assertIn("BGE_M3_MODEL", missing["configuration_todos"])
        self.assertEqual(missing["knowledge_result"], {})

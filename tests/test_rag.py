import unittest

from flmh_agent.application.rag import hybrid_retrieve, reciprocal_rank_fusion
from flmh_agent.domain.rag import Chunk


class RagTests(unittest.IsolatedAsyncioTestCase):
    async def test_rrf_merges_dense_and_sparse_rankings(self):
        dense = [Chunk("a", "pa", "A"), Chunk("b", "pb", "B")]
        sparse = [Chunk("b", "pb", "B"), Chunk("c", "pc", "C")]
        result = reciprocal_rank_fusion(dense, sparse, rank_constant=1)
        self.assertEqual([chunk.chunk_id for chunk in result], ["b", "a", "c"])

    async def test_dense_sparse_rerank_and_parent_fetch_use_batch_calls(self):
        class Embedder:
            async def encode(self, text):
                self.text = text
                return [0.1, 0.2], {"word": 0.5}

        class Store:
            def __init__(self):
                self.calls = []

            async def search_dense(self, vector, limit):
                self.calls.append(("dense", vector, limit))
                return [Chunk("d1", "parent-1", "Dense child")]

            async def search_sparse(self, vector, limit):
                self.calls.append(("sparse", vector, limit))
                return [Chunk("s1", "parent-1", "Sparse child")]

            async def get_parents(self, parent_ids):
                self.calls.append(("parents", list(parent_ids)))
                return [Chunk("parent-1", "", "Parent block")]

        class Reranker:
            async def rerank(self, query, chunks, limit):
                self.calls = (query, list(chunks), limit)
                return list(chunks)[:limit]

        store = Store()
        reranker = Reranker()
        result = await hybrid_retrieve("query", Embedder(), store, reranker, limit=2)
        self.assertEqual([chunk.chunk_id for chunk in result], ["parent-1"])
        self.assertEqual([call[0] for call in store.calls], ["dense", "sparse", "parents"])
        self.assertEqual(len(store.calls[-1][1]), 1)

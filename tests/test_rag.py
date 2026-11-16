import asyncio
import unittest

from flmh_agent.rag import Chunk, reciprocal_rank_fusion, rerank_and_backfill_parents


class RagBoundaryTests(unittest.TestCase):
    def test_rrf_fuses_dense_and_sparse_rankings_without_claiming_retrieval(self):
        dense = [Chunk("a", "pa", "A"), Chunk("b", "pb", "B")]
        sparse = [Chunk("b", "pb", "B"), Chunk("c", "pc", "C")]
        fused = reciprocal_rank_fusion(dense, sparse, rank_constant=0)
        self.assertEqual([chunk.chunk_id for chunk in fused], ["b", "a", "c"])

    def test_reranked_children_are_backfilled_with_parent_blocks(self):
        class Reranker:
            async def rerank(self, query, chunks, limit):
                return list(chunks)[:limit]

        class ParentStore:
            async def get_parents(self, parent_ids):
                self.parent_ids = list(parent_ids)
                return [Chunk("parent-1", "", "Parent block")]

        store = ParentStore()
        result = asyncio.run(rerank_and_backfill_parents(
            "query",
            [Chunk("child-1", "parent-1", "child")],
            Reranker(),
            store,
            limit=3,
        ))
        self.assertEqual(store.parent_ids, ["parent-1"])
        self.assertEqual([chunk.chunk_id for chunk in result], ["parent-1"])


if __name__ == "__main__":
    unittest.main()

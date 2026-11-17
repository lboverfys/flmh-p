from __future__ import annotations

import asyncio
from dataclasses import replace
from collections.abc import Sequence

from flmh_agent.domain.rag import Chunk
from flmh_agent.ports.rag import HybridEmbeddingPort, HybridVectorStorePort, RerankerPort


def reciprocal_rank_fusion(
    dense: Sequence[Chunk],
    sparse: Sequence[Chunk],
    *,
    rank_constant: int = 60,
) -> list[Chunk]:
    if rank_constant < 1:
        raise ValueError("rank_constant must be at least 1")
    scores: dict[str, float] = {}
    chunks: dict[str, Chunk] = {}
    for ranking in (dense, sparse):
        for rank, chunk in enumerate(ranking, start=1):
            chunks.setdefault(chunk.chunk_id, chunk)
            scores[chunk.chunk_id] = scores.get(chunk.chunk_id, 0.0) + 1.0 / (rank_constant + rank)
    return [
        replace(chunks[chunk_id], score=score)
        for chunk_id, score in sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    ]


async def hybrid_retrieve(
    query: str,
    embedder: HybridEmbeddingPort,
    store: HybridVectorStorePort,
    reranker: RerankerPort,
    *,
    limit: int = 5,
    candidate_multiplier: int = 4,
) -> list[Chunk]:
    if not query.strip():
        raise ValueError("query is required")
    if not 1 <= limit <= 100 or not 1 <= candidate_multiplier <= 10:
        raise ValueError("invalid retrieval bounds")
    dense_vector, sparse_vector = await embedder.encode(query)
    candidate_limit = limit * candidate_multiplier
    async with store.snapshot() as published:
        recalls = await asyncio.gather(
            published.search_dense(dense_vector, candidate_limit),
            published.search_sparse(sparse_vector, candidate_limit),
            return_exceptions=True,
        )
        for recall in recalls:
            if isinstance(recall, BaseException):
                raise recall
        dense, sparse = recalls
        fused = reciprocal_rank_fusion(dense, sparse)
        reranked = await reranker.rerank(query, fused, candidate_limit)
        parent_ids: dict[str, None] = {}
        for chunk in reranked:
            if chunk.parent_id:
                parent_ids[chunk.parent_id] = None
                if len(parent_ids) == limit:
                    break
        parents = await published.get_parents(list(parent_ids)) if parent_ids else []
    by_id = {chunk.chunk_id: chunk for chunk in parents}
    return [by_id[parent_id] for parent_id in parent_ids if parent_id in by_id]


class HybridKnowledgeService:
    def __init__(self, embedder: HybridEmbeddingPort, store: HybridVectorStorePort, reranker: RerankerPort):
        self.embedder = embedder
        self.store = store
        self.reranker = reranker

    async def retrieve(self, question: str) -> dict:
        chunks = await hybrid_retrieve(question, self.embedder, self.store, self.reranker, limit=5)
        return {"source": "CONFIGURED_HYBRID_RETRIEVAL", "chunks": [
            {"id": chunk.chunk_id, "text": chunk.text, "sourceRef": chunk.source_ref,
             "documentId": chunk.document_id, "version": chunk.version} for chunk in chunks
        ]}

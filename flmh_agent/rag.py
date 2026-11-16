from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    parent_id: str
    text: str
    score: float = 0.0


class DenseRetriever(Protocol):
    async def search_dense(self, query: str, limit: int) -> Sequence[Chunk]: ...


class SparseRetriever(Protocol):
    async def search_sparse(self, query: str, limit: int) -> Sequence[Chunk]: ...


class Reranker(Protocol):
    async def rerank(self, query: str, chunks: Sequence[Chunk], limit: int) -> Sequence[Chunk]: ...


class ParentChunkStore(Protocol):
    async def get_parents(self, parent_ids: Sequence[str]) -> Sequence[Chunk]: ...


def reciprocal_rank_fusion(
    dense: Sequence[Chunk],
    sparse: Sequence[Chunk],
    *,
    rank_constant: int = 60,
) -> list[Chunk]:
    """Fuse already-produced dense and sparse rankings; performs no retrieval itself."""
    scores: dict[str, float] = {}
    chunks: dict[str, Chunk] = {}
    for ranking in (dense, sparse):
        for rank, chunk in enumerate(ranking, start=1):
            chunks.setdefault(chunk.chunk_id, chunk)
            scores[chunk.chunk_id] = scores.get(chunk.chunk_id, 0.0) + 1.0 / (rank_constant + rank)
    return [
        Chunk(chunk.chunk_id, chunk.parent_id, chunk.text, scores[chunk_id])
        for chunk_id, chunk in sorted(chunks.items(), key=lambda item: (-scores[item[0]], item[0]))
    ]


async def rerank_and_backfill_parents(
    query: str,
    chunks: Sequence[Chunk],
    reranker: Reranker,
    parent_store: ParentChunkStore,
    *,
    limit: int,
) -> list[Chunk]:
    reranked = await reranker.rerank(query, chunks, limit)
    parent_ids = list(dict.fromkeys(chunk.parent_id for chunk in reranked if chunk.parent_id))
    parents = await parent_store.get_parents(parent_ids)
    parents_by_id = {chunk.chunk_id: chunk for chunk in parents}
    return [parents_by_id[parent_id] for parent_id in parent_ids if parent_id in parents_by_id]


async def hybrid_retrieve(
    query: str,
    dense_retriever: DenseRetriever,
    sparse_retriever: SparseRetriever,
    reranker: Reranker,
    parent_store: ParentChunkStore,
    *,
    limit: int = 5,
) -> list[Chunk]:
    dense, sparse = await asyncio.gather(
        dense_retriever.search_dense(query, limit * 3),
        sparse_retriever.search_sparse(query, limit * 3),
    )
    fused = reciprocal_rank_fusion(dense, sparse)
    return await rerank_and_backfill_parents(query, fused, reranker, parent_store, limit=limit)


def rag_configuration_state() -> dict[str, str | bool]:
    from .config import settings

    configured = bool(settings.embedding_provider and settings.embedding_model and settings.vector_store_url)
    return {
        "configured": configured,
        "embeddingProvider": settings.embedding_provider,
        "embeddingModel": settings.embedding_model,
        "vectorStoreUrl": settings.vector_store_url,
        "status": "TODO: embedding and vector store are not selected; no real retrieval is claimed.",
    }

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol

from flmh_agent.domain.rag import Chunk


class HybridEmbeddingPort(Protocol):
    async def encode(self, text: str) -> tuple[Sequence[float], Mapping[str, float]]: ...


class HybridVectorStorePort(Protocol):
    async def search_dense(self, vector: Sequence[float], limit: int) -> Sequence[Chunk]: ...
    async def search_sparse(self, vector: Mapping[str, float], limit: int) -> Sequence[Chunk]: ...
    async def get_parents(self, parent_ids: Sequence[str]) -> Sequence[Chunk]: ...


class RerankerPort(Protocol):
    async def rerank(self, query: str, chunks: Sequence[Chunk], limit: int) -> Sequence[Chunk]: ...

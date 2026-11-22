from __future__ import annotations

import asyncio
import json
import math
import re
from dataclasses import replace
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from flmh_agent.domain.rag import Chunk
from flmh_agent.ports.rag import HybridEmbeddingPort, HybridVectorStorePort, RerankerPort


_TOKEN_RE = re.compile(r"[A-Za-z0-9_.-]+|[\u4e00-\u9fff]")


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


def ranked_parent_ids(children: Sequence[Chunk], limit: int) -> list[str]:
    parents: dict[str, None] = {}
    for child in children:
        if child.parent_id:
            parents[child.parent_id] = None
            if len(parents) == limit:
                break
    return list(parents)


async def hybrid_child_ranking(
    query: str,
    dense: Sequence[Chunk],
    sparse: Sequence[Chunk],
    reranker: RerankerPort,
    candidate_limit: int,
    *,
    rank_constant: int = 60,
) -> tuple[list[Chunk], Sequence[Chunk]]:
    fused = reciprocal_rank_fusion(dense, sparse, rank_constant=rank_constant)
    return fused, await reranker.rerank(query, fused, candidate_limit)


async def hybrid_retrieve(
    query: str,
    embedder: HybridEmbeddingPort,
    store: HybridVectorStorePort,
    reranker: RerankerPort,
    *,
    limit: int = 5,
    candidate_multiplier: int = 4,
    rank_constant: int = 60,
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
        _, reranked = await hybrid_child_ranking(
            query, dense, sparse, reranker, candidate_limit, rank_constant=rank_constant
        )
        parent_ids = ranked_parent_ids(reranked, limit)
        parents = await published.get_parents(parent_ids) if parent_ids else []
    by_id = {chunk.chunk_id: chunk for chunk in parents}
    return [by_id[parent_id] for parent_id in parent_ids if parent_id in by_id]


class HybridKnowledgeService:
    def __init__(
        self,
        embedder: HybridEmbeddingPort,
        store: HybridVectorStorePort,
        reranker: RerankerPort,
        *,
        limit: int = 5,
        candidate_multiplier: int = 4,
        rank_constant: int = 60,
    ):
        self.embedder = embedder
        self.store = store
        self.reranker = reranker
        self.limit = limit
        self.candidate_multiplier = candidate_multiplier
        self.rank_constant = rank_constant

    async def retrieve(self, question: str) -> dict:
        chunks = await hybrid_retrieve(
            question,
            self.embedder,
            self.store,
            self.reranker,
            limit=self.limit,
            candidate_multiplier=self.candidate_multiplier,
            rank_constant=self.rank_constant,
        )
        return {"source": "CONFIGURED_HYBRID_RETRIEVAL", "chunks": [
            {"id": chunk.chunk_id, "text": chunk.text, "sourceRef": chunk.source_ref,
             "documentId": chunk.document_id, "version": chunk.version} for chunk in chunks
        ]}


def _tokenize_rag(text: str) -> list[str]:
    base = [tok.casefold() for tok in _TOKEN_RE.findall(text or "")]
    bigrams = [a + b for a, b in zip(base, base[1:]) if "\u4e00" <= a <= "\u9fff" and "\u4e00" <= b <= "\u9fff"]
    return base + bigrams


class LocalCorpusFallbackKnowledgeService:
    """Offline fallback retriever over local operations-v3/v2 chunked Markdown corpus."""

    def __init__(
        self,
        *,
        corpus_path: Path | None = None,
        limit: int = 5,
        candidate_multiplier: int = 4,
        rank_constant: int = 60,
    ) -> None:
        self.limit = max(1, min(int(limit), 20))
        self.candidate_multiplier = max(1, min(int(candidate_multiplier), 10))
        self.rank_constant = max(1, int(rank_constant))
        self._corpus_path = corpus_path
        self._parents: dict[str, Chunk] = {}
        self._children: list[Chunk] = []
        self._child_tfs: list[Counter[str]] = []
        self._idf: dict[str, float] = {}
        self._avg_dl: float = 1.0
        self._loaded = False

    def _resolve_corpus_path(self) -> Path | None:
        if self._corpus_path is not None:
            return self._corpus_path if self._corpus_path.is_file() else None
        base = Path(__file__).resolve().parents[2] / "examples" / "rag-evaluation"
        for candidate in (base / "operations-v3" / "chunks.jsonl", base / "operations-v2" / "chunks.jsonl"):
            if candidate.is_file() and candidate.stat().st_size > 0:
                return candidate
        return None

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        path = self._resolve_corpus_path()
        if path is None:
            return
        parents: dict[str, Chunk] = {}
        children: list[Chunk] = []
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                raw = line.strip()
                if not raw:
                    continue
                item = json.loads(raw)
                chunk = Chunk(
                    chunk_id=str(item["id"]),
                    parent_id=str(item.get("parent_id", "")),
                    text=str(item.get("text", "")),
                    score=0.0,
                    source_ref=str(item.get("source_ref", "")),
                    document_id=str(item.get("document_id", "")),
                    version=str(item.get("version", "")),
                )
                if chunk.parent_id:
                    children.append(chunk)
                else:
                    parents[chunk.chunk_id] = chunk
        self._parents = parents
        self._children = children or list(parents.values())
        doc_freq: Counter[str] = Counter()
        tfs: list[Counter[str]] = []
        total_len = 0
        for chunk in self._children:
            toks = _tokenize_rag(chunk.text)
            tf = Counter(toks)
            tfs.append(tf)
            total_len += sum(tf.values())
            for term in tf:
                doc_freq[term] += 1
        n_docs = max(1, len(self._children))
        self._child_tfs = tfs
        self._avg_dl = total_len / n_docs if total_len else 1.0
        self._idf = {term: math.log(1.0 + (n_docs - df + 0.5) / (df + 0.5)) for term, df in doc_freq.items()}

    def search_chunks(self, question: str, *, limit: int | None = None) -> list[Chunk]:
        if not question.strip():
            raise ValueError("query is required")
        self._ensure_loaded()
        if not self._children:
            return []
        top_k = max(1, min(int(limit or self.limit), 20))
        cand_limit = top_k * self.candidate_multiplier
        q_tokens = _tokenize_rag(question)
        q_tf = Counter(q_tokens)
        if not q_tf:
            return []

        bm25_scores: list[tuple[int, float]] = []
        cosine_scores: list[tuple[int, float]] = []
        q_norm = math.sqrt(sum((cnt * self._idf.get(t, 0.5)) ** 2 for t, cnt in q_tf.items())) or 1.0
        k1, b = 1.2, 0.75
        for idx, (chunk, tf) in enumerate(zip(self._children, self._child_tfs)):
            dl = sum(tf.values()) or 1
            bm25 = 0.0
            dot = 0.0
            for term, q_cnt in q_tf.items():
                f = tf.get(term, 0)
                if f <= 0:
                    continue
                idf = self._idf.get(term, 0.5)
                bm25 += idf * (f * (k1 + 1.0)) / (f + k1 * (1.0 - b + b * dl / self._avg_dl))
                dot += (f * idf) * (q_cnt * idf)
            if bm25 > 0:
                bm25_scores.append((idx, bm25))
            if dot > 0:
                d_norm = math.sqrt(sum((cnt * self._idf.get(t, 0.5)) ** 2 for t, cnt in tf.items())) or 1.0
                cosine_scores.append((idx, dot / (q_norm * d_norm)))

        sparse_ranked = [replace(self._children[i], score=s) for i, s in sorted(bm25_scores, key=lambda x: -x[1])[:cand_limit]]
        dense_ranked = [replace(self._children[i], score=s) for i, s in sorted(cosine_scores, key=lambda x: -x[1])[:cand_limit]]
        fused = reciprocal_rank_fusion(dense_ranked, sparse_ranked, rank_constant=self.rank_constant)
        if not self._parents:
            return fused[:top_k]
        parent_ids = ranked_parent_ids(fused, top_k)
        score_by_parent: dict[str, float] = {}
        for c in fused:
            if c.parent_id and c.parent_id not in score_by_parent:
                score_by_parent[c.parent_id] = c.score
        return [replace(self._parents[pid], score=score_by_parent.get(pid, 0.0)) for pid in parent_ids if pid in self._parents]

    async def retrieve(self, question: str) -> dict:
        chunks = self.search_chunks(question, limit=self.limit)
        return {"source": "LOCAL_CORPUS_FALLBACK", "chunks": [
            {"id": chunk.chunk_id, "parentId": chunk.parent_id, "text": chunk.text, "score": chunk.score,
             "sourceRef": chunk.source_ref, "documentId": chunk.document_id, "version": chunk.version}
            for chunk in chunks
        ]}

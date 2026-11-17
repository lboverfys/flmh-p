from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import httpx

from flmh_agent.config import Settings
from flmh_agent.domain.rag import Chunk


class BgeM3HttpAdapter:
    """Calls the configured BGE-M3 service contract for dense and lexical vectors."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._client = client

    async def encode(self, text: str) -> tuple[list[float], dict[str, float]]:
        if not text.strip():
            raise ValueError("embedding input is required")
        result = await self._post(self.settings.bge_m3_url, {
            "model": self.settings.bge_m3_model,
            "input": [text],
            "returnDense": True,
            "returnSparse": True,
        }, self.settings.bge_m3_api_key)
        records = result.get("data")
        record = records[0] if isinstance(records, list) and records else result
        dense = record.get("denseVector", record.get("dense_vector", record.get("dense_vecs")))
        sparse = record.get("sparseVector", record.get("sparse_vector", record.get("lexical_weights")))
        if not isinstance(dense, list) or len(dense) != self.settings.embedding_dimension:
            raise ValueError("BGE-M3 returned an invalid dense vector dimension")
        if not isinstance(sparse, dict) or not sparse:
            raise ValueError("BGE-M3 returned no sparse lexical weights")
        return [float(value) for value in dense], {str(key): float(value) for key, value in sparse.items()}

    async def _post(self, url: str, payload: dict[str, Any], api_key: str) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        if self._client is not None:
            response = await self._client.post(url, json=payload, headers=headers)
        else:
            async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds, trust_env=False) as client:
                response = await client.post(url, json=payload, headers=headers)
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, dict):
            raise TypeError("BGE-M3 service returned a non-object response")
        return value


class BgeRerankerHttpAdapter:
    """Calls a configured BGE-Reranker endpoint with a batch of candidate passages."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._client = client

    async def rerank(self, query: str, chunks: Sequence[Chunk], limit: int) -> list[Chunk]:
        if not 1 <= limit <= 100:
            raise ValueError("rerank limit must be between 1 and 100")
        if not chunks:
            return []
        result = await self._post({
            "model": self.settings.bge_reranker_model,
            "query": query,
            "documents": [chunk.text for chunk in chunks],
            "topN": min(limit, len(chunks)),
        })
        ranked = result.get("results")
        if not isinstance(ranked, list):
            raise ValueError("BGE-Reranker response is missing results")
        output: list[Chunk] = []
        seen: set[int] = set()
        for item in ranked:
            if not isinstance(item, dict):
                raise ValueError("BGE-Reranker result entries must be objects")
            index = item.get("index")
            if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(chunks) or index in seen:
                raise ValueError("BGE-Reranker returned an invalid document index")
            score = item.get("relevanceScore", item.get("relevance_score", item.get("score")))
            if isinstance(score, bool) or not isinstance(score, (float, int)):
                raise ValueError("BGE-Reranker result is missing a numeric score")
            seen.add(index)
            chunk = chunks[index]
            output.append(Chunk(chunk.chunk_id, chunk.parent_id, chunk.text, float(score)))
            if len(output) >= limit:
                break
        return output

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.settings.bge_reranker_api_key}"} if self.settings.bge_reranker_api_key else {}
        if self._client is not None:
            response = await self._client.post(self.settings.bge_reranker_url, json=payload, headers=headers)
        else:
            async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds, trust_env=False) as client:
                response = await client.post(self.settings.bge_reranker_url, json=payload, headers=headers)
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, dict):
            raise TypeError("BGE-Reranker service returned a non-object response")
        return value


class ElasticsearchHybridStore:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._client = client

    async def search_dense(self, vector: Sequence[float], limit: int) -> list[Chunk]:
        body = {
            "size": limit,
            "knn": {
                "field": self.settings.elasticsearch_dense_field,
                "query_vector": list(vector),
                "k": limit,
                "num_candidates": max(limit * 4, limit),
            },
            "_source": ["parent_id", "text"],
        }
        return await self._search(body)

    async def search_sparse(self, vector: Mapping[str, float], limit: int) -> list[Chunk]:
        body = {
            "size": limit,
            "query": {
                "sparse_vector": {
                    "field": self.settings.elasticsearch_sparse_field,
                    "query_vector": {str(key): float(value) for key, value in vector.items()},
                }
            },
            "_source": ["parent_id", "text"],
        }
        return await self._search(body)

    async def get_parents(self, parent_ids: Sequence[str]) -> list[Chunk]:
        if not parent_ids:
            return []
        result = await self._request(
            "POST", f"/{self.settings.elasticsearch_index}/_mget",
            json={"ids": list(dict.fromkeys(parent_ids))},
        )
        docs = result.get("docs")
        return [self._chunk(document) for document in docs if isinstance(document, dict) and document.get("found")] if isinstance(docs, list) else []

    async def _search(self, body: dict[str, Any]) -> list[Chunk]:
        result = await self._request("POST", f"/{self.settings.elasticsearch_index}/_search", json=body)
        hits = result.get("hits", {}).get("hits", [])
        return [self._chunk(hit) for hit in hits if isinstance(hit, dict)]

    def _chunk(self, hit: Mapping[str, Any]) -> Chunk:
        source = hit.get("_source", {})
        return Chunk(
            chunk_id=str(hit.get("_id", source.get("id", ""))),
            parent_id=str(source.get("parent_id", "")),
            text=str(source.get("text", "")),
            score=float(hit.get("_score") or 0.0),
        )

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        headers = kwargs.pop("headers", {})
        if self.settings.elasticsearch_api_key:
            headers["Authorization"] = f"ApiKey {self.settings.elasticsearch_api_key}"
        if self._client is not None:
            response = await self._client.request(method, f"{self.settings.elasticsearch_url}{path}", headers=headers, **kwargs)
        else:
            async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds, trust_env=False) as client:
                response = await client.request(method, f"{self.settings.elasticsearch_url}{path}", headers=headers, **kwargs)
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, dict):
            raise TypeError("Elasticsearch returned a non-object response")
        return value

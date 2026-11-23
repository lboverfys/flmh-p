from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any
import json
import math
import hashlib
from dataclasses import replace

import httpx

from flmh_agent.config import Settings
from flmh_agent.domain.rag import Chunk
from flmh_agent.resources import complete_cleanup


class BgeM3HttpAdapter:
    """Calls the configured BGE-M3 service contract for dense and lexical vectors."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._client = client

    async def encode(self, text: str) -> tuple[list[float], dict[str, float]]:
        return (await self.encode_many([text]))[0]

    async def encode_many(self, texts: Sequence[str]) -> list[tuple[list[float], dict[str, float]]]:
        if self.settings.embedding_dimension is None:
            raise ValueError("TODO: configure BGE_M3_DIMENSION before embedding")
        if not 1 <= len(texts) <= 32 or any(not text.strip() for text in texts):
            raise ValueError("embedding input is required")
        result = await self._post(self.settings.bge_m3_url, {
            "model": self.settings.bge_m3_model,
            "input": list(texts),
            "returnDense": True,
            "returnSparse": True,
        }, self.settings.bge_m3_api_key)
        records = result.get("data")
        if not isinstance(records, list) or len(records) != len(texts):
            raise ValueError("BGE-M3 did not return one result per input")
        output = []
        for record in records:
            dense = record.get("denseVector", record.get("dense_vector", record.get("dense_vecs")))
            sparse = record.get("sparseVector", record.get("sparse_vector", record.get("lexical_weights")))
            if not isinstance(dense, list) or len(dense) != self.settings.embedding_dimension:
                raise ValueError("BGE-M3 returned an invalid dense vector dimension")
            if not isinstance(sparse, dict) or not sparse:
                raise ValueError("BGE-M3 returned no sparse lexical weights")
            if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
                   for value in [*dense, *sparse.values()]):
                raise ValueError("BGE-M3 vectors must contain finite numbers")
            output.append(([float(value) for value in dense], {str(key): float(value) for key, value in sparse.items()}))
        return output

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
            if isinstance(score, bool) or not isinstance(score, (float, int)) or not math.isfinite(score):
                raise ValueError("BGE-Reranker result is missing a numeric score")
            seen.add(index)
            chunk = chunks[index]
            output.append(replace(chunk, score=float(score)))
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
    reuse_existing_indices = True

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None, *, pit_id: str = "") -> None:
        self.settings = settings
        self._client = client
        self._pit_id = pit_id
        self._pit_ids: set[str] = {pit_id} if pit_id else set()
        self._parent_indices: dict[str, str] = {}

    def _document_index(self, document_id: str, version: str) -> str:
        digest = hashlib.sha256(f"{document_id}\n{version}".encode()).hexdigest()[:32]
        return f"{self.settings.elasticsearch_index}-doc-{digest}"

    def _document_alias(self, document_id: str) -> str:
        digest = hashlib.sha256(document_id.encode()).hexdigest()[:32]
        return f"{self.settings.elasticsearch_index}-active-{digest}"

    def _anchor_index(self, document_id: str) -> str:
        digest = hashlib.sha256(document_id.encode()).hexdigest()[:32]
        return f"{self.settings.elasticsearch_index}-anchor-{digest}"

    def _manifest_id(self, document_id: str, version: str) -> str:
        return "manifest-" + hashlib.sha256(f"{document_id}\n{version}".encode()).hexdigest()

    @property
    def _published_aliases(self) -> str:
        return f"{self.settings.elasticsearch_index}-active-*"

    async def active_corpus_version(self) -> str:
        try:
            aliases = await self._request("GET", f"/_alias/{self._published_aliases}", params={"filter_path": "*.aliases"})
        except httpx.HTTPStatusError as error:
            if error.response.status_code == 404:
                return ""
            raise
        # Concrete document indices are content-addressed and sealed before alias publication.
        return hashlib.sha256("\n".join(sorted(aliases)).encode()).hexdigest() if aliases else ""

    def snapshot(self):
        return self._open_snapshot(self._published_aliases, keep_alive=self.settings.rag_pit_keep_alive, allow_missing=True)

    @asynccontextmanager
    async def _open_snapshot(self, target: str, *, keep_alive: str, allow_missing: bool):
        try:
            opened = await self._request("POST", f"/{target}/_pit?keep_alive={keep_alive}&allow_partial_search_results=false")
        except httpx.HTTPStatusError as failure:
            if not allow_missing or failure.response.status_code != 404 or failure.response.json().get("error", {}).get("type") != "index_not_found_exception":
                raise
            yield ElasticsearchHybridStore(self.settings, self._client, pit_id="NO_PUBLISHED_INDEX")
            return
        pit_id = opened.get("id")
        if not isinstance(pit_id, str) or not pit_id:
            raise ValueError("Elasticsearch did not return a point-in-time ID")
        snapshot = type(self)(self.settings, self._client, pit_id=pit_id)
        original = None
        try:
            yield snapshot
        except BaseException as failure:
            original = failure
            raise
        finally:
            await complete_cleanup(self._close_snapshots(snapshot._pit_ids), original)

    async def _close_snapshots(self, identifiers: set[str]) -> None:
        failures: list[Exception] = []
        for identifier in sorted(identifiers):
            try:
                await self._request("DELETE", "/_pit", json={"id": identifier})
            except Exception as failure:
                failures.append(failure)
        if failures:
            raise ExceptionGroup("Elasticsearch PIT cleanup failed", failures)

    async def search_dense(self, vector: Sequence[float], limit: int) -> list[Chunk]:
        if self._pit_id == "NO_PUBLISHED_INDEX":
            return []
        return await self._search(self._dense_query(vector, limit))

    def _dense_query(self, vector: Sequence[float], limit: int) -> dict:
        return {
            "size": limit,
            "knn": {
                "field": self.settings.elasticsearch_dense_field,
                "query_vector": list(vector),
                "k": limit,
                "num_candidates": max(limit * 4, limit),
                "filter": {"term": {"kind": "child"}},
            },
            "_source": ["parent_id", "text", "source_ref", "document_id", "version"],
        }

    async def search_sparse(self, vector: Mapping[str, float], limit: int) -> list[Chunk]:
        if self._pit_id == "NO_PUBLISHED_INDEX":
            return []
        return await self._search(self._sparse_query(vector, limit))

    def _sparse_query(self, vector: Mapping[str, float], limit: int) -> dict:
        return {
            "size": limit,
            "query": {"bool": {"filter": [{"term": {"kind": "child"}}], "must": {
                "sparse_vector": {
                    "field": self.settings.elasticsearch_sparse_field,
                    "query_vector": {str(key): float(value) for key, value in vector.items()},
                }
            }}},
            "_source": ["parent_id", "text", "source_ref", "document_id", "version"],
        }

    async def get_parents(self, parent_ids: Sequence[str]) -> list[Chunk]:
        if not parent_ids or self._pit_id == "NO_PUBLISHED_INDEX":
            return []
        docs = [{"_index": self._parent_indices[parent_id], "_id": parent_id}
                for parent_id in dict.fromkeys(parent_ids)]
        result = await self._request("POST", "/_mget", json={"docs": docs})
        documents = result.get("docs", [])
        if len(documents) != len(docs) or any(not document.get("found") for document in documents):
            raise RuntimeError("Elasticsearch could not return every parent from the published version")
        return [self._chunk(document) for document in documents]

    async def _search(self, body: dict[str, Any]) -> list[Chunk]:
        if self._pit_id:
            body["pit"] = {"id": self._pit_id, "keep_alive": self.settings.rag_pit_keep_alive}
            result = await self._request("POST", "/_search?allow_partial_search_results=false", json=body)
        else:
            result = await self._request("POST", f"/{self._published_aliases}/_search?allow_partial_search_results=false", json=body)
        return self._search_hits(result)

    def _search_hits(self, result: dict) -> list[Chunk]:
        returned_id = result.get("pit_id")
        if isinstance(returned_id, str) and returned_id:
            self._pit_id = returned_id
            self._pit_ids.add(returned_id)
        if result.get("timed_out") or result.get("_shards", {}).get("failed", 0):
            raise RuntimeError("Elasticsearch did not complete every published-version search shard")
        hits = result.get("hits", {}).get("hits")
        if not isinstance(hits, list):
            raise ValueError("Elasticsearch search response is missing hits")
        return [self._chunk(hit) for hit in hits]

    def _chunk(self, hit: Mapping[str, Any]) -> Chunk:
        source = hit.get("_source", {})
        if source.get("parent_id") and hit.get("_index"):
            self._parent_indices[source["parent_id"]] = str(hit["_index"])
        return Chunk(
            chunk_id=str(hit.get("_id", source.get("id", ""))),
            parent_id=str(source.get("parent_id", "")),
            text=str(source.get("text", "")),
            score=float(hit.get("_score") or 0.0),
            source_ref=str(source.get("source_ref", "")),
            document_id=str(source.get("document_id", "")),
            version=str(source.get("version", "")),
        )

    async def ensure_index(self, document_id: str, version: str) -> None:
        if self.settings.embedding_dimension is None:
            raise ValueError("TODO: configure BGE_M3_DIMENSION before creating vector indices")
        properties = {name: {"type": "keyword"} for name in ("parent_id", "document_id", "version", "kind", "source_ref")}
        properties.update({"text": {"type": "text"},
            self.settings.elasticsearch_dense_field: {"type": "dense_vector", "dims": self.settings.embedding_dimension, "index": True, "similarity": "cosine"},
            self.settings.elasticsearch_sparse_field: {"type": "sparse_vector"}})
        definitions = (
            (self._anchor_index(document_id), {"mappings": {"properties": properties},
                                               "aliases": {self._document_alias(document_id): {}}}),
            (self._document_index(document_id, version), {"mappings": {"properties": properties}}),
        )
        for index, definition in definitions:
            try:
                await self._request("PUT", f"/{index}", json=definition)
            except httpx.HTTPStatusError as failure:
                if (not self.reuse_existing_indices or failure.response.status_code != 400
                        or failure.response.json().get("error", {}).get("type") != "resource_already_exists_exception"):
                    raise

    async def upsert_chunks(self, chunks: Sequence[Chunk], vectors: Sequence[tuple[Sequence[float], Mapping[str, float]]] | None = None) -> None:
        if not 1 <= len(chunks) <= 32 or vectors is not None and len(chunks) != len(vectors):
            raise ValueError("ingestion batch must contain 1 to 32 matching chunks and vectors")
        document_id, version = chunks[0].document_id, chunks[0].version
        if not document_id or not version or any(chunk.document_id != document_id or chunk.version != version for chunk in chunks):
            raise ValueError("ingestion batch must belong to one document version")
        lines = []
        for index, chunk in enumerate(chunks):
            lines.append(json.dumps({"index": {"_id": chunk.chunk_id}}, separators=(",", ":")))
            document = {"parent_id": chunk.parent_id, "text": chunk.text, "source_ref": chunk.source_ref,
                        "document_id": chunk.document_id, "version": chunk.version, "kind": "child" if chunk.parent_id else "parent"}
            if vectors is not None:
                document[self.settings.elasticsearch_dense_field] = list(vectors[index][0])
                document[self.settings.elasticsearch_sparse_field] = dict(vectors[index][1])
            lines.append(json.dumps(document, ensure_ascii=False, separators=(",", ":")))
        response = await self._request("POST", f"/{self._document_index(document_id, version)}/_bulk", content="\n".join(lines)+"\n",
                                       headers={"content-type": "application/x-ndjson"})
        items = response.get("items", [])
        if len(items) != len(chunks) or any(not 200 <= item.get("index", {}).get("status", 0) < 300 for item in items):
            raise RuntimeError("Elasticsearch ingestion batch was not fully acknowledged")

    async def reserve_document(self, document_id: str, version: str, source_ref: str, content_hash: str) -> str:
        if self.settings.embedding_dimension is None:
            raise ValueError("TODO: configure BGE_M3_DIMENSION before reserving a document")
        identifier = self._manifest_id(document_id, version)
        document = {"document_id": document_id, "version": version, "source_ref": source_ref, "content_hash": content_hash,
                    "embedding_model": self.settings.bge_m3_model, "embedding_dimension": self.settings.embedding_dimension,
                    "kind": "manifest"}
        index = self._document_index(document_id, version)
        current = await self._request("GET", f"/_alias/{self._document_alias(document_id)}")
        if len(current) != 1:
            raise RuntimeError("document publication alias must select exactly one index")
        base_index = next(iter(current))
        try:
            existing = await self._request("GET", f"/{index}/_doc/{identifier}")
        except httpx.HTTPStatusError as missing:
            if missing.response.status_code != 404:
                raise
            existing = None
        if existing is not None and any(existing.get("_source", {}).get(key) != value for key, value in document.items()):
            raise ValueError("document version already belongs to different content or source; choose a new version")
        try:
            if existing is None:
                document["base_index"] = base_index
                await self._request("PUT", f"/{index}/_create/{identifier}", json=document)
                existing = {"_source": document}
        except httpx.HTTPStatusError as conflict:
            if conflict.response.status_code != 409:
                raise
            existing = await self._request("GET", f"/{index}/_doc/{identifier}")
            if any(existing.get("_source", {}).get(key) != value for key, value in document.items() if key != "base_index"):
                raise ValueError("document version already belongs to different content or source; choose a new version") from conflict
        if index in current:
            return "PUBLISHED"
        if existing.get("_source", {}).get("base_index") != base_index:
            raise ValueError("document publication baseline changed; choose a new version instead of overwriting a newer publication")
        metadata = await self._request("GET", f"/{index}/_settings?flat_settings=true")
        blocked = metadata.get(index, {}).get("settings", {}).get("index.blocks.write")
        return "SEALED" if blocked in (True, "true") else "STAGING"

    async def publish_document(self, document_id: str, version: str, parents: int, children: int) -> None:
        index = self._document_index(document_id, version)
        manifest = await self._request("GET", f"/{index}/_doc/{self._manifest_id(document_id, version)}")
        base_index = manifest.get("_source", {}).get("base_index")
        if not isinstance(base_index, str) or not base_index:
            raise ValueError("ingestion manifest has no publication baseline")
        sealed = await self._request("PUT", f"/{index}/_block/write")
        if not sealed.get("acknowledged") or sealed.get("errors"):
            raise RuntimeError("Elasticsearch did not acknowledge the immutable version block")
        await self._request("POST", f"/{index}/_refresh")
        for kind, expected in (("parent", parents), ("child", children)):
            result = await self._request("POST", f"/{index}/_count", json={"query": {"term": {"kind": kind}}})
            if result.get("count") != expected:
                raise RuntimeError(f"Elasticsearch {kind} count differs from completed ingestion")
        alias = self._document_alias(document_id)
        published = await self._request("POST", "/_aliases", json={"actions": [
            {"remove": {"index": base_index, "alias": alias, "must_exist": True}},
            {"add": {"index": index, "alias": alias}},
        ]})
        if not published.get("acknowledged") or published.get("errors"):
            raise RuntimeError("Elasticsearch did not acknowledge the version publication")
        current = await self._request("GET", f"/_alias/{alias}")
        if set(current) != {index}:
            raise RuntimeError("Elasticsearch publication was superseded or did not select exactly one version")

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

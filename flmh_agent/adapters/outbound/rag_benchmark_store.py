"""Batch both evaluation arms over one PIT on the explicitly published corpus."""
from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from contextlib import asynccontextmanager

import httpx

from flmh_agent.adapters.outbound.bge_http import ElasticsearchHybridStore
from flmh_agent.config import Settings
from flmh_agent.domain.rag import Chunk
from flmh_agent.rag_corpus import Corpus, sha256


class BenchmarkStore(ElasticsearchHybridStore):
    reuse_existing_indices = False

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None, *, pit_id: str = ""):
        if not re.fullmatch(r"rag-eval-[a-z0-9][a-z0-9_-]*", settings.elasticsearch_index):
            raise ValueError("choose an explicit dedicated rag-eval-... index prefix")
        super().__init__(settings, client, pit_id=pit_id)

    async def require_unused_prefix(self) -> None:
        prefix = self.settings.elasticsearch_index
        try:
            resolved = await self._request("GET", f"/_resolve/index/{prefix}-*?expand_wildcards=all")
        except httpx.HTTPStatusError as error:
            if error.response.status_code == 404 and error.response.json().get("error", {}).get("type") == "index_not_found_exception":
                return
            raise
        if any(resolved[key] for key in ("indices", "aliases", "data_streams")):
            raise ValueError(f"prefix {prefix} is already in use; choose a new one")

    @asynccontextmanager
    async def corpus_snapshot(self, corpus: Corpus):
        documents = {self._document_index(doc.document_id, corpus.version): doc for doc in corpus.documents.values()}
        target = ",".join(documents)
        metadata = await self._request("GET", f"/{target}?flat_settings=true")
        requests = []
        for index, doc in documents.items():
            entry = metadata[index]
            fields = entry["mappings"]["properties"]
            dense = fields.get(self.settings.elasticsearch_dense_field, {})
            if (self._document_alias(doc.document_id) not in entry["aliases"]
                    or entry["settings"].get("index.blocks.write") not in (True, "true")
                    or dense.get("type") != "dense_vector" or dense.get("dims") != self.settings.embedding_dimension
                    or fields.get(self.settings.elasticsearch_sparse_field, {}).get("type") != "sparse_vector"):
                raise ValueError(f"index must be published, sealed and use the configured vector mapping: {index}")
            requests.append({"_index": index, "_id": self._manifest_id(doc.document_id, corpus.version)})
        result = await self._request("POST", "/_mget", json={"docs": requests})
        for request, item in zip(requests, result["docs"], strict=True):
            doc = documents[request["_index"]]
            expected = {"kind": "manifest", "document_id": doc.document_id, "version": corpus.version,
                        "source_ref": doc.source_ref, "content_hash": doc.content_hash,
                        "embedding_model": self.settings.bge_m3_model, "embedding_dimension": self.settings.embedding_dimension}
            if (not item.get("found") or item.get("_index") != request["_index"] or item.get("_id") != request["_id"]
                    or any(item["_source"].get(key) != value for key, value in expected.items())):
                raise ValueError(f"indexed corpus or embedding model differs from source manifest: {request['_index']}")
        async with self._open_snapshot(target, keep_alive="10m", allow_missing=False) as snapshot:
            yield snapshot, list(documents), sha256(snapshot._pit_id)

    async def search_comparison_batch(self, vectors: Sequence[tuple[Sequence[float], Mapping[str, float]]],
                                      k: int, depth: int) -> list[tuple[list[Chunk], list[Chunk], list[Chunk]]]:
        if not self._pit_id or not 1 <= len(vectors) <= 8 or not 1 <= k <= depth <= 100:
            raise ValueError("a pinned PIT and bounded comparison batch are required")
        lines = []
        for dense, sparse in vectors:
            queries = [self._dense_query(dense, k)]
            if k != depth:
                queries.append(self._dense_query(dense, depth))
            queries.append(self._sparse_query(sparse, depth))
            for query in queries:
                query["pit"] = {"id": self._pit_id, "keep_alive": "10m"}
                lines.extend(("{}", json.dumps(query, separators=(",", ":"))))
        response = await self._request("POST", "/_msearch", content="\n".join(lines) + "\n",
                                       headers={"content-type": "application/x-ndjson"})
        responses = response["responses"]
        # Retain even the siblings of a failed subsearch for PIT cleanup.
        self._pit_ids.update(item["pit_id"] for item in responses if item.get("pit_id"))
        if len(responses) != len(lines) // 2:
            raise RuntimeError("Elasticsearch did not return every comparison subsearch")
        for index, item in enumerate(responses):
            if "error" in item:
                raise RuntimeError(f"Elasticsearch comparison subsearch {index} failed: {item['error']}")
        rankings = (self._search_hits(item) for item in responses)
        output = []
        for _ in vectors:
            baseline = next(rankings)
            hybrid_dense = next(rankings) if k != depth else baseline
            output.append((baseline, hybrid_dense, next(rankings)))
        return output

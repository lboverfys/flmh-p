from __future__ import annotations

import hashlib
import itertools
from collections.abc import Iterator

from flmh_agent.domain.rag import Chunk
from flmh_agent.ports.rag import HybridEmbeddingPort, KnowledgeIngestionStorePort


def chunk_document(document_id: str, version: str, source_ref: str, text: str) -> Iterator[tuple[Chunk, list[Chunk]]]:
    if not document_id.strip() or not version.strip() or not source_ref.strip() or not text.strip():
        raise ValueError("document_id, version, source_ref and text are required")
    if len(text.encode("utf-8")) > 1024 * 1024:
        raise ValueError("one ingestion document must not exceed 1 MiB")
    prefix = hashlib.sha256(f"{document_id}\n{version}".encode()).hexdigest()[:32]
    for parent_index, offset in enumerate(range(0, len(text), 3000)):
        parent_text = text[offset:offset+3000]
        parent = Chunk(f"{prefix}-p{parent_index}", "", parent_text, source_ref=source_ref, document_id=document_id, version=version)
        children = [Chunk(f"{parent.chunk_id}-c{index}", parent.chunk_id, parent_text[start:start+800],
                          source_ref=source_ref, document_id=document_id, version=version)
                    for index, start in enumerate(range(0, len(parent_text), 700))]
        yield parent, children


class KnowledgeIngestion:
    def __init__(self, embedder: HybridEmbeddingPort, store: KnowledgeIngestionStorePort):
        self.embedder = embedder
        self.store = store

    async def ingest(self, document_id: str, version: str, source_ref: str, text: str) -> dict:
        groups = chunk_document(document_id, version, source_ref, text)
        first = next(groups)
        await self.store.ensure_index(document_id, version)
        reservation = await self.store.reserve_document(document_id, version, source_ref, hashlib.sha256(text.encode("utf-8")).hexdigest())
        if reservation == "PUBLISHED":
            return {"documentId": document_id, "version": version, "sourceRef": source_ref,
                    "mode": "ALREADY_PUBLISHED", "source": "CONFIGURED_ADAPTERS"}
        if reservation == "SEALED":
            parent_count = child_count = 0
            for parent, chunks in itertools.chain((first,), groups):
                parent_count += 1
                child_count += len(chunks)
            await self.store.publish_document(document_id, version, parent_count, child_count)
            return {"documentId": document_id, "version": version, "sourceRef": source_ref,
                    "parents": parent_count, "children": child_count, "embeddingBatches": 0,
                    "mode": "VERSIONED_ALIAS_PUBLICATION", "source": "CONFIGURED_ADAPTERS"}
        parents: list[Chunk] = []
        children: list[Chunk] = []
        parent_count = child_count = batch_count = 0

        async def flush() -> None:
            nonlocal child_count, parent_count, batch_count
            if parents:
                await self.store.upsert_chunks(parents)
                parent_count += len(parents)
                parents.clear()
            if children:
                vectors = await self.embedder.encode_many([chunk.text for chunk in children])
                await self.store.upsert_chunks(children, vectors)
                child_count += len(children)
                batch_count += 1
                children.clear()

        for parent, chunks in itertools.chain((first,), groups):
            if len(children) + len(chunks) > 32:
                await flush()
            parents.append(parent)
            children.extend(chunks)
        await flush()
        await self.store.publish_document(document_id, version, parent_count, child_count)
        return {"documentId": document_id, "version": version, "sourceRef": source_ref,
                "parents": parent_count, "children": child_count, "embeddingBatches": batch_count,
                "mode": "VERSIONED_ALIAS_PUBLICATION", "source": "CONFIGURED_ADAPTERS"}

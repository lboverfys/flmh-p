"""Prepare, review, index and record the versioned operations retrieval benchmark."""
from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import os
from bisect import bisect_right
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TextIO

import httpx

from flmh_agent.adapters.outbound.bge_http import BgeM3HttpAdapter, BgeRerankerHttpAdapter
from flmh_agent.adapters.outbound.rag_benchmark_store import BenchmarkStore
from flmh_agent.application.ingestion import KnowledgeIngestion
from flmh_agent.application.rag import hybrid_child_ranking, ranked_parent_ids
from flmh_agent.config import Settings
from flmh_agent.evaluate import Judgments, Rankings, Record, Recording
from flmh_agent.ports.rag import HybridEmbeddingPort, RerankerPort
from flmh_agent.rag_corpus import Corpus, Review, freeze_review, load_corpus, read_small, review_cases

EXAMPLE = Path("examples/rag-evaluation/operations-v1")


@contextmanager
def new_bundle(destination: Path):
    if os.path.lexists(destination):
        raise FileExistsError(destination)
    with TemporaryDirectory(prefix=".rag-work-", dir=destination.parent) as temp:
        stage = Path(temp) / "bundle"
        stage.mkdir()
        yield stage
        if os.path.lexists(destination):
            raise FileExistsError(destination)
        stage.rename(destination)


def write_model(path: Path, model: Record) -> None:
    with path.open("x", encoding="utf-8") as output:
        print(model.model_dump_json(indent=2), file=output)


def prepare(corpus: Corpus, review: Review, destination: Path) -> None:
    review_cases(corpus, review, freeze=False)
    with new_bundle(destination) as stage:
        write_model(stage / "review.json", review)
        with (stage / "chunks.jsonl").open("x", encoding="utf-8") as output:
            for key, document in corpus.documents.items():
                for parent, children in document.groups:
                    for chunk in (parent, *children):
                        first_child = chunk.chunk_id if chunk.parent_id else children[0].chunk_id
                        start = corpus.children[first_child][1]
                        row = {"document": key, "document_id": document.document_id, "version": corpus.version,
                               "id": chunk.chunk_id, "parent_id": chunk.parent_id, "source_ref": chunk.source_ref,
                               "lines": [bisect_right(document.offsets, start),
                                         bisect_right(document.offsets, start + len(chunk.text) - 1)], "text": chunk.text}
                        print(json.dumps(row, ensure_ascii=False, separators=(",", ":")), file=output)


async def index_corpus(corpus: Corpus, store: BenchmarkStore, embedder: HybridEmbeddingPort) -> dict:
    await store.require_unused_prefix()
    ingestion = KnowledgeIngestion(embedder, store)
    receipts = []
    for doc in corpus.documents.values():
        receipts.append(await ingestion.ingest_groups(doc.document_id, corpus.version, doc.source_ref,
                                                      doc.content_hash, iter(doc.groups)))
    return {"dataset": corpus.dataset, "corpus_sha256": corpus.version,
            "index_prefix": store.settings.elasticsearch_index, "documents": receipts}


async def record_pair(corpus: Corpus, gold: Judgments, store: BenchmarkStore, embedder: HybridEmbeddingPort,
                      reranker: RerankerPort, trace: TextIO, *, k: int, multiplier: int, batch_size: int) -> list[Rankings]:
    if (gold.dataset != corpus.dataset or gold.corpus_sha256 != corpus.version or gold.granularity != "parent"
            or not gold.reviewer.strip() or not gold.cases):
        raise ValueError("use frozen, reviewed parent-level judgments for this corpus")
    if any(parent not in corpus.parents for case in gold.cases for parent in case.relevant):
        raise ValueError("unknown relevant parent in frozen judgments")
    depth = k * multiplier
    if not 1 <= batch_size <= 8 or not 1 <= multiplier <= 10 or not 1 <= k <= depth <= 100:
        raise ValueError("require batch size 1..8, multiplier 1..10 and 1 <= k <= k*multiplier <= 100")
    dense_rows: dict[str, list[str]] = {}
    hybrid_rows: dict[str, list[str]] = {}
    async with store.corpus_snapshot(corpus) as (published, indices, pit_hash):
        for batch in itertools.batched(gold.cases, batch_size):
            vectors = await embedder.encode_many([case.question for case in batch])
            recalls = await published.search_comparison_batch(vectors, k, depth)
            tasks = []
            for baseline, dense, sparse in recalls:
                for hit in itertools.chain(baseline, dense, sparse):
                    expected = corpus.children.get(hit.chunk_id)
                    if expected is None or replace(hit, score=0.0) != expected[0]:
                        raise ValueError(f"retrieved child differs from pinned corpus: {hit.chunk_id}")
            async with asyncio.TaskGroup() as group:
                for case, (_, dense, sparse) in zip(batch, recalls, strict=True):
                    tasks.append(group.create_task(hybrid_child_ranking(case.question, dense, sparse, reranker, depth)))
            needed: set[str] = set()
            for case, (baseline, dense, sparse), task in zip(batch, recalls, tasks, strict=True):
                fused, reranked = task.result()
                if len(reranked) != min(depth, len(fused)):
                    raise ValueError(f"incomplete reranker response for {case.id}")
                dense_rows[case.id] = ranked_parent_ids(baseline, k)
                hybrid_rows[case.id] = ranked_parent_ids(reranked, k)
                needed.update(dense_rows[case.id])
                needed.update(hybrid_rows[case.id])
                stages = {"dense_only": baseline, "hybrid_dense": dense, "hybrid_sparse": sparse,
                          "rrf": fused, "reranked": reranked}
                print(json.dumps({"id": case.id, **{name: [c.chunk_id for c in hits] for name, hits in stages.items()}}),
                      file=trace)
            # Both arms hydrate the union from the same sealed physical indices in one mget.
            parents = await published.get_parents(sorted(needed))
            if ({chunk.chunk_id for chunk in parents} != needed
                    or any(replace(chunk, score=0.0) != corpus.parents[chunk.chunk_id] for chunk in parents)):
                raise ValueError("returned parents differ from the pinned source chunks")
    settings = store.settings
    meta = Recording(judgments_sha256=gold.fingerprint(), corpus_sha256=corpus.version, pit_sha256=pit_hash,
                     recorded_at_utc=datetime.now(UTC).isoformat(), indices=indices, embedding_model=settings.bge_m3_model,
                     embedding_dimension=settings.embedding_dimension, reranker_model=settings.bge_reranker_model,
                     ranking_depth=k, candidate_multiplier=multiplier)
    return [Rankings(name=name, dataset=gold.dataset, source="RECORDED_RETRIEVAL", granularity="parent",
                     recording=meta, rankings=rows) for name, rows in (("dense-only", dense_rows), ("hybrid", hybrid_rows))]


async def use_services(args: argparse.Namespace, corpus: Corpus) -> None:
    env = {key: value for key, value in os.environ.items()
           if key.startswith(("BGE_", "ELASTICSEARCH_")) or key in {"EMBEDDING_BACKEND", "REQUEST_TIMEOUT_SECONDS"}}
    settings = Settings.from_env({**env, "ELASTICSEARCH_INDEX": args.index_prefix})
    if settings.embedding_backend != "bge_m3_http":
        raise ValueError("set EMBEDDING_BACKEND=bge_m3_http and the BGE/ES endpoint settings")
    async with httpx.AsyncClient(timeout=settings.request_timeout_seconds, trust_env=False) as client:
        store = BenchmarkStore(settings, client)
        embedder = BgeM3HttpAdapter(settings, client)
        if args.command == "index":
            print(json.dumps(await index_corpus(corpus, store, embedder), ensure_ascii=False, indent=2))
            return
        gold = Judgments.model_validate_json(read_small(args.judgments))
        with new_bundle(args.out_dir) as stage:
            with (stage / "trace.jsonl").open("x", encoding="utf-8") as trace:
                dense, hybrid = await record_pair(corpus, gold, store, embedder, BgeRerankerHttpAdapter(settings, client),
                                                  trace, k=args.k, multiplier=args.candidate_multiplier, batch_size=args.batch_size)
            write_model(stage / "dense.json", dense)
            write_model(stage / "hybrid.json", hybrid)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    local = argparse.ArgumentParser(add_help=False)
    local.add_argument("--source-root", required=True, type=Path, help="authorized local flmh checkout containing docs/")
    local.add_argument("--manifest", type=Path, default=EXAMPLE / "sources.json")
    prep = sub.add_parser("prepare", parents=[local], help="write the local review sheet and exact source chunks")
    prep.add_argument("--candidates", type=Path, default=EXAMPLE / "candidates.json")
    prep.add_argument("--out-dir", required=True, type=Path)
    freeze = sub.add_parser("freeze", parents=[local], help="validate human decisions and export parent qrels")
    freeze.add_argument("--review", required=True, type=Path)
    freeze.add_argument("--out", required=True, type=Path)
    online = argparse.ArgumentParser(parents=[local], add_help=False)
    online.add_argument("--index-prefix", required=True, help="dedicated rag-eval-... prefix, new for index")
    sub.add_parser("index", parents=[online], help="publish source documents via configured BGE and Elasticsearch")
    record = sub.add_parser("record", parents=[online], help="record paired retrieval from published immutable indices")
    record.add_argument("--judgments", required=True, type=Path)
    record.add_argument("--out-dir", required=True, type=Path)
    record.add_argument("--k", type=int, default=5)
    record.add_argument("--candidate-multiplier", type=int, default=4)
    record.add_argument("--batch-size", type=int, default=8)
    args = parser.parse_args()
    try:
        corpus = load_corpus(args.source_root, args.manifest)
        if args.command == "prepare":
            prepare(corpus, Review.model_validate_json(read_small(args.candidates)), args.out_dir)
        elif args.command == "freeze":
            review = Review.model_validate_json(read_small(args.review))
            write_model(args.out, freeze_review(corpus, review))
        else:
            asyncio.run(use_services(args, corpus))
    except (OSError, ValueError, RuntimeError, httpx.HTTPError) as error:
        parser.exit(2, f"benchmark failed: {error}\n")


if __name__ == "__main__":
    main()

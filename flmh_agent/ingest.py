from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from flmh_agent.adapters.outbound.bge_http import BgeM3HttpAdapter, ElasticsearchHybridStore
from flmh_agent.application.ingestion import KnowledgeIngestion
from flmh_agent.config import Settings


def main() -> None:
    parser = argparse.ArgumentParser(description="Import one versioned UTF-8 text document into the configured knowledge index")
    parser.add_argument("--file", required=True, type=Path)
    parser.add_argument("--document-id", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--source-ref", required=True)
    args = parser.parse_args()
    settings = Settings.from_env()
    if not settings.embedding_backend:
        parser.error("TODO: configure the embedding model and Elasticsearch before ingestion")
    if args.file.stat().st_size > 1024 * 1024:
        parser.error("one input file must not exceed 1 MiB")
    service = KnowledgeIngestion(BgeM3HttpAdapter(settings), ElasticsearchHybridStore(settings))
    result = asyncio.run(service.ingest(args.document_id, args.version, args.source_ref, args.file.read_text(encoding="utf-8")))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

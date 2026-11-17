from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    parent_id: str
    text: str
    score: float = 0.0
    source_ref: str = ""
    document_id: str = ""
    version: str = ""

"""Pin local source ranges and resolve review evidence against the actual ingestion chunks."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from itertools import accumulate
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SerializerFunctionWrapHandler, model_serializer

from flmh_agent.application.ingestion import CHILD_SIZE, CHILD_STRIDE, PARENT_SIZE, chunk_document
from flmh_agent.domain.rag import Chunk
from flmh_agent.evaluate import Case, Digest, Identifier, Judgments, Record

LAYOUT = f"section-parent{PARENT_SIZE}-child{CHILD_SIZE}-step{CHILD_STRIDE}-v1"


def read_small(path: Path) -> bytes:
    with path.open("rb") as stream:
        data = stream.read(1024 * 1024 + 1)
    if len(data) > 1024 * 1024:
        raise ValueError(f"input exceeds 1 MiB: {path}")
    return data


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Source(Record):
    id: Identifier
    path: Annotated[str, Field(pattern=r"^(?:docs|examples/rag-evaluation/operations-v3/corpus)/[a-zA-Z0-9_./-]+\.md$")]
    sha256: Digest
    sections: list[tuple[int, int]] = Field(min_length=1, max_length=100)


class Manifest(Record):
    dataset: Identifier
    revision: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    documents: list[Source] = Field(min_length=1, max_length=16)
    child_size: int | None = Field(default=None, ge=100, le=3000)
    child_stride: int | None = Field(default=None, ge=80, le=3000)

    @model_serializer(mode="wrap")
    def _serialize_compact(self, handler: SerializerFunctionWrapHandler) -> dict:
        data = handler(self)
        if data.get("child_size") is None:
            data.pop("child_size", None)
        if data.get("child_stride") is None:
            data.pop("child_stride", None)
        return data


class Evidence(Record):
    document: Identifier
    line: int = Field(ge=1)
    quote: Identifier
    parent: Identifier
    child: Identifier


class Candidate(Record):
    id: Identifier
    question: Identifier
    status: Literal["pending", "approved", "rejected"]
    evidence: list[Evidence] = Field(max_length=32)
    equivalent_groups: list[list[Identifier]] = Field(default_factory=list, exclude_if=lambda groups: not groups)


class Review(Record):
    dataset: Identifier
    corpus_sha256: Digest
    reviewer: str
    cases: list[Candidate] = Field(min_length=1)


@dataclass
class Document:
    document_id: str
    source_ref: str
    content_hash: str
    lines: list[str]
    offsets: list[int]
    groups: list[tuple[Chunk, list[Chunk]]]


@dataclass
class Corpus:
    dataset: str
    version: str
    documents: dict[str, Document]
    parents: dict[str, Chunk]
    children: dict[str, tuple[Chunk, int]]  # child and its start in the normalized source file

    def check_evidence(self, evidence: Evidence) -> None:
        document = self.documents.get(evidence.document)
        location = self.children.get(evidence.child)
        if document is None or location is None or evidence.line > len(document.lines):
            raise ValueError("unknown source, line or child")
        line = document.lines[evidence.line - 1].rstrip("\n")
        if not evidence.quote.strip() or line.count(evidence.quote) != 1:
            raise ValueError("quote must occur exactly once on the cited line")
        child, start = location
        relative = document.offsets[evidence.line - 1] + line.index(evidence.quote) - start
        if (child.document_id != document.document_id or child.parent_id != evidence.parent or relative < 0
                or not child.text.startswith(evidence.quote, relative)):
            raise ValueError("cited child/parent does not cover this occurrence of the quote")


def load_corpus(root: Path, manifest_path: Path) -> Corpus:
    manifest = Manifest.model_validate_json(read_small(manifest_path))
    version = sha256(LAYOUT + "\n" + manifest.model_dump_json(exclude_none=True))
    child_size = manifest.child_size or CHILD_SIZE
    child_stride = manifest.child_stride or CHILD_STRIDE
    corpus = Corpus(manifest.dataset, version, {}, {}, {})
    root = root.resolve(strict=True)
    if len({doc.path for doc in manifest.documents}) != len(manifest.documents):
        raise ValueError("duplicate source path")
    allowed_roots = (root / "docs", root / "examples" / "rag-evaluation" / "operations-v3" / "corpus")
    for source in manifest.documents:
        path = (root / source.path).resolve(strict=True)
        if not any(path.is_relative_to(allowed) for allowed in allowed_roots) or source.id in corpus.documents:
            raise ValueError(f"source must be unique and inside allowed corpus roots: {source.id}")
        text = read_small(path).decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
        if sha256(text) != source.sha256:
            raise ValueError(f"source digest mismatch: {source.path} at {manifest.revision}")
        lines = text.splitlines(keepends=True)
        offsets = list(accumulate(map(len, lines), initial=0))
        doc_id = f"flmh/{source.path}"
        ref = f"flmh@{manifest.revision}:{source.path}"
        groups: list[tuple[Chunk, list[Chunk]]] = []
        selected = hashlib.sha256()
        previous = 0
        for first, last in source.sections:
            if not previous < first <= last <= len(lines):
                raise ValueError(f"unordered or invalid line range {source.id}:{first}-{last}")
            previous = last
            start = offsets[first - 1]
            excerpt = text[start:offsets[last]]
            selected.update(excerpt.encode("utf-8"))
            for parent, children in chunk_document(doc_id, version, f"{ref}#L{first}-L{last}", excerpt,
                                                   parent_start=len(groups), child_size=child_size,
                                                   child_stride=child_stride):
                groups.append((parent, children))
                corpus.parents[parent.chunk_id] = parent
                for index, child in enumerate(children):
                    corpus.children[child.chunk_id] = (child, start + index * child_stride)
                start += len(parent.text)
        corpus.documents[source.id] = Document(doc_id, ref, selected.hexdigest(), lines, offsets, groups)
    return corpus


def review_cases(corpus: Corpus, review: Review, *, freeze: bool, min_approved: int = 1) -> list[Case]:
    if review.dataset != corpus.dataset or review.corpus_sha256 != corpus.version:
        raise ValueError("review belongs to another dataset or source snapshot")
    if min_approved < 1:
        raise ValueError("min_approved must be positive")
    approved = sum(case.status == "approved" for case in review.cases)
    if freeze and approved < min_approved:
        raise ValueError(f"freeze requires at least {min_approved} approved cases; found {approved}; a named reviewer is required")
    if freeze and (not review.reviewer.strip() or any(case.status == "pending" for case in review.cases)):
        raise ValueError("a named reviewer and a decision for every question are required")
    if (len({c.id for c in review.cases}) != len(review.cases)
            or len({c.question.strip() for c in review.cases}) != len(review.cases)):
        raise ValueError("duplicate case id or question")
    cases = []
    for case in review.cases:
        if case.status == "rejected":
            continue
        if not case.evidence or not case.question.strip():
            raise ValueError(f"{case.id}: a question and source evidence are required")
        for evidence in case.evidence:
            try:
                corpus.check_evidence(evidence)
            except ValueError as error:
                raise ValueError(f"{case.id} {evidence.document}:{evidence.line}: {error}") from error
        cases.append(Case(id=case.id, question=case.question, relevant=list(dict.fromkeys(e.parent for e in case.evidence)),
                          equivalent_groups=case.equivalent_groups))
    return cases


def freeze_review(corpus: Corpus, review: Review, *, min_approved: int | None = None) -> Judgments:
    minimum = (300 if corpus.dataset == "flmh-radar-ops-v3" else 1) if min_approved is None else min_approved
    cases = review_cases(corpus, review, freeze=True, min_approved=minimum)
    return Judgments(dataset=corpus.dataset, corpus_sha256=corpus.version, granularity="parent",
                     reviewer=review.reviewer.strip(), cases=cases)

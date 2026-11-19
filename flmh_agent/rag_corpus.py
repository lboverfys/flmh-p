"""Pin local source ranges and resolve review evidence against the actual ingestion chunks."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from itertools import accumulate
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field

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
    path: Annotated[str, Field(pattern=r"^docs/[a-zA-Z0-9_./-]+\.md$")]
    sha256: Digest
    sections: list[tuple[int, int]] = Field(min_length=1, max_length=100)


class Manifest(Record):
    dataset: Identifier
    revision: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    documents: list[Source] = Field(min_length=1, max_length=8)


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
    version = sha256(LAYOUT + "\n" + manifest.model_dump_json())
    corpus = Corpus(manifest.dataset, version, {}, {}, {})
    root = root.resolve(strict=True)
    if len({doc.path for doc in manifest.documents}) != len(manifest.documents):
        raise ValueError("duplicate source path")
    for source in manifest.documents:
        path = (root / source.path).resolve(strict=True)
        if not path.is_relative_to(root / "docs") or source.id in corpus.documents:
            raise ValueError(f"source must be unique and inside docs/: {source.id}")
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
                                                   parent_start=len(groups)):
                groups.append((parent, children))
                corpus.parents[parent.chunk_id] = parent
                for index, child in enumerate(children):
                    corpus.children[child.chunk_id] = (child, start + index * CHILD_STRIDE)
                start += len(parent.text)
        corpus.documents[source.id] = Document(doc_id, ref, selected.hexdigest(), lines, offsets, groups)
    return corpus


def review_cases(corpus: Corpus, review: Review, *, freeze: bool) -> list[Case]:
    if review.dataset != corpus.dataset or review.corpus_sha256 != corpus.version:
        raise ValueError("review belongs to another dataset or source snapshot")
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
        cases.append(Case(id=case.id, question=case.question, relevant=list(dict.fromkeys(e.parent for e in case.evidence))))
    if freeze and not cases:
        raise ValueError("freeze requires at least one approved case")
    return cases


def freeze_review(corpus: Corpus, review: Review) -> Judgments:
    cases = review_cases(corpus, review, freeze=True)
    return Judgments(dataset=corpus.dataset, corpus_sha256=corpus.version, granularity="parent",
                     reviewer=review.reviewer.strip(), cases=cases)

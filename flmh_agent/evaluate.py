"""Score saved retrieval rankings locally; no model, network or database is used."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


Identifier = Annotated[str, Field(min_length=1)]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Granularity = Literal["document", "parent"]


class Case(Record):
    id: Identifier
    question: Identifier
    relevant: list[Identifier] = Field(min_length=1)
    equivalent_groups: list[list[Identifier]] = Field(default_factory=list, exclude_if=lambda groups: not groups)

    @model_validator(mode="after")
    def validate_groups(self):
        members = [doc for group in self.equivalent_groups for doc in group]
        if (any(len(group) < 2 for group in self.equivalent_groups) or len(set(members)) != len(members)
                or not set(members) <= set(self.relevant)):
            raise ValueError("equivalent groups must be disjoint groups of labeled parents")
        return self

    def required_groups(self):
        assigned = {doc for group in self.equivalent_groups for doc in group}
        return [set(group) for group in self.equivalent_groups] + [{doc} for doc in self.relevant if doc not in assigned]


class Judgments(Record):
    dataset: Identifier
    cases: list[Case] = Field(min_length=1)
    granularity: Granularity = "document"
    corpus_sha256: Digest | None = None
    reviewer: str = ""

    def fingerprint(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()

    @model_validator(mode="after")
    def unique_labels(self):
        if len({c.id for c in self.cases}) != len(self.cases):
            raise ValueError("duplicate case id")
        if any(len(set(c.relevant)) != len(c.relevant) for c in self.cases):
            raise ValueError("duplicate relevant id")
        return self


class Recording(Record):
    judgments_sha256: Digest
    corpus_sha256: Digest
    pit_sha256: Digest
    recorded_at_utc: Identifier
    indices: list[Identifier] = Field(min_length=1)
    embedding_model: Identifier
    embedding_dimension: int = Field(ge=1)
    reranker_model: Identifier
    ranking_depth: int = Field(ge=1, le=100)
    candidate_multiplier: int = Field(ge=1, le=10)


class Rankings(Record):
    name: Identifier
    dataset: Identifier
    source: Literal["SCRIPTED_FIXTURE", "RECORDED_RETRIEVAL"]
    rankings: dict[Identifier, list[Identifier]]
    granularity: Granularity = "document"
    recording: Recording | None = None


def score(gold: Judgments, run: Rankings, k: int = 5) -> dict:
    if k < 1 or run.dataset != gold.dataset or set(run.rankings) != {c.id for c in gold.cases}:
        raise ValueError("positive k, matching dataset and exactly one ranking per question required")
    if run.granularity != gold.granularity:
        raise ValueError("ranking and judgment granularity differ")
    if gold.corpus_sha256 is not None or run.recording is not None:
        meta = run.recording
        if (meta is None or run.source != "RECORDED_RETRIEVAL" or meta.corpus_sha256 != gold.corpus_sha256
                or meta.judgments_sha256 != gold.fingerprint() or k > meta.ranking_depth):
            raise ValueError("recording does not match frozen labels, corpus or requested depth")
        if any(len(ids) > meta.ranking_depth for ids in run.rankings.values()):
            raise ValueError("ranking exceeds recorded depth")
    recalls, reciprocal_ranks, cutoff_ranks, strict_hits = [], [], [], []
    depth = run.recording.ranking_depth if run.recording else max(map(len, run.rankings.values()), default=0)
    for case in gold.cases:
        ranked = run.rankings[case.id]
        if len(set(ranked)) != len(ranked):
            raise ValueError(f"duplicate result in {case.id}")
        relevant = set(case.relevant)
        groups = case.required_groups()
        hits = sum(not group.isdisjoint(ranked[:k]) for group in groups)
        recalls.append(hits / len(groups))
        strict_hits.append(hits == len(groups))
        first = next((i for i, doc in enumerate(ranked, 1) if doc in relevant), None)
        reciprocal_ranks.append(1 / first if first else 0.0)
        cutoff_ranks.append(1 / first if first and first <= k else 0.0)
    result = {"dataset": gold.dataset, "run": run.name, "source": run.source, "queries": len(gold.cases),
              f"recall@{k}": sum(recalls) / len(recalls), "mrr": sum(reciprocal_ranks) / len(reciprocal_ranks)}
    result.update(mrr_depth=depth, mrr_k=k, mrr_at_k=sum(cutoff_ranks) / len(cutoff_ranks),
                  all_required_at_k=sum(strict_hits) / len(strict_hits))
    if run.recording:
        result.update(granularity=gold.granularity, ranking_depth=run.recording.ranking_depth)
    return result


def paired_report(gold: Judgments, runs: list[Rankings], k: int) -> dict:
    by_name = {run.name: run for run in runs}
    reranked = by_name.get("dense-rerank")
    names = ("dense-only", "dense-rerank", "hybrid") if reranked else ("dense-only", "hybrid")
    if len(runs) != len(names) or set(by_name) != set(names):
        raise ValueError("case comparison requires one dense-only, one hybrid and optionally one dense-rerank recording")
    dense, hybrid = by_name["dense-only"], by_name["hybrid"]
    if (gold.granularity != "parent" or dense.recording is None
            or any(run.recording != dense.recording for run in runs)):
        raise ValueError("case comparison requires parent rankings from the same recording and PIT")
    scores = [score(gold, by_name[name], k) for name in names]
    groups: dict[str, list[dict]] = {"recovered": [], "still_missed": [], "regressed": []}
    if reranked:
        groups["sparse_recovered"] = []
        groups["sparse_regressed"] = []
    for case in gold.cases:
        baseline, combined = dense.rankings[case.id][:k], hybrid.rankings[case.id][:k]
        evidence = case.required_groups()
        dense_hit = all(not group.isdisjoint(baseline) for group in evidence)
        hybrid_hit = all(not group.isdisjoint(combined) for group in evidence)
        row = {"id": case.id, "question": case.question, "relevant": case.relevant,
               "dense_top_k": baseline, "hybrid_top_k": combined}
        rerank_hit = False
        if reranked:
            control = reranked.rankings[case.id][:k]
            row["dense_rerank_top_k"] = control
            rerank_hit = all(not group.isdisjoint(control) for group in evidence)
            if hybrid_hit and not rerank_hit:
                groups["sparse_recovered"].append(row)
            elif rerank_hit and not hybrid_hit:
                groups["sparse_regressed"].append(row)
        if hybrid_hit and not dense_hit:
            groups["recovered"].append(row)
        elif dense_hit and not hybrid_hit:
            groups["regressed"].append(row)
        elif not (dense_hit or hybrid_hit or rerank_hit):
            groups["still_missed"].append(row)
    return {"k": k, "granularity": "parent", "recording": dense.recording.model_dump(),
            "scores": scores, **groups}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--judgments", required=True, type=Path)
    parser.add_argument("--run", required=True, type=Path, action="append", help="rankings from one retriever; repeat for a baseline")
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--compare-cases", action="store_true", help="compare dense-only and hybrid, optionally with a dense-rerank control")
    parser.add_argument("--output", type=Path, help="save to a new JSON file instead of stdout")
    args = parser.parse_args()
    try:
        gold = Judgments.model_validate_json(args.judgments.read_bytes())
        runs = [Rankings.model_validate_json(path.read_bytes()) for path in args.run]
        if args.compare_cases:
            report = paired_report(gold, runs, args.k)
        else:
            if runs[0].recording is not None and any(run.recording != runs[0].recording for run in runs):
                raise ValueError("cannot compare runs from different recordings")
            report = [score(gold, run, args.k) for run in runs]
        text = json.dumps(report, ensure_ascii=False, indent=2)
        if args.output:
            with args.output.open("x", encoding="utf-8") as output:
                print(text, file=output)
        else:
            print(text)
    except (OSError, ValidationError, ValueError) as error:
        parser.exit(2, f"evaluation failed: {error}\n")


if __name__ == "__main__":
    main()

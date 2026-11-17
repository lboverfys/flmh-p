"""Score saved retrieval rankings locally; no model, network or database is used."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


Identifier = Annotated[str, Field(min_length=1)]


class Case(Record):
    id: Identifier
    question: Identifier
    relevant: list[Identifier] = Field(min_length=1)


class Judgments(Record):
    dataset: Identifier
    cases: list[Case] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_labels(self):
        if len({c.id for c in self.cases}) != len(self.cases):
            raise ValueError("duplicate case id")
        if any(len(set(c.relevant)) != len(c.relevant) for c in self.cases):
            raise ValueError("duplicate relevant id")
        return self


class Rankings(Record):
    name: Identifier
    dataset: Identifier
    source: Literal["SCRIPTED_FIXTURE", "RECORDED_RETRIEVAL"]
    rankings: dict[Identifier, list[Identifier]]


def score(gold: Judgments, run: Rankings, k: int = 5) -> dict:
    if k < 1 or run.dataset != gold.dataset or set(run.rankings) != {c.id for c in gold.cases}:
        raise ValueError("positive k, matching dataset and exactly one ranking per question required")
    recalls, reciprocal_ranks = [], []
    for case in gold.cases:
        ranked = run.rankings[case.id]
        if len(set(ranked)) != len(ranked):
            raise ValueError(f"duplicate result in {case.id}")
        relevant = set(case.relevant)
        recalls.append(len(relevant.intersection(ranked[:k])) / len(relevant))
        first = next((i for i, doc in enumerate(ranked, 1) if doc in relevant), None)
        reciprocal_ranks.append(1 / first if first else 0.0)
    return {"dataset": gold.dataset, "run": run.name, "source": run.source, "queries": len(gold.cases),
            f"recall@{k}": sum(recalls) / len(recalls), "mrr": sum(reciprocal_ranks) / len(reciprocal_ranks)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--judgments", required=True, type=Path)
    parser.add_argument("--run", required=True, type=Path, action="append", help="rankings from one retriever; repeat for a baseline")
    parser.add_argument("--k", type=int, default=5)
    args = parser.parse_args()
    try:
        gold = Judgments.model_validate_json(args.judgments.read_bytes())
        scores = [score(gold, Rankings.model_validate_json(path.read_bytes()), args.k) for path in args.run]
    except (OSError, ValidationError, ValueError) as error:
        parser.exit(2, f"evaluation failed: {error}\n")
    print(json.dumps(scores, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

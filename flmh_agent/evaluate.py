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


class UnanswerableCase(Record):
    id: Identifier
    question: Identifier
    reason: Identifier


class UnanswerableJudgments(Record):
    dataset: Identifier
    corpus_sha256: Digest
    reviewer: Identifier
    cases: list[UnanswerableCase] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_labels(self):
        if not self.reviewer.strip() or any(not case.question.strip() or not case.reason.strip() for case in self.cases):
            raise ValueError("a named reviewer, question and rationale are required")
        if (len({case.id for case in self.cases}) != len(self.cases)
                or len({case.question.strip() for case in self.cases}) != len(self.cases)):
            raise ValueError("duplicate unanswerable case id or question")
        return self

    def fingerprint(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()


class RefusalRecording(Record):
    corpus_sha256: Digest
    unanswerable_judgments_sha256: Digest
    positive_judgments_sha256: Digest | None = None
    raw_evidence_sha256: Digest
    recorded_at_utc: Identifier


class RefusalRun(Record):
    name: Identifier
    source: Literal["SCRIPTED_FIXTURE", "RECORDED_REFUSAL_EVALUATION"]
    unanswerable_dataset: Identifier
    unanswerable_refused: dict[Identifier, bool] = Field(min_length=1)
    positive_dataset: Identifier | None = None
    positive_refused: dict[Identifier, bool] = Field(default_factory=dict)
    recording: RefusalRecording | None = None

    @model_validator(mode="after")
    def positive_decisions_have_dataset(self):
        if self.positive_dataset is None and self.positive_refused:
            raise ValueError("positive_dataset is required when positive refusal decisions are supplied")
        if self.source == "RECORDED_REFUSAL_EVALUATION" and self.recording is None:
            raise ValueError("recorded refusal runs require a corpus, judgment and raw-evidence fingerprint")
        if self.source == "RECORDED_REFUSAL_EVALUATION" and self.recording is not None:
            has_positive_hash = self.recording.positive_judgments_sha256 is not None
            if has_positive_hash != (self.positive_dataset is not None):
                raise ValueError("positive refusal decisions and their frozen-label fingerprint must be supplied together")
        if self.source == "SCRIPTED_FIXTURE" and self.recording is not None:
            raise ValueError("scripted fixtures cannot carry recorded-evaluation metadata")
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


def score_refusals(
    unanswerable: UnanswerableJudgments,
    run: RefusalRun,
    positive: Judgments | None = None,
) -> dict:
    if run.unanswerable_dataset != unanswerable.dataset:
        raise ValueError("refusal run and unanswerable judgments belong to different datasets")
    if run.recording is not None and (
            run.recording.corpus_sha256 != unanswerable.corpus_sha256
            or run.recording.unanswerable_judgments_sha256 != unanswerable.fingerprint()):
        raise ValueError("refusal recording does not match the corpus or unanswerable judgments")
    if set(run.unanswerable_refused) != {case.id for case in unanswerable.cases}:
        raise ValueError("exactly one refusal decision per unanswerable question is required")

    details = []
    for case in unanswerable.cases:
        refused = run.unanswerable_refused[case.id]
        details.append({"id": case.id, "question": case.question, "reason": case.reason,
                        "expected": "refuse", "refused": refused,
                        "outcome": "CORRECT_REFUSAL" if refused else "FALSE_ANSWER"})
    correct = sum(item["refused"] for item in details)
    total = len(details)
    report = {"dataset": unanswerable.dataset, "run": run.name, "source": run.source, "queries": total,
              "correct_refusals": correct, "false_answers": total - correct,
              "refusal_rate": correct / total, "false_positive_rate": (total - correct) / total,
              "cases": details}

    if positive is None:
        if run.positive_dataset is not None or run.positive_refused:
            raise ValueError("positive judgments are required to score supplied positive decisions")
        if run.recording is not None and run.recording.positive_judgments_sha256 is not None:
            raise ValueError("positive judgments are required to verify the refusal recording")
        if run.recording is not None:
            report["recording"] = run.recording.model_dump()
        return report
    if positive.corpus_sha256 != unanswerable.corpus_sha256:
        raise ValueError("positive and unanswerable judgments belong to different corpus snapshots")
    if run.recording is not None and run.recording.positive_judgments_sha256 != positive.fingerprint():
        raise ValueError("refusal recording does not match the positive judgments")
    if run.positive_dataset != positive.dataset or set(run.positive_refused) != {case.id for case in positive.cases}:
        raise ValueError("exactly one positive refusal decision per positive question is required")

    positive_details = []
    for case in positive.cases:
        refused = run.positive_refused[case.id]
        positive_details.append({"id": case.id, "question": case.question, "expected": "answer",
                                 "refused": refused, "outcome": "FALSE_REFUSAL" if refused else "ANSWERED"})
    false_refusals = sum(item["refused"] for item in positive_details)
    report["positive"] = {"dataset": positive.dataset, "queries": len(positive_details),
                           "false_refusals": false_refusals,
                           "false_refusal_rate": false_refusals / len(positive_details),
                           "cases": positive_details}
    if run.recording is not None:
        report["recording"] = run.recording.model_dump()
    return report


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
    parser.add_argument("--judgments", type=Path, help="positive retrieval judgments")
    parser.add_argument("--run", type=Path, action="append", help="rankings from one retriever; repeat for a baseline")
    parser.add_argument("--unanswerable-judgments", type=Path, help="human-frozen unsupported-question labels")
    parser.add_argument("--refusal-run", type=Path, help="per-question refusal decisions to score")
    parser.add_argument("--positive-judgments", type=Path, help="optional frozen answerable labels for false-refusal rate")
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--compare-cases", action="store_true", help="compare dense-only and hybrid, optionally with a dense-rerank control")
    parser.add_argument("--output", type=Path, help="save to a new JSON file instead of stdout")
    args = parser.parse_args()
    try:
        retrieval_requested = args.judgments is not None or bool(args.run)
        refusal_requested = args.unanswerable_judgments is not None or args.refusal_run is not None or args.positive_judgments is not None
        reports = {}
        if retrieval_requested:
            if args.judgments is None or not args.run:
                parser.error("--judgments and at least one --run are required for retrieval scoring")
            gold = Judgments.model_validate_json(args.judgments.read_bytes())
            runs = [Rankings.model_validate_json(path.read_bytes()) for path in args.run]
            if args.compare_cases:
                reports["retrieval"] = paired_report(gold, runs, args.k)
            else:
                if runs[0].recording is not None and any(run.recording != runs[0].recording for run in runs):
                    raise ValueError("cannot compare runs from different recordings")
                reports["retrieval"] = [score(gold, run, args.k) for run in runs]
        elif args.compare_cases:
            parser.error("--compare-cases requires --judgments and --run")

        if refusal_requested:
            if args.unanswerable_judgments is None or args.refusal_run is None:
                parser.error("--unanswerable-judgments and --refusal-run must be provided together")
            unanswerable = UnanswerableJudgments.model_validate_json(args.unanswerable_judgments.read_bytes())
            refusal_run = RefusalRun.model_validate_json(args.refusal_run.read_bytes())
            positive = Judgments.model_validate_json(args.positive_judgments.read_bytes()) if args.positive_judgments else None
            reports["unanswerable_refusal"] = score_refusals(unanswerable, refusal_run, positive)

        if not reports:
            parser.error("provide retrieval inputs or unanswerable refusal inputs")
        output = reports.get("retrieval") if set(reports) == {"retrieval"} else reports
        text = json.dumps(output, ensure_ascii=False, indent=2)
        if args.output:
            with args.output.open("x", encoding="utf-8") as destination:
                print(text, file=destination)
        else:
            print(text)
    except (OSError, ValidationError, ValueError) as error:
        parser.exit(2, f"evaluation failed: {error}\n")


if __name__ == "__main__":
    main()

from __future__ import annotations

import hashlib
import json
import re
from bisect import bisect_right
from pathlib import Path

from flmh_agent.rag_corpus import Candidate, Evidence, Review, load_corpus, review_cases, sha256

ROOT = Path(__file__).resolve().parents[1]
V3_DIR = ROOT / "examples" / "rag-evaluation" / "operations-v3"
CORPUS_DIR = V3_DIR / "corpus"

BACKTICK_RE = re.compile(r"`([^`]{2,40})`")
METRIC_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:%|天|日|小时|分钟|秒|个|条|次|美元|元|MiB|GB|ms|px|s|h)")
PROHIBITION_CUES = ("禁止", "严禁", "不得", "不可", "红线", "阻断", "拒绝", "封禁", "扣分")
THRESHOLD_CUES = ("不低于", "不超过", "至少", "上限", "封顶", "阈值", "比例", "周期", "时效", "天内")
CONFIG_CUES = ("配置", "开关", "字段", "参数", "接口", "标识", "标签", "模板", "权限")
TROUBLESHOOT_CUES = ("失败", "超时", "限流", "拦截", "申诉", "排查", "冻结", "熔断", "降级", "重试")


def _extract_quote_candidates(line_text: str) -> list[str]:
    results: list[str] = []
    clauses = [c.strip() for c in re.split(r"[。！？；;]", line_text) if c.strip()]
    for clause in clauses:
        subparts = [s.strip() for s in re.split(r"[，,]", clause) if s.strip()]
        for sub in subparts:
            if 8 <= len(sub) <= 56:
                results.append(sub)
        if 10 <= len(clause) <= 68:
            results.append(clause)
    for match in BACKTICK_RE.finditer(line_text):
        span = match.group(0).strip()
        if 4 <= len(span) <= 52:
            results.append(span)

    def _score(q: str) -> tuple[int, int]:
        has_code = 1 if ("`" in q or METRIC_RE.search(q)) else 0
        has_cue = 1 if any(k in q for k in PROHIBITION_CUES + THRESHOLD_CUES + CONFIG_CUES + TROUBLESHOOT_CUES) else 0
        starts_cond = 1 if q.startswith(("如果", "当", "需要注意", "切忌")) else 0
        ideal_len = -abs(len(q) - 26)
        return (has_code * 3 + has_cue * 2 - starts_cond * 2, ideal_len)

    seen: set[str] = set()
    unique: list[str] = []
    for cand in sorted(results, key=_score, reverse=True):
        if cand not in seen:
            seen.add(cand)
            unique.append(cand)
    return unique


def build() -> dict[str, int]:
    md_files = sorted(CORPUS_DIR.glob("*.md"))
    if len(md_files) != 10:
        raise RuntimeError(f"Expected 10 corpus markdown files in {CORPUS_DIR}, found {len(md_files)}")

    sources_docs: list[dict] = []
    corpus_hasher = hashlib.sha256()

    for md_path in md_files:
        normalized_text = md_path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
        doc_sha = sha256(normalized_text)
        corpus_hasher.update(md_path.name.encode("utf-8"))
        corpus_hasher.update(normalized_text.encode("utf-8"))

        rel_path = f"examples/rag-evaluation/operations-v3/corpus/{md_path.name}"
        doc_slug = md_path.stem
        lines = normalized_text.splitlines()

        h3_indices = [i for i, line in enumerate(lines) if line.startswith("### ")]
        section_ranges: list[list[int]] = []
        for sec_idx, h3_i in enumerate(h3_indices):
            next_h3_i = h3_indices[sec_idx + 1] if sec_idx + 1 < len(h3_indices) else len(lines)
            end_i = next_h3_i - 1
            while end_i > h3_i and (not lines[end_i].strip() or lines[end_i].startswith("## ")):
                end_i -= 1
            section_ranges.append([h3_i + 1, end_i + 1])

        sources_docs.append(
            {
                "id": doc_slug,
                "path": rel_path,
                "sha256": doc_sha,
                "sections": section_ranges,
            }
        )

    manifest_payload = {
        "dataset": "flmh-radar-ops-v3",
        "revision": corpus_hasher.hexdigest()[:40],
        "child_size": 200,
        "child_stride": 130,
        "documents": sources_docs,
    }
    sources_path = V3_DIR / "sources.json"
    sources_path.write_text(json.dumps(manifest_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    corpus = load_corpus(ROOT, sources_path)
    if len(corpus.parents) < 300:
        raise RuntimeError(f"Expected >= 300 parents, got {len(corpus.parents)}")
    if len(corpus.children) < 500:
        raise RuntimeError(f"Expected >= 500 children, got {len(corpus.children)}")

    chunks_path = V3_DIR / "chunks.jsonl"
    with chunks_path.open("w", encoding="utf-8", newline="\n") as handle:
        for doc_slug, document in corpus.documents.items():
            for parent, children in document.groups:
                for chunk in (parent, *children):
                    first_child = chunk.chunk_id if chunk.parent_id else children[0].chunk_id
                    start = corpus.children[first_child][1]
                    row = {
                        "document": doc_slug,
                        "document_id": document.document_id,
                        "version": corpus.version,
                        "id": chunk.chunk_id,
                        "parent_id": chunk.parent_id,
                        "source_ref": chunk.source_ref,
                        "lines": [
                            bisect_right(document.offsets, start),
                            bisect_right(document.offsets, start + len(chunk.text) - 1),
                        ],
                        "text": chunk.text,
                    }
                    handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")

    cases: list[Candidate] = []
    questions = json.loads(Path(__file__).with_name("rag-v3-seller-questions.json").read_text(encoding="utf-8"))

    required_case_ids: dict[str, list[str]] = {}
    for doc_meta in sources_docs:
        doc_slug = doc_meta["id"]
        document = corpus.documents[doc_slug]
        sections = doc_meta["sections"]

        groups_with_lines = list(zip(sections, document.groups))
        # Prefer sections 1..30 (30 operational topics), falling back to 0 if needed
        candidate_pool = groups_with_lines[1:] + groups_with_lines[:1] if len(groups_with_lines) >= 31 else groups_with_lines
        doc_cases = 0

        for ([sec_start, sec_end], (parent, children)) in candidate_pool:
            if doc_cases >= 30:
                break

            body_lines = [
                (ln, document.lines[ln - 1].rstrip("\n"))
                for ln in range(sec_start + 1, sec_end + 1)
                if document.lines[ln - 1].strip() and not document.lines[ln - 1].startswith("## ")
            ]
            # Balance child indices across cases
            ordered_children = list(reversed(children)) if doc_cases % 2 == 1 and len(children) > 1 else list(children)

            chosen_evidence: Evidence | None = None

            for child in ordered_children:
                for ln, raw_line in body_lines:
                    for quote in _extract_quote_candidates(raw_line):
                        ev = Evidence(
                            document=doc_slug,
                            line=ln,
                            quote=quote,
                            parent=parent.chunk_id,
                            child=child.chunk_id,
                        )
                        try:
                            corpus.check_evidence(ev)
                        except ValueError:
                            continue
                        if parent.text.count(quote) == 1 and child.text.count(quote) == 1:
                            chosen_evidence = ev
                            break
                    if chosen_evidence is not None:
                        break
                if chosen_evidence is not None:
                    break

            if chosen_evidence is None:
                continue

            case_id = f"{doc_slug[:2]}-sec-{sec_start:03d}"
            item = questions.pop(case_id)
            question = item if isinstance(item, str) else item["question"]
            if isinstance(item, dict):
                required_case_ids[case_id] = item["also_requires"]

            doc_cases += 1
            cases.append(
                Candidate(
                    id=case_id,
                    question=question,
                    status="pending",
                    evidence=[chosen_evidence],
                )
            )

        if doc_cases != 30:
            raise RuntimeError(f"Expected 30 cases for {doc_slug}, built {doc_cases}")

    by_id = {case.id: case for case in cases}
    for case_id, supporting in required_case_ids.items():
        by_id[case_id].evidence.extend(by_id[other].evidence[0] for other in supporting)
    if questions:
        raise ValueError(f"seller questions without verified evidence: {sorted(questions)}")
    review = Review(
        dataset="flmh-radar-ops-v3",
        corpus_sha256=corpus.version,
        reviewer="",
        cases=cases,
    )
    review_cases(corpus, review, freeze=False)

    candidates_path = V3_DIR / "candidates.json"
    candidates_path.write_text(review.model_dump_json(indent=2) + "\n", encoding="utf-8")

    return {
        "documents": len(corpus.documents),
        "parents": len(corpus.parents),
        "children": len(corpus.children),
        "cases": len(cases),
    }


if __name__ == "__main__":
    summary = build()
    print(json.dumps(summary, ensure_ascii=False))

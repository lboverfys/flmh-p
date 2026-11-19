"""通过 FLMH 模型网关手动测量规划策略和多轮记忆策略。"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from flmh_agent.adapters.outbound.model_gateway import ModelGatewayClient, ModelGatewayPlanner
from flmh_agent.application.workflow import _layered_search
from flmh_agent.config import Settings
from flmh_agent.domain.models import ProductQuery
from flmh_agent.domain.slots import update_slots


DATA_DIR = Path(__file__).resolve().parents[1] / "examples" / "agent-evaluation"
ENDPOINT = "/api/v1/models/completions"
PURPOSE = "agent_plan"
MAX_TOKENS = 2048
TOOL_LIMIT = 4  # AgentPlan 的查询上限，两种策略共用。
HISTORY_SIZE = 12
HISTORY_CHARS = 2400
ARMS = {"planning": ("planner", "react"), "memory": ("slots", "history12")}
TOOL_PROTOCOL = (
    "可用只读查询为 product_search 和 product_ranking。均用商品查询 JSON 表示。"
    "目录查询用 region 及可选 keyword、category、max_price。关键词按标题子串匹配，类目按原值匹配。"
    "榜单查询用 region、ranking_limit。region 为两位大写国家代码。每个结果的 products 按工具顺序排列，"
    "商品有 id、title、region、category、price。要求同类时取指定上游结果第一件的 category，不猜类目。"
    "每个市场按工具返回顺序列出全部命中 ID，空结果也保留市场。"
)
ANSWER_FORMAT = '最终答案为 {"answer":{"两位市场代码":["商品ID"]}}，所有 ID 均须来自相应查询。'
REACT_PROMPT = (
    TOOL_PROTOCOL + '每次仅返回一个 JSON 对象。需要观测时给 {"query":{实际查询参数}}，'
    '每步只执行一条，绑定类目时从已返回的观测取值。最多可执行四条查询。' + ANSWER_FORMAT
)
MEMORY_PROMPT = (
    '根据已有上下文和本轮用户消息，仅输出 JSON {"updates":{...},"constraints":{...}}。'
    'updates 只包含本轮用户明确更改的字段，取消限制写 null，没有更改用空对象。'
    'constraints 写出更新后全部四个字段，未给过或已取消的值写 null。'
    'product 为用户指定的商品关键词原文，region 为两位大写国家代码，'
    'product_max_price 为商品单价上限，budget_max 为内容制作预算上限，金额用 JSON 数字。'
    '其他要求不改变这四个条件，沿用未修改的值。'
)


def compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class Record(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


Text = Annotated[str, Field(min_length=1)]
Region = Annotated[str, Field(pattern=r"^[A-Z]{2}$")]
Amount = Annotated[float, Field(ge=0, allow_inf_nan=False)]


class Product(Record):
    id: Text
    title: Text
    region: Region
    category: Text
    price: Amount


class Target(Record):
    query: ProductQuery
    ids: list[Text]
    after_region: Region | None = None


class Task(Record):
    id: Text
    request: Text
    checks: list[Target] = Field(min_length=1, max_length=TOOL_LIMIT)


class PlanningSet(Record):
    catalog: list[Product] = Field(min_length=1)
    tasks: list[Task] = Field(min_length=1)


class Slots(Record):
    product: Text | None = None
    region: Region | None = None
    product_max_price: Amount | None = None
    budget_max: Amount | None = None


class MemoryAnswer(Record):
    updates: Slots
    constraints: Slots


class Turn(Record):
    user: Text
    assistant: Text
    check: Slots | None = None


class Dialogue(Record):
    id: Text
    turns: list[Turn] = Field(min_length=1)


class MemorySet(Record):
    dialogues: list[Dialogue] = Field(min_length=1)


class FinalAnswer(Record):
    answer: dict[Region, list[Text]]


class EvalError(ValueError):
    """本次测量缺少可用于继续执行的模型或工具输出。"""


def usage_fields(usage: Any) -> dict:
    counts = {}
    invalid = False
    for source, key in (("inputTokens", "input_tokens"), ("outputTokens", "output_tokens")):
        value = usage.get(source) if isinstance(usage, dict) else None
        valid = type(value) is int and value >= 0
        invalid |= value is not None and not valid
        counts[key] = value if valid else None
    known = sum(v is not None for v in counts.values())
    status = ("missing", "partial", "complete")[known]
    if invalid or (usage is not None and not isinstance(usage, dict)):
        status = "invalid"
    return {**counts, "usage_status": status}


class MeasuredGateway(ModelGatewayClient):
    """会话的 HTTP client 持有真实用户凭据。每个对照臂单独记录每次出站尝试。"""

    def __init__(self, settings: Settings, client: httpx.AsyncClient, provider: str, model: str):
        super().__init__(settings, client)
        self.provider = provider
        self.model = model
        self.calls: list[dict] = []
        self.phase = "plan"

    async def complete(self, *, purpose: str, messages: Sequence[Mapping[str, str]],
                       temperature: float = 0.0, max_tokens: int = MAX_TOKENS,
                       response_format: str = "json_object") -> dict[str, Any]:
        if (purpose, temperature, max_tokens, response_format) != (PURPOSE, 0.0, MAX_TOKENS, "json_object"):
            raise EvalError("模型调用参数与本轮对照配置不一致")
        payload = {"purpose": purpose, "messages": [dict(m) for m in messages], "temperature": temperature,
                   "maxTokens": max_tokens, "responseFormat": response_format}
        call = {"phase": self.phase, "messages": payload["messages"], "pinned_response": False,
                "status": "error", **usage_fields(None)}
        self.calls.append(call)
        try:
            response = await self._post(ENDPOINT, payload, {})
        except httpx.HTTPStatusError as exc:
            call["error"] = f"HTTP {exc.response.status_code}"
            raise EvalError(call["error"]) from exc
        except (httpx.RequestError, ValueError, TypeError) as exc:
            call["error"] = type(exc).__name__
            raise EvalError(f"模型网关响应失败 ({call['error']})") from exc
        for key in ("provider", "model", "source"):
            value = response.get(key)
            call[key] = value if isinstance(value, str) else None
        call.update(usage_fields(response.get("usage")))
        if (call["source"], call["provider"], call["model"]) != ("OPENAI_COMPATIBLE", self.provider, self.model):
            call["error"] = "真实供应商来源或固定的 provider/model 不匹配"
            raise EvalError(call["error"])
        call["pinned_response"] = True
        content = response.get("content")
        if not isinstance(content, str) or not content.strip():
            call["error"] = "模型响应缺少文本"
            raise EvalError(call["error"])
        call.update(status="received", content=content)
        return response

    async def ask(self, phase: str, instruction: str, payload: dict) -> dict:
        self.phase = phase
        response = await self.complete(purpose=PURPOSE, messages=[
            {"role": "system", "content": instruction}, {"role": "user", "content": compact(payload)}])
        value = json.loads(response["content"])
        if not isinstance(value, dict):
            raise EvalError("模型输出必须是 JSON 对象")
        return value


def tool_request(query: ProductQuery) -> tuple[str, dict]:
    if query.ranking_limit is not None:
        return "product_ranking", {"region": query.region, "limit": query.ranking_limit}
    return "product_search", query.model_dump(exclude_none=True, exclude={"query_id", "depends_on", "category_from_query"})


def lookup(catalog: Sequence[Product], tool: str, args: dict) -> dict:
    matches = [p for p in catalog if p.region == args["region"]]
    if tool == "product_ranking":
        matches = matches[:args["limit"]]
    else:
        matches = [p for p in matches if args.get("keyword", "").casefold() in p.title.casefold()
                   and ("category" not in args or p.category == args["category"])
                   and ("max_price" not in args or p.price <= args["max_price"])][:20]
    return {"source": "LOCAL_EVALUATION_FIXTURE", "products": [p.model_dump() for p in matches]}


class FixtureTools:
    def __init__(self, catalog: list[Product]):
        self.catalog = catalog
        self.batches: list[list[dict]] = []
        self.active = 0

    async def search_products(self, query: Mapping[str, Any]) -> dict:
        tool, args = tool_request(ProductQuery.model_validate(query))
        if not self.active:
            self.batches.append([])
        event = {"tool": tool, "arguments": args}
        self.batches[-1].append(event)
        self.active += 1
        try:
            # 工具替身让出一次调度，让同一 gather 内的实际调用全部进入后再返回。
            await asyncio.sleep(0)
            event["result"] = lookup(self.catalog, tool, args)
            return event["result"]
        finally:
            self.active -= 1

    async def execute(self, queries: list[ProductQuery]) -> list[dict]:
        results, _, _, failures = await _layered_search([q.model_dump() for q in queries], self, {})
        if failures:
            raise EvalError(f"查询未完成 {compact(failures)}")
        return results

    def observations(self) -> list[dict]:
        return [event for batch in self.batches for event in batch]


def plan_bindings(queries: list[ProductQuery], results: list[dict]) -> list[tuple]:
    # 成功执行后 results 与计划顺序相同。用实际首件类目还原每一条绑定边的两端。
    names = [q.query_id or f"query-{i}" for i, q in enumerate(queries, 1)]
    by_id = dict(zip(names, results, strict=True))
    requests = {}
    for name, query in zip(names, queries):
        tool, args = tool_request(query)
        if query.category_from_query:
            args["category"] = by_id[query.category_from_query]["products"][0]["category"]
        requests[name] = (tool, args)
    return [(requests[q.category_from_query], requests[name]) for name, q in zip(names, queries) if q.category_from_query]


def score_task(task: Task, answer: dict, tools: FixtureTools, bindings: list[tuple] | None) -> dict:
    # 独立查询无顺序要求。跨区同类需要更早的真实观测，Planner 还须实际绑定到所要求的上游。
    requests = {t.query.region: tool_request(t.query) for t in task.checks}
    observed: dict[str, list[tuple[int, list[dict]]]] = {region: [] for region in requests}
    for target in task.checks:
        region = target.query.region
        for number, batch in enumerate(tools.batches):
            for event in batch:
                if (event["tool"], event["arguments"]) == requests[region]:
                    products = event["result"]["products"]
                    if [p["id"] for p in products] == target.ids:
                        observed[region].append((number, products))
    evidence = {}
    for target in task.checks:
        region = target.query.region
        source = target.after_region
        if source is None:
            evidence[region] = bool(observed[region])
            continue
        edge = (requests[source], requests[region])
        evidence[region] = (bindings is None or edge in bindings) and any(
            earlier < later and products and products[0]["category"] == target.query.category
            for earlier, products in observed[source] for later, _ in observed[region])
    expected = {t.query.region: t.ids for t in task.checks}
    return {"correct": answer == expected and all(evidence.values()), "answer": answer,
            "expected": expected, "query_evidence": evidence}


async def run_planning(task: Task, arm: str, gateway: MeasuredGateway, tools: FixtureTools) -> dict:
    bindings = None
    if arm == "planner":
        gateway.phase = "plan"
        plan = await ModelGatewayPlanner(gateway).plan(task.request + "\n\n" + TOOL_PROTOCOL, {})
        if not plan.queries or plan.create_content or plan.knowledge_query or plan.analysis_question:
            raise EvalError("任务仅支持商品目录和榜单查询")
        results = await tools.execute(plan.queries)
        bindings = plan_bindings(plan.queries, results)
        raw = await gateway.ask("final", TOOL_PROTOCOL + ANSWER_FORMAT,
                                {"request": task.request, "observations": tools.observations()})
    else:
        for step in range(TOOL_LIMIT + 1):
            raw = await gateway.ask("react_decision", REACT_PROMPT,
                                    {"request": task.request, "observations": tools.observations()})
            if set(raw) != {"query"}:
                break
            if step == TOOL_LIMIT:
                raise EvalError("ReAct 超过工具调用上限")
            query = ProductQuery.model_validate(raw["query"])
            if query.query_id or query.depends_on or query.category_from_query:
                raise EvalError("ReAct 单步查询须提交已解析的工具参数")
            await tools.execute([query])
    return score_task(task, FinalAnswer.model_validate(raw).answer, tools, bindings)


def history_window(history: list[str], unit: str, char_limit: int) -> tuple[str, dict]:
    # TS 先写入当前 user 再 take:12。当前消息在两个策略中都单独完整传入。
    keep = HISTORY_SIZE - 1 if unit == "message" else HISTORY_SIZE * 2
    selected = history[-keep:]
    transcript = "\n".join(selected)
    return transcript[-char_limit:], {"dropped_messages": len(history) - len(selected),
                                      "dropped_chars": max(0, len(transcript) - char_limit)}


def error_text(exc: ValueError) -> str:
    if isinstance(exc, ValidationError):
        return compact(exc.errors(include_url=False, include_input=False, include_context=False))
    return str(exc)


def retention(matched: int, evaluated: int, expected: int) -> dict:
    observed = matched / evaluated if evaluated else None
    return {"matched": matched, "evaluated": evaluated, "expected": expected, "unscored": expected - evaluated,
            "observed_rate": observed, "retention_rate": observed if expected == evaluated else None}


async def run_memory(dialogue: Dialogue, arm: str, gateway: MeasuredGateway, unit: str, char_limit: int) -> dict:
    history: list[str] = []
    slots: dict = {}
    versions: dict[str, int] = {}
    turns = []
    failure = None
    evaluated = matched = expected = 0
    for index, turn in enumerate(dialogue.turns, 1):
        check = turn.check.model_dump(exclude_unset=True) if turn.check is not None else {}
        expected += len(check)
        row = {"turn": index, "expected": check}
        turns.append(row)
        if failure is not None:
            row["status"] = "skipped_after_error"
            continue
        if arm == "slots":
            context = {"slots": slots, "versions": versions}
        else:
            transcript, row["window"] = history_window(history, unit, char_limit)
            context = {"history": transcript}
        try:
            raw = await gateway.ask(f"turn_{index}", MEMORY_PROMPT, {"context": context, "user": turn.user})
            answer = MemoryAnswer.model_validate(raw)
            changes = answer.updates.model_dump(exclude_unset=True)
            if arm == "slots":
                normalized, _ = update_slots(slots, changes)
                # 只写显式 patch。未提及的默认市场和被撤销的限制都不能当作用户条件。
                for key, value in changes.items():
                    new_value = normalized[key] if value is not None else None
                    if key not in slots or slots[key] != new_value:
                        slots[key] = new_value
                        versions[key] = versions.get(key, 0) + 1
        except ValueError as exc:
            failure = error_text(exc)
            row.update(status="failed", error=failure)
            continue
        effective = answer.constraints.model_dump(exclude_unset=True)
        hits = [key for key, value in check.items() if key in effective and effective[key] == value]
        row.update(status="completed", constraints=effective, updates=changes, matched_fields=hits)
        matched += len(hits)
        evaluated += len(check)
        if arm == "history12":
            history.extend(("用户：" + turn.user, "助手：" + turn.assistant))
    result = {"turns": turns, "constraints": retention(matched, evaluated, expected)}
    if failure is not None:
        result["error"] = failure
    return result


def metrics(calls: list[dict], batches: list[list[dict]]) -> dict:
    pinned = [c for c in calls if c["pinned_response"]]
    complete = sum(c["usage_status"] == "complete" for c in pinned)
    tokens = {}
    for field in ("input_tokens", "output_tokens"):
        known = [c[field] for c in pinned if c[field] is not None]
        tokens[field] = sum(known) if known and len(known) == len(calls) else None
    tokens["total_tokens"] = (tokens["input_tokens"] + tokens["output_tokens"]
                              if calls and complete == len(calls) else None)
    if not calls:
        status = "not_called"
    elif complete == len(calls):
        status = "complete"
    elif any(c["usage_status"] == "invalid" for c in pinned):
        status = "invalid"
    elif any(c["input_tokens"] is not None or c["output_tokens"] is not None for c in pinned):
        status = "partial"
    else:
        status = "missing"
    return {"model_calls": len(calls), "pinned_model_responses": len(pinned), "usage_complete_calls": complete,
            "usage_status": status, **tokens, "tool_calls": sum(map(len, batches)), "tool_batches": len(batches),
            "parallel_batches": sum(len(batch) > 1 for batch in batches)}


async def measure(case: Task | Dialogue, arm: str, gateway: MeasuredGateway, catalog: list[Product],
                  unit: str, char_limit: int) -> dict:
    tools = FixtureTools(catalog)
    if isinstance(case, Task):
        suite = "planning"
        try:
            outcome = await run_planning(case, arm, gateway, tools)
        except ValueError as exc:
            outcome = {"error": error_text(exc), "correct": None}
    else:
        suite = "memory"
        outcome = await run_memory(case, arm, gateway, unit, char_limit)
    return {"kind": "case", "suite": suite, "case_id": case.id, "arm": arm,
            "status": "failed" if "error" in outcome else "completed", **outcome,
            "metrics": metrics(gateway.calls, tools.batches), "model_trace": gateway.calls,
            "tool_trace": tools.batches}


def sum_retention(runs: list[dict]) -> dict:
    totals = {field: sum(run["constraints"][field] for run in runs) for field in ("matched", "evaluated", "expected")}
    return retention(**totals)


def summarize(suite: str, runs: list[dict]) -> dict:
    arms = ARMS[suite]
    candidate, baseline = arms
    pairs: dict[str, dict] = {}
    for run in runs:
        pairs.setdefault(run["case_id"], {})[run["arm"]] = run
    paired = [key for key, pair in pairs.items() if all(
        pair[arm]["status"] == "completed" and (suite == "memory" or pair[arm]["correct"]) for arm in arms)]
    with_usage = [key for key in paired if all(pairs[key][arm]["metrics"]["usage_status"] == "complete" for arm in arms)]
    by_arm = {}
    for arm in arms:
        records = [pair[arm] for pair in pairs.values()]
        averages = {field: sum(pairs[k][arm]["metrics"][field] for k in paired) / len(paired) if paired else None
                    for field in ("model_calls", "tool_calls", "tool_batches", "parallel_batches")}
        token_sums = {field: sum(pairs[k][arm]["metrics"][field] for k in with_usage) if with_usage else None
                      for field in ("input_tokens", "output_tokens", "total_tokens")}
        result = {"failed_ids": [r["case_id"] for r in records if r["status"] == "failed"],
                  "incomplete_usage_ids": [r["case_id"] for r in records if r["metrics"]["usage_status"] != "complete"],
                  "all_attempted_model_calls": sum(r["metrics"]["model_calls"] for r in records),
                  "paired_means": averages, "paired_token_sums": token_sums}
        if suite == "planning":
            result["wrong_answer_ids"] = [r["case_id"] for r in records if r["correct"] is False]
        else:
            result["all_constraints"] = sum_retention(records)
            result["paired_constraints"] = sum_retention([pairs[k][arm] for k in paired])
        by_arm[arm] = result
    reductions = {}
    for field, reference in by_arm[baseline]["paired_token_sums"].items():
        reductions[field] = 1 - by_arm[candidate]["paired_token_sums"][field] / reference if reference else None
    return {"cases": len(pairs), "paired_ids": paired, "usage_paired_ids": with_usage,
            "arms": by_arm, "paired_token_reduction": reductions}


def load_cases(directory: Path, suite: str, selected: set[str]) -> tuple[dict, list[Product], dict]:
    cases = {}
    hashes = {}
    catalog = []
    available: set[str] = set()
    for group in (ARMS if suite == "all" else (suite,)):
        data = (directory / f"{group}.json").read_bytes()
        hashes[group] = hashlib.sha256(data).hexdigest()
        if group == "planning":
            dataset = PlanningSet.model_validate_json(data)
            items = dataset.tasks
            catalog = dataset.catalog
            if len({product.id for product in catalog}) != len(catalog):
                raise ValueError("商品样本 ID 重复")
            for task in items:
                regions = {t.query.region for t in task.checks}
                if len(regions) != len(task.checks):
                    raise ValueError(f"{task.id} 同一市场只能有一个评分目标")
                for target in task.checks:
                    if target.after_region and (target.after_region not in regions or target.after_region == target.query.region):
                        raise ValueError(f"{task.id} 的依赖市场无效")
                    result = lookup(catalog, *tool_request(target.query))
                    if [p["id"] for p in result["products"]] != target.ids:
                        raise ValueError(f"{task.id} 的标签与固定工具数据不符")
        else:
            items = MemorySet.model_validate_json(data).dialogues
            for dialogue in items:
                if not any(turn.check and turn.check.model_fields_set for turn in dialogue.turns):
                    raise ValueError(f"{dialogue.id} 缺少评分点")
        for item in items:
            if item.id in available:
                raise ValueError(f"重复样本 ID {item.id}")
            available.add(item.id)
        chosen = [item for item in items if not selected or item.id in selected]
        if chosen:
            cases[group] = chosen
    if selected - available:
        raise ValueError(f"未找到样本 {sorted(selected - available)}")
    return cases, catalog, hashes


async def evaluate(args: argparse.Namespace) -> int:
    cases, catalog, hashes = load_cases(args.data_dir, args.suite, set(args.case))
    token = os.environ.get("AGENT_EVAL_BEARER_TOKEN", "").strip()
    if not token:
        raise ValueError("请提供当前用户的 AGENT_EVAL_BEARER_TOKEN（只填 token 值）")
    keys = ("MODEL_GATEWAY_URL", "MODEL_GATEWAY_API_KEY", "REQUEST_TIMEOUT_SECONDS")
    settings = Settings.from_env({k: os.environ[k] for k in keys if k in os.environ})
    headers = {"Authorization": "Bearer " + token, "X-Client-Id": "flmh-agent"}
    if settings.model_gateway_api_key:
        headers["X-Service-Key"] = settings.model_gateway_api_key
    async with httpx.AsyncClient(base_url=settings.model_gateway_url, headers=headers, trust_env=False,
                                 timeout=settings.request_timeout_seconds) as client:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as output:
            def emit(record: dict) -> None:
                output.write(compact(record) + "\n")
                output.flush()

            emit({"kind": "metadata", "format_version": 1, "started_utc": datetime.now(timezone.utc).isoformat(),
                  "endpoint": ENDPOINT, "expected_source": "OPENAI_COMPATIBLE", "provider": args.provider,
                  "model": args.model, "purpose": PURPOSE, "temperature": 0.0, "max_tokens": MAX_TOKENS,
                  "response_format": "json_object", "tool_source": "LOCAL_EVALUATION_FIXTURE", "tool_limit": TOOL_LIMIT,
                  "history_size": HISTORY_SIZE, "history_unit": args.history_unit, "history_chars": args.history_chars,
                  "dataset_sha256": hashes, "cases": {s: [c.id for c in items] for s, items in cases.items()}})
            summaries = {}
            incomplete = False
            for suite, items in cases.items():
                records = []
                for index, case in enumerate(items):
                    arms = ARMS[suite] if index % 2 == 0 else reversed(ARMS[suite])
                    for arm in arms:
                        gateway = MeasuredGateway(settings, client, args.provider, args.model)
                        record = await measure(case, arm, gateway, catalog, args.history_unit, args.history_chars)
                        emit(record)
                        incomplete |= record["status"] == "failed" or record["metrics"]["usage_status"] != "complete"
                        # 落盘后，汇总只保存计数和评分，不随样本数累积原始模型上下文。
                        records.append({k: record[k] for k in ("case_id", "arm", "status", "metrics", "correct", "constraints") if k in record})
                summaries[suite] = summarize(suite, records)
            emit({"kind": "summary", "suites": summaries})
    print(f"结果已写入 {args.output.resolve()}")
    return int(incomplete)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", required=True, help="网关配置的真实供应商 ID")
    parser.add_argument("--model", required=True, help="网关配置的模型标识，两侧必须相同")
    parser.add_argument("--output", type=Path, required=True, help="新建的 UTF-8 JSONL 文件")
    parser.add_argument("--suite", choices=("all", *ARMS), default="all")
    parser.add_argument("--case", action="append", default=[], help="只跑指定样本，可重复")
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--history-unit", choices=("message", "turn"), default="message")
    parser.add_argument("--history-chars", type=int, default=HISTORY_CHARS, help="保留最近历史的字符数")
    args = parser.parse_args()
    if args.history_chars < 1:
        parser.error("--history-chars 必须为正整数")
    try:
        return asyncio.run(evaluate(args))
    except (ValueError, OSError, httpx.InvalidURL) as exc:
        print(f"评测未完成：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

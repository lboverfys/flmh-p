from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from flmh_agent.domain.idempotency import idempotency_key
from flmh_agent.domain.content import build_content_request
from flmh_agent.domain.models import AgentPlan, AgentState, ContentBrief
from flmh_agent.domain.slots import INVALIDATION_ORDER, normalize_slots, update_slots
from flmh_agent.ports.workflow import BiQueryPort, PlannerPort, ProductContentPort


def build_workflow(
    *,
    planner: PlannerPort,
    tools: ProductContentPort,
    bi_query: BiQueryPort | None = None,
    bi_principal: str = "",
    checkpointer: Any | None = None,
):
    graph = StateGraph(AgentState)

    async def plan_request(state: AgentState) -> dict[str, Any]:
        slots, invalidated = update_slots(state.get("slot_values"), state.get("slot_updates"))
        slot_snapshot = hashlib.sha256(
            json.dumps(slots, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        slots_changed = normalize_slots(state.get("slot_values")) != slots
        input_snapshot = hashlib.sha256(
            json.dumps(
                {"request": state["user_request"], "slots": slots},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if state.get("input_snapshot") and state["input_snapshot"] != input_snapshot:
            invalidated = list(INVALIDATION_ORDER)
        plan = await planner.plan(state["user_request"], slots)
        _validate_plan_content(plan)
        return {
            "slot_values": slots,
            "invalidated": invalidated,
            "slot_version": int(state.get("slot_version", 0)) + int(slots_changed),
            "slot_snapshot": slot_snapshot,
            "input_snapshot": input_snapshot,
            "plan": plan.model_dump(mode="json"),
            "product_results": [],
            "selected_product": {},
            "content_brief": {},
            "quote": {},
            "budget_ok": False,
            "confirmation": {},
            "confirmation_request": {},
            "confirmed_quote": {},
            "content_task": {},
            "task_status": {},
            "idempotency_key": "",
            "error": "",
            "summary": "",
            "bi_result": {},
        }

    async def run_bi_query(state: AgentState) -> dict[str, Any]:
        if bi_query is None or not bi_principal:
            raise RuntimeError("BI query adapter or authorized service principal is not configured")
        result = await bi_query.query(state["plan"]["analysis_question"], bi_principal)
        return {"bi_result": dict(result)}

    async def search_products(state: AgentState) -> dict[str, Any]:
        queries = state["plan"]["queries"]
        results = await _bounded_parallel_search(queries, tools)
        return {"product_results": results}

    def select_product(state: AgentState) -> dict[str, Any]:
        product_slot = state["slot_values"].get("product")
        products = [product for result in state["product_results"] for product in _extract_products(result)]
        if product_slot:
            selected = next(
                (product for product in products if product.get("id") == product_slot or product.get("title") == product_slot),
                None,
            )
        else:
            selected = products[0] if products else None
        if selected is None:
            reason = "未找到指定商品" if product_slot else "未找到符合条件的商品"
            return {"error": f"{reason}；未创建内容任务。"}
        if not selected.get("id"):
            return {"error": "商品服务返回记录缺少 ID；未创建内容任务。"}
        return {"selected_product": selected}

    def prepare_content_request(state: AgentState) -> dict[str, Any]:
        brief = build_content_request(
            state["selected_product"], state["user_request"], state["plan"]["content_type"]
        )
        return {"content_brief": brief.model_dump(mode="json")}

    async def create_quote(state: AgentState) -> dict[str, Any]:
        product = state["selected_product"]
        query = state["plan"]["queries"][0]
        key = idempotency_key(
            user_request=state["user_request"],
            product_id=str(product["id"]),
            content_type=state["plan"]["content_type"],
            budget_max=state["plan"]["budget_max"],
            region=str(product.get("region") or query["region"]),
        )
        quote = await tools.create_quote({
            "product_id": str(product["id"]),
            "content_type": state["plan"]["content_type"],
            "brief": state["content_brief"]["body"],
            "budget_max": state["plan"]["budget_max"],
        })
        amount = _quote_amount(quote)
        budget = state["plan"]["budget_max"]
        within_budget = budget is None or amount <= Decimal(str(budget))
        prompt = {
            "kind": "budget_confirmation",
            "quoteId": _required_identifier(quote, "quoteId", "id", "quote_id"),
            "quote": dict(quote),
            "product": state["selected_product"],
            "contentBrief": state["content_brief"],
            "message": "请确认报价后再创建内容任务。",
            "planSource": state["plan"].get("source"),
        }
        return {
            "quote": dict(quote), "budget_ok": within_budget, "idempotency_key": key,
            "confirmation_request": prompt if within_budget else {},
        }

    def request_confirmation(state: AgentState) -> dict[str, Any]:
        prompt = state["confirmation_request"]
        decision = interrupt(prompt)
        confirmed = isinstance(decision, dict) and decision.get("confirmed") is True
        return {"confirmation": {"confirmed": confirmed}, "confirmation_request": prompt}

    async def confirm_quote(state: AgentState) -> dict[str, Any]:
        quote_id = _required_identifier(state["quote"], "quoteId", "id", "quote_id")
        response = await tools.confirm_quote(quote_id)
        if response.get("confirmed") is not True:
            raise RuntimeError("content service did not confirm the quote")
        return {"confirmed_quote": dict(response)}

    async def submit_content_task(state: AgentState) -> dict[str, Any]:
        quote_id = _required_identifier(state["quote"], "quoteId", "id", "quote_id")
        response = await tools.submit_task({
            "quote_id": quote_id,
            "product_id": str(state["selected_product"]["id"]),
            "content_type": state["plan"]["content_type"],
            "brief": state["content_brief"]["body"],
        }, state["idempotency_key"])
        return {"content_task": dict(response)}

    async def read_task_status(state: AgentState) -> dict[str, Any]:
        task_id = _required_identifier(state["content_task"], "taskId", "id", "task_id")
        return {"task_status": dict(await tools.get_task(task_id))}

    def summarize(state: AgentState) -> dict[str, Any]:
        if state.get("error"):
            return {"summary": state["error"]}
        plan = state["plan"]
        if plan.get("analysis_question"):
            result = state.get("bi_result") or {}
            rows = result.get("rows", [])
            return {"summary": f"BI 查询返回 {len(rows)} 行，只读 SQL；结果来源：{result.get('source', 'POSTGRES_READ_ONLY')}。"}
        products = [product for result in state.get("product_results", []) for product in _extract_products(result)]
        parts = [
            f"规划来源：{plan['source']}。",
            f"商品查询返回 {len(products)} 条结果。",
        ]
        if plan["create_content"]:
            if not state.get("budget_ok"):
                parts.append("报价超出预算，未请求确认且未创建任务。")
            elif state.get("content_task"):
                parts.append(f"内容任务：{state['content_task']}；状态：{state.get('task_status', {})}。")
                parts.append(f"内容来源：{state['content_brief']['source']}。")
            elif state.get("confirmation", {}).get("confirmed") is True:
                parts.append("内容服务已确认报价，但任务提交未返回结果。")
            else:
                parts.append("等待用户确认报价；尚未创建任务。")
        return {"summary": "\n".join(parts)}

    def after_selection(state: AgentState) -> str:
        if state.get("error"):
            return "summary"
        return "prepare_content_request" if state["plan"]["create_content"] else "summary"

    def after_generation(state: AgentState) -> str:
        return "create_quote" if state["content_brief"] else "summary"

    def after_quote(state: AgentState) -> str:
        return "request_confirmation" if state.get("budget_ok") else "summary"

    def after_confirmation(state: AgentState) -> str:
        return "confirm_quote" if state.get("confirmation", {}).get("confirmed") is True else "summary"

    graph.add_node("plan_request", plan_request)
    graph.add_node("run_bi_query", run_bi_query)
    graph.add_node("search_products", search_products)
    graph.add_node("select_product", select_product)
    graph.add_node("prepare_content_request", prepare_content_request)
    graph.add_node("create_quote", create_quote)
    graph.add_node("request_confirmation", request_confirmation)
    graph.add_node("confirm_quote", confirm_quote)
    graph.add_node("submit_content_task", submit_content_task)
    graph.add_node("read_task_status", read_task_status)
    graph.add_node("summarize", summarize)
    graph.add_edge(START, "plan_request")
    graph.add_conditional_edges("plan_request", lambda state: "bi" if state["plan"].get("analysis_question") else "products", {
        "bi": "run_bi_query", "products": "search_products",
    })
    graph.add_edge("run_bi_query", "summarize")
    graph.add_edge("search_products", "select_product")
    graph.add_conditional_edges("select_product", after_selection, {
        "prepare_content_request": "prepare_content_request", "summary": "summarize",
    })
    graph.add_conditional_edges("prepare_content_request", after_generation, {
        "create_quote": "create_quote", "summary": "summarize",
    })
    graph.add_conditional_edges("create_quote", after_quote, {
        "request_confirmation": "request_confirmation", "summary": "summarize",
    })
    graph.add_conditional_edges("request_confirmation", after_confirmation, {
        "confirm_quote": "confirm_quote", "summary": "summarize",
    })
    graph.add_edge("confirm_quote", "submit_content_task")
    graph.add_edge("submit_content_task", "read_task_status")
    graph.add_edge("read_task_status", "summarize")
    graph.add_edge("summarize", END)
    return graph.compile(checkpointer=checkpointer or InMemorySaver())


async def _bounded_parallel_search(queries: list[dict[str, Any]], tools: ProductContentPort) -> list[dict[str, Any]]:
    if not 1 <= len(queries) <= 4:
        raise ValueError("the plan must contain 1 to 4 product queries")
    results = await asyncio.gather(*(tools.search_products(query) for query in queries))
    return [dict(item) for item in results]


def _validate_plan_content(plan: AgentPlan) -> None:
    if plan.create_content != (plan.content_type == "product_script"):
        raise ValueError("content_type must be product_script only when create_content is true")


def _extract_products(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    value = result.get("products", result.get("data", []))
    if isinstance(value, dict):
        value = value.get("products", [])
    return [dict(item) for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []


def _quote_amount(quote: Mapping[str, Any]) -> Decimal:
    for key in ("totalAmount", "total_amount", "amount", "estimatedCost", "estimated_cost"):
        value = quote.get(key)
        if value is not None:
            try:
                amount = Decimal(str(value))
            except InvalidOperation as error:
                raise ValueError("quote amount must be numeric") from error
            if not amount.is_finite() or amount < 0:
                raise ValueError("quote amount must be a finite non-negative number")
            return amount
    raise ValueError("quote response is missing a numeric amount")


def _required_identifier(value: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        result = value.get(key)
        if result is not None and str(result).strip():
            return str(result).strip()
    raise ValueError(f"service response is missing identifier ({', '.join(keys)})")

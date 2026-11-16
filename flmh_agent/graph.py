from __future__ import annotations

from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from .contracts import AgentState
from .logic import (
    build_mock_content_brief,
    confirmation_is_explicit,
    execute_product_queries,
    quote_within_budget,
    stable_idempotency_key,
)
from .mcp_client import McpToolClient
from .planner import build_demo_plan, validate_plan
from .slots import update_slots


def build_agent_graph(tool_client: Any | None = None, checkpointer: Any | None = None):
    client = tool_client or McpToolClient()
    graph = StateGraph(AgentState)

    async def plan_request(state: AgentState) -> dict[str, Any]:
        slots, invalidated = update_slots(state.get("slot_values"), state.get("slot_updates"))
        plan = build_demo_plan(state["user_request"], slots)
        return {
            "slot_values": slots,
            "invalidated": invalidated,
            "plan": plan,
            "product_results": [],
            "selected_product": {},
            "content_brief": {},
            "quote": {},
            "budget_ok": False,
            "confirmation": {},
            "confirmed_quote": {},
            "content_task": {},
            "task_status": {},
            "idempotency_key": "",
            "error": "",
        }

    def validate(state: AgentState) -> dict[str, Any]:
        validate_plan(state["plan"])
        return {}

    async def search(state: AgentState) -> dict[str, Any]:
        results = await execute_product_queries(state["plan"]["queries"], client.call)
        return {"product_results": results}

    def choose_product(state: AgentState) -> dict[str, Any]:
        product_slot = state["slot_values"].get("product")
        product = _select_product(state["product_results"], product_slot)
        if product is None:
            message = "没有找到指定商品；未创建内容任务。" if product_slot else "没有找到符合条件的商品；未创建内容任务。"
            return {"error": message}
        if not product.get("id"):
            return {"error": "商品服务返回记录缺少商品 ID；未创建内容任务。"}
        return {"selected_product": product}

    def route_after_selection(state: AgentState) -> str:
        if state.get("error"):
            return "summarize"
        return "quote" if state["plan"]["create_content"] else "summarize"

    async def quote(state: AgentState) -> dict[str, Any]:
        product = state["selected_product"]
        brief = build_mock_content_brief(product, state["user_request"])
        query = state["plan"]["queries"][0]
        key = stable_idempotency_key(
            user_request=state["user_request"],
            product_id=str(product.get("id", "")),
            content_type=state["plan"]["content_type"],
            budget_max=state["plan"]["budget_max"],
            region=str(product.get("region") or query["region"]),
        )
        quote_result = await client.call("create_content_quote", {
            "product_id": str(product.get("id", "")),
            "content_type": state["plan"]["content_type"],
            "brief": brief["body"],
            "budget_max": state["plan"]["budget_max"],
        })
        budget_ok = quote_within_budget(quote_result, state["plan"]["budget_max"])
        return {
            "content_brief": brief,
            "quote": quote_result,
            "budget_ok": budget_ok,
            "idempotency_key": key,
        }

    def route_after_quote(state: AgentState) -> str:
        return "confirm" if state.get("budget_ok") else "summarize"

    def await_confirmation(state: AgentState) -> dict[str, Any]:
        quote_id = _required_string(state["quote"], "quoteId", "id")
        decision = interrupt({
            "kind": "budget_confirmation",
            "quoteId": quote_id,
            "quote": state["quote"],
            "product": state["selected_product"],
            "contentBrief": state["content_brief"],
            "message": "请显式确认预算后再创建内容任务。",
            "source": "MOCK_LOCAL_DEMO",
        })
        confirmed = confirmation_is_explicit(decision)
        return {"confirmation": {"confirmed": confirmed, "response": decision}}

    def route_after_confirmation(state: AgentState) -> str:
        return "confirm_quote" if state.get("confirmation", {}).get("confirmed") is True else "summarize"

    async def confirm_quote(state: AgentState) -> dict[str, Any]:
        quote_id = _required_string(state["quote"], "quoteId", "id")
        result = await client.call("confirm_content_quote", {"quote_id": quote_id, "confirmed": True})
        return {"confirmed_quote": result}

    async def create_task(state: AgentState) -> dict[str, Any]:
        quote_id = _required_string(state["quote"], "quoteId", "id")
        product_id = str(state["selected_product"].get("id", ""))
        if not product_id:
            raise ValueError("selected product response is missing id")
        task = await client.call("create_content_task", {
            "quote_id": quote_id,
            "product_id": product_id,
            "content_type": state["plan"]["content_type"],
            "brief": state["content_brief"]["body"],
            "idempotency_key": state["idempotency_key"],
        })
        return {"content_task": task}

    async def check_task(state: AgentState) -> dict[str, Any]:
        task_id = _required_string(state["content_task"], "taskId", "id")
        status = await client.call("get_content_task", {"task_id": task_id})
        return {"task_status": status}

    def summarize(state: AgentState) -> dict[str, Any]:
        if state.get("error"):
            return {"summary": state["error"]}
        products = [product for result in state.get("product_results", []) for product in _products(result)]
        lines = [
            f"计划模式：{state['plan']['mode']}（MOCK，本地确定性规则）",
            f"商品查询结果：{len(products)} 件；商品数据源以 Gateway 返回值为准，Demo 数据会带来源标记。",
        ]
        if state["plan"]["create_content"]:
            if not state.get("budget_ok"):
                lines.append("报价超出预算上限或报价缺少金额，未请求用户确认，也未创建任务。")
            elif state.get("content_task"):
                lines.append(
                    f"内容任务已创建：{state['content_task']}；当前状态：{state.get('task_status', {})}。"
                )
                lines.append("内容草稿为 MOCK 模板输出，不代表真实模型生成结果。")
            elif state.get("confirmation", {}).get("confirmed") is True:
                lines.append("任务创建步骤未返回任务结果。")
            else:
                lines.append("等待用户显式确认预算；未创建内容任务。")
        return {"summary": "\n".join(lines)}

    graph.add_node("plan", plan_request)
    graph.add_node("validate_plan", validate)
    graph.add_node("search_products", search)
    graph.add_node("choose_product", choose_product)
    graph.add_node("quote", quote)
    graph.add_node("await_confirmation", await_confirmation)
    graph.add_node("confirm_quote", confirm_quote)
    graph.add_node("create_task", create_task)
    graph.add_node("check_task", check_task)
    graph.add_node("summarize", summarize)
    graph.add_edge(START, "plan")
    graph.add_edge("plan", "validate_plan")
    graph.add_edge("validate_plan", "search_products")
    graph.add_edge("search_products", "choose_product")
    graph.add_conditional_edges("choose_product", route_after_selection, {
        "quote": "quote",
        "summarize": "summarize",
    })
    graph.add_conditional_edges("quote", route_after_quote, {
        "confirm": "await_confirmation",
        "summarize": "summarize",
    })
    graph.add_conditional_edges("await_confirmation", route_after_confirmation, {
        "confirm_quote": "confirm_quote",
        "summarize": "summarize",
    })
    graph.add_edge("confirm_quote", "create_task")
    graph.add_edge("create_task", "check_task")
    graph.add_edge("check_task", "summarize")
    graph.add_edge("summarize", END)
    return graph.compile(checkpointer=checkpointer or InMemorySaver())


def _products(result: dict[str, Any]) -> list[dict[str, Any]]:
    value = result.get("products", result.get("data", []))
    if isinstance(value, dict):
        value = value.get("products", [])
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _select_product(results: list[dict[str, Any]], product_slot: str | None) -> dict[str, Any] | None:
    products = [product for result in results for product in _products(result)]
    if product_slot:
        matching = next((p for p in products if p.get("id") == product_slot or p.get("title") == product_slot), None)
        return matching
    return products[0] if products else None


def _required_string(value: dict[str, Any], *keys: str) -> str:
    for key in keys:
        result = value.get(key)
        if result is not None and str(result).strip():
            return str(result).strip()
    raise ValueError(f"Gateway response is missing one of {keys}")

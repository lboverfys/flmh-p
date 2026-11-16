from __future__ import annotations

from typing import Any, TypedDict


class ProductQuery(TypedDict, total=False):
    region: str
    keyword: str
    category: str
    max_price: float


class AgentPlan(TypedDict):
    mode: str
    summary: str
    queries: list[ProductQuery]
    create_content: bool
    content_type: str
    budget_max: float | None


class AgentState(TypedDict, total=False):
    user_request: str
    slot_values: dict[str, Any]
    slot_updates: dict[str, Any]
    invalidated: list[str]
    plan: AgentPlan
    product_results: list[dict[str, Any]]
    selected_product: dict[str, Any]
    content_brief: dict[str, Any]
    quote: dict[str, Any]
    budget_ok: bool
    confirmation: dict[str, Any]
    confirmed_quote: dict[str, Any]
    content_task: dict[str, Any]
    task_status: dict[str, Any]
    idempotency_key: str
    summary: str
    error: str

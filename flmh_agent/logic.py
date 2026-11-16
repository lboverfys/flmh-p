from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from typing import Any

from .contracts import ProductQuery


ToolCaller = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


async def execute_product_queries(
    queries: list[ProductQuery],
    call_tool: ToolCaller,
) -> list[dict[str, Any]]:
    """Run independent region/category searches concurrently, once per query."""
    return await asyncio.gather(*(
        call_tool("search_products", dict(query))
        for query in queries
    ))


def stable_idempotency_key(
    *,
    user_request: str,
    product_id: str,
    content_type: str,
    budget_max: float | None,
    region: str,
) -> str:
    canonical = {
        "user_request": " ".join(user_request.split()),
        "product_id": product_id.strip(),
        "content_type": content_type.strip(),
        "budget_max": budget_max,
        "region": region.strip().upper(),
    }
    payload = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "demo-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def quote_within_budget(quote: dict[str, Any], budget_max: float | None) -> bool:
    amount = quote_amount(quote)
    if amount is None:
        raise ValueError("quote response is missing a numeric amount")
    if budget_max is None:
        return True
    return amount <= budget_max


def confirmation_is_explicit(decision: Any) -> bool:
    return isinstance(decision, dict) and decision.get("confirmed") is True


def quote_amount(quote: dict[str, Any]) -> float | None:
    for key in ("totalAmount", "total_amount", "amount", "estimatedCost", "estimated_cost"):
        value = quote.get(key)
        if value is not None:
            return float(value)
    return None


def build_mock_content_brief(product: dict[str, Any], user_request: str) -> dict[str, Any]:
    title = str(product.get("title") or "Demo 商品")
    return {
        "source": "MOCK_LOCAL_TEMPLATE",
        "contentType": "product_script",
        "title": f"{title} 体验分享",
        "body": f"[Demo 模板草稿] 围绕“{title}”介绍主要特点和适用场景。成稿要求：{user_request.strip()}",
        "provider": "MOCK",
    }

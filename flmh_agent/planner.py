from __future__ import annotations

import re
import math
from typing import Any

from .contracts import AgentPlan, ProductQuery
from .slots import normalize_slots


REGION_ALIASES = {
    "印尼": "ID",
    "印度尼西亚": "ID",
    "美国": "US",
    "越南": "VN",
    "泰国": "TH",
    "英国": "GB",
    "马来西亚": "MY",
    "菲律宾": "PH",
}
CONTENT_TERMS = ("文案", "口播", "脚本", "内容", "视频")
CONTENT_TYPE = "product_script"
MAX_PARALLEL_REGION_QUERIES = 4
MAX_KEYWORD_LENGTH = 80
MAX_CATEGORY_LENGTH = 100
MAX_SUMMARY_LENGTH = 500
PLAN_FIELDS = {"mode", "summary", "queries", "create_content", "content_type", "budget_max"}
QUERY_FIELDS = {"region", "keyword", "category", "max_price"}


def build_demo_plan(user_request: str, slots: dict[str, Any] | None = None) -> AgentPlan:
    """Build a local plan; product slots are Gateway keywords and match by id or title."""
    text = user_request.strip()
    if not text:
        raise ValueError("user_request is required")

    normalized_slots = normalize_slots(slots)
    regions = _find_regions(text)
    if not regions:
        regions = [normalized_slots["region"]]
    if len(regions) > MAX_PARALLEL_REGION_QUERIES:
        raise ValueError(f"at most {MAX_PARALLEL_REGION_QUERIES} region queries are allowed")

    budget = normalized_slots["budget_max"]
    if budget is None:
        budget = _find_budget(text)

    keyword = normalized_slots["product"] or _find_keyword(text)
    queries: list[ProductQuery] = []
    for region in regions:
        query: ProductQuery = {"region": region}
        if keyword:
            query["keyword"] = keyword
        if budget is not None:
            query["max_price"] = budget
        queries.append(query)

    create_content = any(term in text for term in CONTENT_TERMS)
    return AgentPlan(
        mode="MOCK_RULES",
        summary="本地确定性规则计划；未调用 LLM。",
        queries=queries,
        create_content=create_content,
        content_type=CONTENT_TYPE if create_content else "",
        budget_max=budget,
    )


def validate_plan(plan: AgentPlan) -> None:
    if not isinstance(plan, dict):
        raise ValueError("plan must be an object")
    unknown_plan_fields = set(plan) - PLAN_FIELDS
    missing_plan_fields = PLAN_FIELDS - set(plan)
    if unknown_plan_fields:
        raise ValueError(f"unknown plan fields: {sorted(unknown_plan_fields, key=str)}")
    if missing_plan_fields:
        raise ValueError(f"missing plan fields: {sorted(missing_plan_fields)}")
    if plan["mode"] != "MOCK_RULES":
        raise ValueError("only the explicit MOCK_RULES planner is enabled in this Demo")
    if not isinstance(plan["summary"], str) or not plan["summary"].strip() or len(plan["summary"]) > MAX_SUMMARY_LENGTH:
        raise ValueError("summary must be a non-empty string of at most 500 characters")

    queries = plan["queries"]
    if not isinstance(queries, list) or not 1 <= len(queries) <= MAX_PARALLEL_REGION_QUERIES:
        raise ValueError("plan must contain 1 to 4 independent product queries")
    for query in queries:
        if not isinstance(query, dict):
            raise ValueError("each product query must be an object")
        unknown_query_fields = set(query) - QUERY_FIELDS
        if unknown_query_fields:
            raise ValueError(f"unknown product query fields: {sorted(unknown_query_fields, key=str)}")
        region = query.get("region")
        if not isinstance(region, str) or not re.fullmatch(r"[A-Z]{2}", region):
            raise ValueError(f"invalid region in plan: {region!r}")
        _validate_optional_text(query, "keyword", MAX_KEYWORD_LENGTH)
        _validate_optional_text(query, "category", MAX_CATEGORY_LENGTH)
        if "max_price" in query:
            _validate_nonnegative_number(query["max_price"], "max_price")

    if not isinstance(plan["create_content"], bool):
        raise ValueError("create_content must be a boolean")
    if not isinstance(plan["content_type"], str):
        raise ValueError("content_type must be a string")
    if plan["create_content"] and plan["content_type"] != CONTENT_TYPE:
        raise ValueError("unsupported Demo content type")
    if not plan["create_content"] and plan["content_type"] != "":
        raise ValueError("content_type must be empty when content creation is disabled")
    if plan["budget_max"] is not None:
        _validate_nonnegative_number(plan["budget_max"], "budget_max")


def _validate_optional_text(query: dict[str, Any], field: str, max_length: int) -> None:
    if field not in query:
        return
    value = query[field]
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise ValueError(f"{field} must be a non-empty string of at most {max_length} characters")


def _validate_nonnegative_number(value: Any, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a finite non-negative number")
    try:
        valid = math.isfinite(value) and value >= 0
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError(f"{field} must be a finite non-negative number")


def _find_regions(text: str) -> list[str]:
    matches = [(text.find(name), code) for name, code in REGION_ALIASES.items() if name in text]
    matches.sort()
    return list(dict.fromkeys(code for _, code in matches))


def _find_budget(text: str) -> float | None:
    match = re.search(r"(?:预算|最多|不超过|不高于)\s*(?:人民币|RMB|CNY|¥|￥)?\s*(\d+(?:\.\d+)?)", text, re.IGNORECASE)
    return float(match.group(1)) if match else None


def _find_keyword(text: str) -> str | None:
    cleaned = text
    for phrase in (
        "请帮我", "请给我", "帮我", "给我", "帮忙", "请问", "请", "在", "查找", "查询", "搜索", "查", "找",
        "生成", "制作", "写一段", "写个", "写", "比较", "对比", "选品并", "选品", "商品", "产品",
        "文案", "口播", "脚本", "内容", "视频", "看看", "最近", "新品", "爆品", "热门", "推荐", "预算", "最多", "不超过", "不高于", "并且", "以及", "并",
        "印尼", "印度尼西亚", "美国", "越南", "泰国", "英国", "马来西亚", "菲律宾",
    ):
        cleaned = cleaned.replace(phrase, " ")
    cleaned = re.sub(r"(?:人民币|RMB|CNY|¥|￥)?\s*\d+(?:\.\d+)?", " ", cleaned, flags=re.IGNORECASE)
    keyword = " ".join(cleaned.split()).strip(" ，,。；;的和并为给")
    return keyword[:80] or None

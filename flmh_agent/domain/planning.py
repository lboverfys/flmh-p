from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Protocol

from .models import AgentPlan, ProductQuery
from .slots import normalize_slots


REGION_ALIASES = {
    "印尼": "ID", "印度尼西亚": "ID", "美国": "US", "越南": "VN",
    "泰国": "TH", "英国": "GB", "马来西亚": "MY", "菲律宾": "PH",
}
CONTENT_TERMS = ("文案", "口播", "脚本", "内容", "视频")
BI_TERMS = ("销量", "销售额", "gmv", "增长", "排名", "top", "趋势", "机会分", "风险分", "排行榜", "类目统计", "数据分析")


class Planner(Protocol):
    async def plan(self, user_request: str, slots: Mapping[str, object]) -> AgentPlan: ...


class RuleBasedPlanner:
    """Deterministic local fallback. It never labels rules output as model output."""

    async def plan(self, user_request: str, slots: Mapping[str, object]) -> AgentPlan:
        text = user_request.strip()
        if not text:
            raise ValueError("user_request is required")
        normalized = normalize_slots(dict(slots))
        if any(term in text.casefold() for term in BI_TERMS):
            return AgentPlan(
                summary="本地规则规划；将问题交给受控 BI SQL 服务。",
                queries=[],
                create_content=False,
                content_type="",
                budget_max=normalized["budget_max"],
                analysis_question=text,
                source="RULES",
            )
        regions = _find_regions(text) or [normalized["region"]]
        if len(regions) > 4:
            raise ValueError("at most 4 independent region queries are allowed")
        budget = normalized["budget_max"]
        if budget is None:
            budget = _find_budget(text)
        keyword = normalized["product"] or _find_keyword(text)
        queries = []
        for region in regions:
            query: dict[str, object] = {"region": region}
            if keyword:
                query["keyword"] = keyword
            if budget is not None:
                query["max_price"] = budget
            queries.append(ProductQuery.model_validate(query))
        wants_content = any(term in text for term in CONTENT_TERMS)
        return AgentPlan(
            summary="本地规则规划；未调用模型。",
            queries=queries,
            create_content=wants_content,
            content_type="product_script" if wants_content else "",
            budget_max=budget,
            analysis_question=None,
            source="RULES",
        )


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
        *REGION_ALIASES.keys(),
    ):
        cleaned = cleaned.replace(phrase, " ")
    cleaned = re.sub(r"(?:人民币|RMB|CNY|¥|￥)?\s*\d+(?:\.\d+)?", " ", cleaned, flags=re.IGNORECASE)
    keyword = " ".join(cleaned.split()).strip(" ，,。；;的和并为给")
    return keyword[:80] or None

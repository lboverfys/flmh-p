from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Protocol

from .models import AgentPlan, ProductQuery
from .slots import is_product_identifier, normalize_slots


REGION_ALIASES = {
    "印尼": "ID", "印度尼西亚": "ID", "美国": "US", "越南": "VN",
    "泰国": "TH", "英国": "GB", "马来西亚": "MY", "菲律宾": "PH",
}
CONTENT_TERMS = ("文案", "口播", "脚本", "内容", "视频")
BI_TERMS = ("销量", "销售额", "gmv", "增长", "排名", "top", "趋势", "机会分", "风险分", "排行榜", "类目统计", "数据分析")
HISTORY_TERMS = ("历史", "趋势", "快照", "history", "trend")
DETAIL_TERMS = ("详情", "detail")


class Planner(Protocol):
    async def plan(self, user_request: str, slots: Mapping[str, object]) -> AgentPlan: ...


class RuleBasedPlanner:
    """Deterministic local fallback. It never labels rules output as model output."""

    async def plan(self, user_request: str, slots: Mapping[str, object]) -> AgentPlan:
        text = user_request.strip()
        if not text:
            raise ValueError("user_request is required")
        normalized = normalize_slots(dict(slots))
        knowledge_query = text if "知识库" in text or "检索资料" in text else None
        wants_content = any(term in text for term in CONTENT_TERMS)
        if knowledge_query and not wants_content:
            return AgentPlan(summary="本地规则规划；检索已配置知识库并保留来源。", queries=[], create_content=False,
                             content_type="", knowledge_query=knowledge_query, source="RULES")
        history_id = find_history_product_id(text, slots)
        if history_id is not None and not wants_content:
            return AgentPlan(summary="本地规则规划；查询指定商品的指标快照，保留采集时间与来源。",
                             queries=[], create_content=False, content_type="", product_history_id=history_id, source="RULES")
        detail_id = find_detail_product_id(text, slots)
        if detail_id is not None and not wants_content:
            return AgentPlan(summary="本地规则规划；查询指定商品详情，保留来源与未知字段。",
                             queries=[], create_content=False, content_type="", product_detail_id=detail_id, source="RULES")
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
        regions = ([normalized["region"]] if "region" in slots else _find_regions(text)) or [normalized["region"]]
        if len(regions) > 4:
            raise ValueError("at most 4 independent region queries are allowed")
        budget = normalized["budget_max"]
        if "budget_max" not in slots:
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
        return AgentPlan(
            summary="本地规则规划；未调用模型。",
            queries=queries,
            create_content=wants_content,
            content_type="product_script" if wants_content else "",
            budget_max=budget,
            analysis_question=None,
            knowledge_query=knowledge_query,
            source="RULES",
        )


def find_history_product_id(text: str, slots: Mapping[str, object]) -> str | None:
    return _find_product_id(text, slots, HISTORY_TERMS)


def find_detail_product_id(text: str, slots: Mapping[str, object]) -> str | None:
    return _find_product_id(text, slots, DETAIL_TERMS)


def _find_product_id(text: str, slots: Mapping[str, object], intent_terms: tuple[str, ...]) -> str | None:
    if not any(term in text.casefold() for term in intent_terms):
        return None
    product_slot = slots.get("product")
    if isinstance(product_slot, str):
        product_slot = product_slot.strip()
    if "product" in slots:
        return product_slot if isinstance(product_slot, str) and is_product_identifier(product_slot) else None
    labeled = re.findall(r"(?:商品\s*(?:ID|编号|标识)|product\s*ID)\s*[:：=]?\s*([A-Za-z0-9][A-Za-z0-9_.:-]{0,95})(?![A-Za-z0-9_.:-])", text, re.IGNORECASE)
    if labeled:
        unique = list(dict.fromkeys(labeled))
        return unique[0] if len(unique) == 1 else None
    candidates = [token for token in re.findall(r"(?<![A-Za-z0-9_.:-])([A-Za-z0-9][A-Za-z0-9_.:-]{0,95})(?![A-Za-z0-9_.:-])", text)
                  if token[0].isalpha() and is_product_identifier(token)]
    unique = list(dict.fromkeys(candidates))
    return unique[0] if len(unique) == 1 else None


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

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Protocol

from .models import AgentPlan, ProductQuery
from .media_production import explicit_media_options, without_media_fields
from .slots import is_product_identifier, normalize_slots


REGION_ALIASES = {
    "印尼": "ID", "印度尼西亚": "ID", "美国": "US", "越南": "VN",
    "泰国": "TH", "英国": "GB", "马来西亚": "MY", "菲律宾": "PH",
}
_REGION_NAME = "(?:" + "|".join(REGION_ALIASES) + ")"
_REGION_GROUP = re.compile(_REGION_NAME + r"(?:\s*(?:和|与|及|以及|、)\s*" + _REGION_NAME + ")*")
CONTENT_TERMS = ("文案", "口播", "脚本", "内容", "视频", "图片")
BI_TERMS = ("销量", "销售额", "gmv", "增长", "排名", "top", "趋势", "机会分", "风险分", "排行榜", "类目统计", "数据分析")
CREATOR_COUNT_TERMS = ("关联达人数", "关联达人数量")
HISTORY_TERMS = ("历史", "趋势", "快照", "history", "trend", *CREATOR_COUNT_TERMS)
DETAIL_TERMS = ("详情", "detail")
DAILY_TERMS = ("日统计", "日报", "每日统计", "日评分", "daily")

_AMOUNT = r"\s*(人民币|美元|美金|[A-Z]{3}|¥|￥)?\s*(\d+(?:\.\d+)?)\s*(人民币|美元|美金|[A-Z]{3}|元)?"
_CHANGE = r"\s*(?:改为|改成|修改为|修改成|调整为|调整成|[:：=]|为)?\s*"
_LIMIT = r"(?:不超过|不高于|最多)"
_BUDGET = re.compile(r"(?:创作|内容(?:生产)?|制作|生产)?预算(?:上限)?" + _CHANGE + _LIMIT + "?" + _AMOUNT, re.I)
_PRICE = re.compile(r"(?:(?:商品|产品)?(?:价格|单价|售价|限价)|(?:商品|产品|选品|采购|购买)预算)(?:上限)?"
                    + _CHANGE + _LIMIT + "?" + _AMOUNT, re.I)
_BARE_LIMIT = re.compile(_LIMIT + _AMOUNT, re.I)


class Planner(Protocol):
    async def plan(self, user_request: str, slots: Mapping[str, object]) -> AgentPlan: ...


class RuleBasedPlanner:
    """Deterministic local fallback. It never labels rules output as model output."""

    async def plan(self, user_request: str, slots: Mapping[str, object]) -> AgentPlan:
        text = user_request.strip()
        if not text:
            raise ValueError("user_request is required")
        normalized = normalize_slots(dict(slots))
        ranking = re.fullmatch(r"(?:请|请帮我)?(?:查询|查看|查|看)?\s*(" + "|".join(REGION_ALIASES) + r"|[A-Z]{2})\s*(?:地区)?商品榜单(?:前\s*(\d{1,3})\s*(?:名|个|件)?)?[。？?]?", text)
        if ranking:
            region = normalized["region"] if "region" in slots else REGION_ALIASES.get(ranking[1], ranking[1])
            return AgentPlan(summary="按地区读取已发布榜单索引。", queries=[ProductQuery(region=region, ranking_limit=int(ranking[2] or 20))],
                             create_content=False, content_type="", source="RULES")
        knowledge_query = text if "知识库" in text or "检索资料" in text else None
        intent = _BUDGET.sub("", text)
        wants_content = any(term in intent for term in CONTENT_TERMS)
        wants_media = "视频" in text and not any(term in text for term in ("脚本", "文案", "口播"))
        daily_id = find_daily_product_id(text, slots)
        if daily_id is not None and not wants_content:
            return AgentPlan(summary="本地规则规划；读取指定商品的来源日统计与评分，不临时编造或写入统计。",
                             queries=[], create_content=False, content_type="", product_daily_id=daily_id, source="RULES")
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
        filters = without_media_fields(text)
        request_regions = _find_regions(filters)
        regions = ([normalized["region"]] if "region" in slots else request_regions) or [normalized["region"]]
        if len(regions) > 4:
            raise ValueError("at most 4 independent region queries are allowed")
        amounts = request_amounts(filters, wants_content)
        budget = slots.get("budget_max", amounts.get("budget_max"))
        price = amounts.get("product_max_price")
        keyword = normalized["product"] if "product" in slots else _find_keyword(filters)
        regional = _regional_filters(filters, request_regions, wants_content)
        regional_keywords = "product" not in slots and any(word is not None for word, _ in regional.values())
        regional_limits = any(limit is not None for _, limit in regional.values())
        queries = []
        for region in regions:
            local_keyword, local_price = regional.get(region, (None, None))
            query_keyword = local_keyword if regional_keywords else keyword
            query_price = local_price if regional_limits else price
            queries.append(ProductQuery(region=region, keyword=query_keyword,
                                        max_price=slots.get("product_max_price", query_price)))
        return AgentPlan(
            summary="本地规则规划；未调用模型。",
            queries=queries,
            create_content=wants_content,
            content_type="product_media" if wants_media else "product_script" if wants_content else "",
            budget_max=budget,
            budget_currency=find_budget_currency(text, create_content=wants_content) if "budget_max" in amounts else None,
            media_options=explicit_media_options(text) if wants_media else {},
            analysis_question=None,
            knowledge_query=knowledge_query,
            source="RULES",
        )


def find_history_product_id(text: str, slots: Mapping[str, object]) -> str | None:
    return _find_product_id(text, slots, HISTORY_TERMS)


def find_detail_product_id(text: str, slots: Mapping[str, object]) -> str | None:
    return _find_product_id(text, slots, DETAIL_TERMS)


def find_daily_product_id(text: str, slots: Mapping[str, object]) -> str | None:
    return _find_product_id(text, slots, DAILY_TERMS)


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


def _money_matches(text: str, create_content: bool) -> tuple[re.Match | None, re.Match | None]:
    text = without_media_fields(text)
    price = _PRICE.search(text)
    remaining = _PRICE.sub(" ", text)
    budget = None
    for match in _BUDGET.finditer(remaining):
        if not create_content and match[0].startswith("预算"):
            price = price or match
        elif budget is None:
            budget = match
    if price is None and not create_content:
        price = _BARE_LIMIT.search(_BUDGET.sub(" ", remaining))
    return price, budget


def request_amounts(text: str, create_content: bool) -> dict[str, float]:
    price, budget = _money_matches(text, create_content)
    amounts = {"product_max_price": float(price[2])} if price else {}
    if budget:
        amounts["budget_max"] = float(budget[2])
    return amounts


def _regional_filters(text: str, regions: list[str], create_content: bool) -> dict[str, tuple[str | None, float | None]]:
    if len(regions) < 2:
        return {}
    scoped = {}
    active = []
    # Enumeration commas inside a region list join its members; elsewhere they separate filters.
    text = _REGION_GROUP.sub(lambda m: m[0].replace("、", "和"), text)
    for clause in re.split(r"[，,；;。、\n]", text):
        boundaries = [match.start() for match in _REGION_GROUP.finditer(clause)][1:]
        for start, end in zip([0, *boundaries], [*boundaries, len(clause)]):
            part = clause[start:end]
            local = _find_regions(part)
            if local:
                active = local
            keyword = _find_keyword(part) if local else None
            price = request_amounts(part, create_content).get("product_max_price")
            for region in active:
                previous_keyword, previous_price = scoped.get(region, (None, None))
                scoped[region] = (keyword or previous_keyword, price if price is not None else previous_price)
    return scoped


def money_revision_slots(text: str, create_content: bool) -> dict[str, float]:
    remaining = _BUDGET.sub("", _PRICE.sub("", text))
    if not create_content:
        remaining = _BARE_LIMIT.sub("", remaining)
    if re.fullmatch(r"(?:[\s，,；;。！!、]|请|把|将|并且|并|和|以及)*", remaining):
        return request_amounts(text, create_content)
    return {}


def explicit_revision_slots(text: str) -> dict[str, object]:
    updates: dict[str, object] = {}
    text = without_media_fields(text)
    start = r"(?:^|[，,；;。\n])\s*(?:请)?(?:把)?\s*"
    product = re.search(start + r"(?:商品|产品)\s*(?:改为|改成|修改为|修改成|换成|调整为|调整成|[:：=])\s*([^，,；;。\n]+)", text)
    if product:
        updates["product"] = product.group(1).strip()
    region = re.search(start + r"(?:地区|市场|国家)\s*(?:改为|改成|修改为|修改成|换成|调整为|调整成|[:：=])\s*([^，,；;。\n]+)", text)
    if region:
        raw = region.group(1).strip()
        updates["region"] = REGION_ALIASES.get(raw, raw)
    updates.update(request_amounts(text, create_content=True))
    normalized = normalize_slots(updates)
    return {key: normalized[key] for key in updates}


def find_budget_currency(text: str, *, create_content: bool = True) -> str | None:
    _, match = _money_matches(text, create_content)
    if not match:
        return None
    unit = (match.group(1) or match.group(3) or "").upper()
    if unit in {"人民币", "RMB", "CNY", "¥", "￥", "元"}:
        return "CNY"
    if unit in {"美元", "美金", "USD"}:
        return "USD"
    if re.fullmatch(r"[A-Z]{3}", unit):
        return unit
    return None


def _find_keyword(text: str) -> str | None:
    cleaned = _BARE_LIMIT.sub(" ", _BUDGET.sub(" ", _PRICE.sub(" ", text)))
    for phrase in (
        "请帮我", "请给我", "帮我", "给我", "帮忙", "请问", "请", "在", "查找", "查询", "搜索", "查", "找",
        "生成", "制作", "写一段", "写个", "写", "比较", "对比", "选品并", "选品", "商品", "产品",
        "文案", "口播", "脚本", "内容", "视频", "图片", "一款", "看看", "最近", "新品", "爆品", "热门", "推荐", "预算", "最多", "不超过", "不高于", "并且", "以及", "并",
        *REGION_ALIASES.keys(),
    ):
        cleaned = cleaned.replace(phrase, " ")
    cleaned = re.sub(r"(?:人民币|美元|美金|[A-Z]{3}|¥|￥)?\s*\d+(?:\.\d+)?\s*(?:人民币|美元|美金|[A-Z]{3}|元)?", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"[，,。；;、]", " ", cleaned)
    keyword = " ".join(cleaned.split()).strip(" ，,。；;的和并为给")
    return keyword[:80] or None

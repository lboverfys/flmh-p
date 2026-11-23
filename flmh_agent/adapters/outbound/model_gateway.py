from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

import httpx

from flmh_agent.config import Settings
from flmh_agent.domain.models import AgentPlan, PlanResponse
from flmh_agent.domain.identity import current_identity
from flmh_agent.domain.planning import find_budget_currency, find_daily_product_id, find_detail_product_id, find_history_product_id


class ModelGatewayClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._client = client

    async def complete(
        self,
        *,
        purpose: str,
        messages: Sequence[Mapping[str, str]],
        temperature: float = 0.0,
        max_tokens: int = 2048,
        response_format: str = "json_object",
    ) -> dict[str, Any]:
        payload = {
            "purpose": purpose,
            "messages": [dict(message) for message in messages],
            "temperature": temperature,
            "maxTokens": max_tokens,
            "responseFormat": response_format,
        }
        headers = {"Authorization": current_identity().authorization, "X-Client-Id": "flmh-agent"}
        if self.settings.model_gateway_api_key:
            headers["X-Service-Key"] = self.settings.model_gateway_api_key
        result = await self._post("/api/v1/models/completions", payload, headers)
        content = result.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("model gateway response is missing content")
        return result

    async def generate_sql(
        self, question: str, schema_context: str, *, previous_sql: str = "", error: str = ""
    ) -> dict[str, Any]:
        dialect_label = "MySQL 8" if self.settings.sql_dialect == "mysql" else "PostgreSQL"
        instruction = (
            f"根据授权表结构为 BI 问题生成单条 {dialect_label} 只读 SELECT。仅返回 JSON 对象 {{sql, reason}}。"
            f"必须使用具体列名、带 LIMIT、结果不超过 {self.settings.sql_max_rows} 行。market_radar_metrics 默认过滤 score_version='{self.settings.sql_default_score_version}'，product_daily_stat 使用 score_version='{self.settings.sql_daily_score_version}'。"
        )
        payload = {"question": question, "schema": schema_context, "previousSql": previous_sql, "error": error}
        response = await self.complete(
            purpose="text_to_sql",
            messages=[{"role": "system", "content": instruction}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
            temperature=0.05,
            max_tokens=1200,
        )
        value = _json_content(response["content"])
        sql = value.get("sql")
        if not isinstance(sql, str) or not sql.strip():
            raise ValueError("model gateway did not return SQL")
        return {"sql": sql.strip(), "reason": str(value.get("reason", ""))}

    async def _post(self, path: str, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        if self._client is not None:
            response = await self._client.post(path, json=payload, headers=headers)
        else:
            async with httpx.AsyncClient(
                base_url=self.settings.model_gateway_url,
                timeout=self.settings.request_timeout_seconds,
                trust_env=False,
            ) as client:
                response = await client.post(path, json=payload, headers=headers)
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, dict):
            raise TypeError("model gateway returned a non-object response")
        return value


class ModelGatewaySqlGenerator:
    def __init__(self, model_gateway: ModelGatewayClient) -> None:
        self.model_gateway = model_gateway

    async def generate_sql(
        self,
        question: str,
        schema_context: str,
        *,
        previous_sql: str = "",
        error: str = "",
    ) -> Mapping[str, Any]:
        return await self.model_gateway.generate_sql(
            question, schema_context, previous_sql=previous_sql, error=error
        )

class ModelGatewayPlanner:
    def __init__(self, model_gateway: ModelGatewayClient) -> None:
        self.model_gateway = model_gateway

    async def plan(self, user_request: str, slots: Mapping[str, object]) -> AgentPlan:
        settings = getattr(self.model_gateway, "settings", None)
        max_queries = getattr(settings, "agent_max_queries_per_plan", 6)
        complete_kwargs: dict[str, Any] = {}
        if settings is not None:
            complete_kwargs["temperature"] = settings.agent_planner_temperature
            complete_kwargs["max_tokens"] = settings.agent_planner_max_tokens
        response = await self.model_gateway.complete(
            purpose="agent_plan",
            **complete_kwargs,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "将用户请求转换为严格 JSON 计划，不执行工具调用。结构为 {summary, queries, create_content, "
                        "content_type, target_language, budget_max, budget_currency, media_options, analysis_question, knowledge_query, product_history_id, product_detail_id, product_daily_id}。"
                        f"商品流程中 queries 为 1 至 {max_queries} 条商品或趋势/达人查询，"
                        "analysis_question=null；BI 分析流程中 queries=[] 且 analysis_question 为原始分析问题。"
                        "商品查询字段为 region、keyword、category、max_price，以及可选的 tool_kind（product_search/product_ranking/product_trend/product_creators）、query_id、depends_on、category_from_query、product_id、product_id_from_query、trend_days、creator_limit；"
                        "查地区商品榜单时给该条查询 ranking_limit（1到50），不带目录筛选；查询某商品的关联达人数用 product_history_id，只返回计数观测，不提供达人名单。"
                        "depends_on 只可引用本计划已有 query_id，依赖必须无环；互不依赖的查询将并行执行。"
                        "跨地区同类选品可用 category_from_query 指定 depends_on 内的直接上游查询 ID，"
                        "仅将该上游第一条商品的 category 绑定为本次查询的类目，不得同时填静态 category；"
                        "分析商品趋势或关联达人时可用 tool_kind='product_trend' 或 'product_creators' 并通过 product_id_from_query 绑定直接上游 query_id。"
                        "target_language 填写 BCP-47 语种标签（如 en-US、id-ID、th-TH、vi-VN、ms-MY、zh-CN）；未指明时为 null。"
                        "region 使用两位大写国家代码；显式 slots 优先于原始请求中的旧值；"
                        "content_type 仅允许 product_script、product_media 或空字符串。"
                        "文案/口播/视频脚本使用 product_script；实际图片到视频生产使用 product_media，"
                        "默认一张图片再生成6秒9:16视频；media_options 只填用户明确提供的 image_prompt、video_prompt、"
                        "aspect_ratio（1:1/3:4/4:3/9:16/16:9）、duration_seconds（2至15的整数）。未提供字段省略，"
                        "不得把整句话无条件当成视频提示词，不得生成 origin_run_id 或决定复用节点。规格与报价会在执行前展示并请求确认。不得推断未给出的预算。"
                        "budget_max 是创作预算。slots.product_max_price 是商品单价上限，对应目录查询的 max_price。"
                        "同时给出两种金额时分别填写。商品价格或币种不能充当创作预算或预算币种。"
                        "仅选品时，口语的‘预算50’或‘不超过50’表示商品限价。各地区可有各自的 max_price，"
                        "未明确全局限价时保留每条查询的筛选条件，榜单不带 max_price。"
                        "budget_currency 为用户明确预算币种的 ISO 三字码（人民币/元=CNY，美元=USD）；未指定时为 null。不得换汇或猜币种。"
                        "用户要求检索知识库或询问运营规范时 knowledge_query 为检索问题；可独立检索或为商品内容补充事实，未要求时为 null。"
                        "指定明确商品 ID（如 camping-lamp-001）的历史/趋势请求只填 product_history_id，queries=[]，"
                        "create_content=false，content_type=''，analysis_question 和 knowledge_query 为 null。"
                        "明确商品 ID 的详情/detail 请求只填 product_detail_id，queries=[]，create_content=false，"
                        "content_type=''，analysis_question、knowledge_query 和 product_history_id 为 null；详情和历史不可混在同一计划。"
                        "只可使用原始请求或 product 槽位明确提供的商品 ID，不得由商品名猜 ID；未给 ID 时保持 BI/商品检索。"
                        "明确商品 ID 的日统计/日报/日评分/daily 请求只填 product_daily_id，queries=[]，create_content=false，"
                        "content_type=''，其他独立查询字段为 null；该接口只读已有来源日统计，不执行重算或补造缺失日期。"
                    ),
                },
                {"role": "user", "content": json.dumps({"request": user_request, "slots": dict(slots)}, ensure_ascii=False)},
            ],
        )
        raw = _json_content(response["content"])
        plan = PlanResponse.model_validate(raw)
        currency = find_budget_currency(user_request, create_content=plan.create_content)
        plan = plan.model_copy(update={"budget_currency": currency})
        if plan.create_content != (plan.content_type in {"product_script", "product_media"}):
            raise ValueError("model plan has inconsistent content_type")
        if plan.product_history_id is not None and plan.product_history_id != find_history_product_id(user_request, slots):
            raise ValueError("product history ID must match the explicit request or product slot")
        if plan.product_detail_id is not None and plan.product_detail_id != find_detail_product_id(user_request, slots):
            raise ValueError("product detail ID must match the explicit request or product slot")
        if plan.product_daily_id is not None and plan.product_daily_id != find_daily_product_id(user_request, slots):
            raise ValueError("product daily ID must match the explicit request or product slot")
        return AgentPlan(**plan.model_dump(), source="MODEL_GATEWAY")


def _json_content(content: str) -> dict[str, Any]:
    try:
        value = json.loads(content)
    except json.JSONDecodeError as error:
        raise ValueError("model gateway content is not valid JSON") from error
    if not isinstance(value, dict):
        raise ValueError("model gateway content must be a JSON object")
    return value

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

import httpx

from flmh_agent.config import Settings
from flmh_agent.domain.models import AgentPlan, PlanResponse


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
        headers = {"X-Service-Key": self.settings.model_gateway_api_key, "X-Client-Id": "flmh-agent"} if self.settings.model_gateway_api_key else {}
        result = await self._post("/api/v1/models/completions", payload, headers)
        content = result.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("model gateway response is missing content")
        return result

    async def generate_sql(
        self, question: str, schema_context: str, *, previous_sql: str = "", error: str = ""
    ) -> dict[str, Any]:
        instruction = (
            "根据授权表结构为 BI 问题生成单条 PostgreSQL 只读 SELECT。仅返回 JSON 对象 {sql, reason}。"
            "必须使用具体列名、带 LIMIT、结果不超过 50 行。市场雷达指标默认过滤 score_version='v2'。"
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
        response = await self.model_gateway.complete(
            purpose="agent_plan",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "将用户请求转换为严格 JSON 计划，不执行工具调用。结构为 {summary, queries, create_content, "
                        "content_type, budget_max, analysis_question, knowledge_query}。商品流程中 queries 为 1 至 4 条商品查询，"
                        "analysis_question=null；BI 分析流程中 queries=[] 且 analysis_question 为原始分析问题。"
                        "商品查询字段为 region、keyword、category、max_price，以及可选的 query_id、depends_on；"
                        "depends_on 只可引用本计划已有 query_id，依赖必须无环；互不依赖的查询将并行执行。"
                        "region 使用两位大写国家代码；显式 slots 优先于原始请求中的旧值；"
                        "content_type 仅允许 product_script 或空字符串。不得推断未给出的预算。"
                        "用户要求检索知识库时 knowledge_query 为检索问题；可独立检索或为商品内容补充事实，未要求时为 null。"
                    ),
                },
                {"role": "user", "content": json.dumps({"request": user_request, "slots": dict(slots)}, ensure_ascii=False)},
            ],
        )
        raw = _json_content(response["content"])
        plan = PlanResponse.model_validate(raw)
        if plan.create_content != (plan.content_type == "product_script"):
            raise ValueError("model plan has inconsistent content_type")
        return AgentPlan(**plan.model_dump(), source="MODEL_GATEWAY")


def _json_content(content: str) -> dict[str, Any]:
    try:
        value = json.loads(content)
    except json.JSONDecodeError as error:
        raise ValueError("model gateway content is not valid JSON") from error
    if not isinstance(value, dict):
        raise ValueError("model gateway content must be a JSON object")
    return value

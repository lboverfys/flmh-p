"""Run a supplied plan against the local MCP service; no model provider is called."""
from __future__ import annotations

import asyncio
import json

from flmh_agent.adapters.outbound.mcp_client import McpProductContentAdapter
from flmh_agent.application.workflow import build_workflow
from flmh_agent.config import Settings
from flmh_agent.domain.models import AgentPlan, ProductQuery


class SuppliedPlanner:
    async def plan(self, user_request, slots):
        return AgentPlan(
            summary="显式示例计划：查询美国露营灯，再查询加拿大同类商品；未调用模型。",
            queries=[
                ProductQuery(region="US", keyword="露营灯", query_id="origin"),
                ProductQuery(region="CA", query_id="same_category", depends_on=["origin"],
                             category_from_query="origin"),
            ],
            create_content=False,
            content_type="",
            source="RULES",
        )


async def main() -> None:
    async with asyncio.timeout(30):
        async with McpProductContentAdapter(Settings()) as tools:
            result = await build_workflow(planner=SuppliedPlanner(), tools=tools).ainvoke(
                {"user_request": "查询美国露营灯，再查询加拿大同类商品"},
                {"configurable": {"thread_id": "cross-region-category-example"}},
            )
    if result.get("query_failures"):
        raise RuntimeError(f"查询失败：{result['query_failures']}")
    responses = result["product_results"]
    if len(responses) != 2 or not all(response["products"] for response in responses):
        raise RuntimeError("本地商品夹具未返回两组商品")
    category = responses[0]["products"][0]["category"]
    if not all(product["category"] == category and product["region"] == "CA"
               for product in responses[1]["products"]):
        raise RuntimeError("下游查询没有按上游类目筛选")
    print(json.dumps({"planSource": "SUPPLIED_EXAMPLE", "category": category,
                      "results": responses, "queryFailures": result.get("query_failures", {})},
                     ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())

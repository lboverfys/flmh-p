from __future__ import annotations

import json
from contextlib import AsyncExitStack
from datetime import timedelta
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from flmh_agent.config import Settings
from flmh_agent.domain.identity import UserIdentity, current_identity


REQUIRED_TOOLS = {
    "product_ranking", "product_creator_observations",
    "product_search", "product_history", "product_detail", "content_quote_create", "content_quote_confirm", "content_task_submit", "content_task_get",
}


class McpProductContentAdapter:
    def __init__(self, settings: Settings, *, identity: UserIdentity | None = None) -> None:
        self.settings = settings
        self._identity = identity
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None
        self._tool_names: set[str] = set()

    async def __aenter__(self) -> McpProductContentAdapter:
        stack = AsyncExitStack()
        try:
            identity = self._identity or current_identity()
            headers = {"Authorization": identity.authorization}
            if self.settings.agent_api_key:
                headers["X-Agent-Key"] = self.settings.agent_api_key
            read_stream, write_stream, _ = await stack.enter_async_context(
                streamablehttp_client(self.settings.mcp_tools_url, timeout=self.settings.request_timeout_seconds,
                                      sse_read_timeout=self.settings.request_timeout_seconds, headers=headers)
            )
            session = await stack.enter_async_context(ClientSession(read_stream, write_stream,
                read_timeout_seconds=timedelta(seconds=self.settings.request_timeout_seconds)))
            await session.initialize()
            tools = await session.list_tools()
            self._tool_names = {tool.name for tool in tools.tools}
            missing = REQUIRED_TOOLS - self._tool_names
            if missing:
                raise RuntimeError(f"MCP tool service is missing required tools: {', '.join(sorted(missing))}")
            self._stack = stack
            self._session = session
            return self
        except BaseException:
            await stack.aclose()
            raise

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self._stack is not None:
            await self._stack.aclose()
            self._stack = None
            self._session = None

    async def search_products(self, query: dict[str, Any]) -> dict[str, Any]:
        if query.get("ranking_limit") is not None:
            return await self._call("product_ranking", {"region": query["region"], "limit": query["ranking_limit"]})
        return await self._call("product_search", {key: value for key, value in query.items() if key != "ranking_limit"})

    async def get_creator_observations(self, product_id: str) -> dict[str, Any]:
        return await self._call("product_creator_observations", {"product_id": product_id})

    async def get_product_history(self, product_id: str, *, limit: int = 30, before_version: int | None = None) -> dict[str, Any]:
        return await self._call("product_history", {"product_id": product_id, "limit": limit, "before_version": before_version})

    async def get_product_detail(self, product_id: str) -> dict[str, Any]:
        return await self._call("product_detail", {"product_id": product_id})

    async def get_product_daily(self, product_id: str, source: str, *, limit: int = 30, before_date: str | None = None) -> dict[str, Any]:
        return await self._call("product_daily_statistics", {"product_id": product_id, "source": source,
                                 "limit": limit, "before_date": before_date})

    async def get_product_trend(self, product_id: str, *, days: int = 30) -> dict[str, Any]:
        return await self._call("product_trend", {"product_id": product_id, "days": days})

    async def get_product_creators(self, product_id: str, *, limit: int = 20) -> dict[str, Any]:
        return await self._call("product_creators", {"product_id": product_id, "limit": limit})

    async def search_creators(
        self,
        region: str = "US",
        *,
        keyword: str | None = None,
        category: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        return await self._call("creator_search", {"region": region, "keyword": keyword, "category": category, "limit": limit})

    async def create_quote(self, request: dict[str, Any]) -> dict[str, Any]:
        return await self._call("content_quote_create", request)

    async def confirm_quote(self, quote_id: str) -> dict[str, Any]:
        return await self._call("content_quote_confirm", {"quote_id": quote_id, "confirmed": True})

    async def submit_task(self, request: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        return await self._call("content_task_submit", {**request, "idempotency_key": idempotency_key})

    async def get_task(self, task_id: str) -> dict[str, Any]:
        return await self._call("content_task_get", {"task_id": task_id})

    async def create_media_quote(self, request: dict[str, Any]) -> dict[str, Any]:
        return await self._call("media_production_quote", request)

    async def submit_media_run(self, quote_id: str, idempotency_key: str) -> dict[str, Any]:
        return await self._call("media_production_submit", {"quote_id": quote_id, "idempotency_key": idempotency_key})

    async def get_media_run(self, run_id: str) -> dict[str, Any]:
        return await self._call("media_production_get", {"run_id": run_id})

    async def advance_media_run(self, run_id: str) -> dict[str, Any]:
        return await self._call("media_production_advance", {"run_id": run_id})

    async def verify_media_run(self, run_id: str) -> dict[str, Any]:
        return await self._call("media_production_verify", {"run_id": run_id})

    async def revise_media_run(self, run_id: str, quote_id: str, idempotency_key: str) -> dict[str, Any]:
        return await self._call("media_production_revise", {"run_id": run_id, "quote_id": quote_id, "idempotency_key": idempotency_key})

    async def media_workflow_capabilities(self) -> dict[str, Any]:
        return await self._call("media_workflow_capabilities", {})

    async def start_media_workflow(self, request: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        return await self._call("media_workflow_start", {**request, "idempotency_key": idempotency_key})

    async def simulate_media_workflow(self, run_id: str) -> dict[str, Any]:
        return await self._call("media_workflow_simulate", {"run_id": run_id})

    async def get_media_workflow(self, run_id: str) -> dict[str, Any]:
        return await self._call("media_workflow_get", {"run_id": run_id})

    async def cancel_media_workflow(self, run_id: str) -> dict[str, Any]:
        return await self._call("media_workflow_cancel", {"run_id": run_id})

    async def _call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if self._session is None:
            raise RuntimeError("MCP adapter is not connected")
        result = await self._session.call_tool(name, arguments)
        if result.isError:
            raise RuntimeError(_read_text(result.content) or f"MCP tool failed: {name}")
        structured = getattr(result, "structuredContent", None)
        if isinstance(structured, dict):
            return structured
        content = _read_text(result.content)
        value = json.loads(content)
        if not isinstance(value, dict):
            raise TypeError(f"MCP tool {name} returned a non-object response")
        return value


def _read_text(content: list[Any]) -> str:
    return "\n".join(str(item.text) for item in content if getattr(item, "type", None) == "text")

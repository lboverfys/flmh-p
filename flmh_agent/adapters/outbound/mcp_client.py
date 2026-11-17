from __future__ import annotations

import json
from contextlib import AsyncExitStack
from datetime import timedelta
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from flmh_agent.config import Settings


REQUIRED_TOOLS = {
    "product_search", "product_history", "product_detail", "content_quote_create", "content_quote_confirm", "content_task_submit", "content_task_get",
}


class McpProductContentAdapter:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None
        self._tool_names: set[str] = set()

    async def __aenter__(self) -> McpProductContentAdapter:
        stack = AsyncExitStack()
        try:
            read_stream, write_stream, _ = await stack.enter_async_context(
                streamablehttp_client(self.settings.mcp_tools_url, timeout=self.settings.request_timeout_seconds,
                                      sse_read_timeout=self.settings.request_timeout_seconds)
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
        return await self._call("product_search", query)

    async def get_product_history(self, product_id: str, *, limit: int = 30, before_version: int | None = None) -> dict[str, Any]:
        return await self._call("product_history", {"product_id": product_id, "limit": limit, "before_version": before_version})

    async def get_product_detail(self, product_id: str) -> dict[str, Any]:
        return await self._call("product_detail", {"product_id": product_id})

    async def create_quote(self, request: dict[str, Any]) -> dict[str, Any]:
        return await self._call("content_quote_create", request)

    async def confirm_quote(self, quote_id: str) -> dict[str, Any]:
        return await self._call("content_quote_confirm", {"quote_id": quote_id, "confirmed": True})

    async def submit_task(self, request: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        return await self._call("content_task_submit", {**request, "idempotency_key": idempotency_key})

    async def get_task(self, task_id: str) -> dict[str, Any]:
        return await self._call("content_task_get", {"task_id": task_id})

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

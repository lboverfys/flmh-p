from __future__ import annotations

import json
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from .config import settings


class McpToolClient:
    """Small stateless MCP client; each call has an isolated HTTP session."""

    def __init__(self, server_url: str | None = None) -> None:
        self.server_url = server_url or settings.mcp_server_url

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        async with streamablehttp_client(self.server_url) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(name, arguments)
        if result.isError:
            raise RuntimeError(_read_text(result.content) or f"MCP tool failed: {name}")
        structured = getattr(result, "structuredContent", None)
        if isinstance(structured, dict):
            return structured
        raw = _read_text(result.content)
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise TypeError(f"MCP tool {name} returned a non-object result")
        return value


def _read_text(content: list[Any]) -> str:
    chunks = [str(item.text) for item in content if getattr(item, "type", None) == "text"]
    return "\n".join(chunks)

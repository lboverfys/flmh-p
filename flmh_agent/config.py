from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    gateway_url: str
    mcp_server_url: str
    mcp_host: str
    mcp_port: int
    llm_provider: str
    llm_model: str
    llm_api_key: str
    embedding_provider: str
    embedding_model: str
    vector_store_url: str


def load_settings() -> Settings:
    return Settings(
        gateway_url=os.getenv("JAVA_GATEWAY_URL", "http://127.0.0.1:8080").rstrip("/"),
        mcp_server_url=os.getenv("MCP_SERVER_URL", "http://127.0.0.1:8001/mcp"),
        mcp_host=os.getenv("MCP_HTTP_HOST", "127.0.0.1"),
        mcp_port=int(os.getenv("MCP_HTTP_PORT", "8001")),
        llm_provider=os.getenv("LLM_PROVIDER", ""),
        llm_model=os.getenv("LLM_MODEL", ""),
        llm_api_key=os.getenv("LLM_API_KEY", ""),
        embedding_provider=os.getenv("EMBEDDING_PROVIDER", ""),
        embedding_model=os.getenv("EMBEDDING_MODEL", ""),
        vector_store_url=os.getenv("VECTOR_STORE_URL", ""),
    )


settings = load_settings()

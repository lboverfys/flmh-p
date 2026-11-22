from __future__ import annotations

import ipaddress
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


SETTING_NAMES = {
    "environment": "APP_ENV", "agent_host": "AGENT_HTTP_HOST", "agent_port": "AGENT_HTTP_PORT",
    "agent_api_key": "AGENT_API_KEY",
    "agent_default_region": "AGENT_DEFAULT_REGION",
    "agent_default_language": "AGENT_DEFAULT_LANGUAGE",
    "agent_region_language_map_json": "AGENT_REGION_LANGUAGE_MAP_JSON",
    "agent_planner_temperature": "AGENT_PLANNER_TEMPERATURE",
    "agent_planner_max_tokens": "AGENT_PLANNER_MAX_TOKENS",
    "agent_max_queries_per_plan": "AGENT_MAX_QUERIES_PER_PLAN",
    "planner_backend": "PLANNER_BACKEND", "model_gateway_url": "MODEL_GATEWAY_URL",
    "model_gateway_api_key": "MODEL_GATEWAY_API_KEY", "mcp_tools_url": "MCP_TOOLS_URL",
    "gateway_url": "JAVA_GATEWAY_URL", "mcp_host": "MCP_HTTP_HOST", "mcp_port": "MCP_HTTP_PORT",
    "gateway_api_key": "JAVA_GATEWAY_API_KEY",
    "trusted_proxy_cidrs": "AGENT_TRUSTED_PROXY_CIDRS",
    "identity_service_url": "IDENTITY_SERVICE_URL",
    "checkpointer_backend": "CHECKPOINTER_BACKEND", "postgres_checkpointer_uri": "POSTGRES_CHECKPOINTER_URI",
    "postgres_checkpoint_setup": "POSTGRES_CHECKPOINT_SETUP",
    "langfuse_host": "LANGFUSE_HOST", "langfuse_public_key": "LANGFUSE_PUBLIC_KEY",
    "langfuse_secret_key": "LANGFUSE_SECRET_KEY", "embedding_backend": "EMBEDDING_BACKEND",
    "bge_m3_url": "BGE_M3_URL", "bge_m3_model": "BGE_M3_MODEL", "bge_m3_api_key": "BGE_M3_API_KEY",
    "embedding_dimension": "BGE_M3_DIMENSION", "bge_reranker_url": "BGE_RERANKER_URL",
    "bge_reranker_model": "BGE_RERANKER_MODEL", "bge_reranker_api_key": "BGE_RERANKER_API_KEY",
    "elasticsearch_url": "ELASTICSEARCH_URL",
    "elasticsearch_api_key": "ELASTICSEARCH_API_KEY", "elasticsearch_index": "ELASTICSEARCH_INDEX",
    "elasticsearch_dense_field": "ELASTICSEARCH_DENSE_FIELD", "elasticsearch_sparse_field": "ELASTICSEARCH_SPARSE_FIELD",
    "rag_top_k": "RAG_TOP_K",
    "rag_candidate_multiplier": "RAG_CANDIDATE_MULTIPLIER",
    "rag_rrf_k": "RAG_RRF_K",
    "rag_pit_keep_alive": "RAG_PIT_KEEP_ALIVE",
    "rag_local_corpus_fallback": "RAG_LOCAL_CORPUS_FALLBACK",
    "sql_dialect": "SQL_DIALECT",
    "bi_sql_role_tables_json": "BI_SQL_ROLE_TABLES_JSON", "bi_database_uri": "BI_DATABASE_URI",
    "sql_max_rows": "SQL_MAX_ROWS",
    "sql_timeout_ms": "SQL_TIMEOUT_MS",
    "sql_max_retries": "SQL_MAX_RETRIES",
    "sql_default_score_version": "SQL_DEFAULT_SCORE_VERSION",
    "request_timeout_seconds": "REQUEST_TIMEOUT_SECONDS",
}


class ConfigurationError(ValueError):
    pass


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    environment: Literal["local", "test", "production"] = "local"
    agent_host: str = "127.0.0.1"
    agent_port: int = Field(default=8090, ge=1, le=65535)
    agent_api_key: str = ""
    agent_default_region: str = Field(default="US", pattern=r"^[A-Z]{2}$")
    agent_default_language: str = Field(default="en-US", pattern=r"^[a-z]{2,3}(?:-[A-Z]{2})?$")
    agent_region_language_map_json: str = ""
    agent_planner_temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    agent_planner_max_tokens: int = Field(default=2048, ge=128, le=16384)
    agent_max_queries_per_plan: int = Field(default=6, ge=1, le=12)
    planner_backend: Literal["rules", "model_gateway"] = "rules"
    model_gateway_url: str = "http://127.0.0.1:8082"
    model_gateway_api_key: str = ""
    mcp_tools_url: str = "http://127.0.0.1:8001/mcp"
    gateway_url: str = "http://127.0.0.1:8080"
    identity_service_url: str = "http://127.0.0.1:8080"
    gateway_api_key: str = ""
    trusted_proxy_cidrs: tuple[str, ...] = ()
    mcp_host: str = "127.0.0.1"
    mcp_port: int = Field(default=8001, ge=1, le=65535)
    checkpointer_backend: Literal["memory", "postgres"] = "memory"
    postgres_checkpointer_uri: str = ""
    postgres_checkpoint_setup: bool = False
    langfuse_host: str = ""
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    embedding_backend: Literal["", "bge_m3_http"] = ""
    bge_m3_url: str = ""
    bge_m3_model: str = ""
    bge_m3_api_key: str = ""
    embedding_dimension: int | None = Field(default=None, ge=1)
    bge_reranker_url: str = ""
    bge_reranker_model: str = ""
    bge_reranker_api_key: str = ""
    elasticsearch_url: str = ""
    elasticsearch_api_key: str = ""
    elasticsearch_index: str = Field(default="", pattern=r"^([a-z0-9][a-z0-9_-]{0,79})?$")
    elasticsearch_dense_field: str = "dense_vector"
    elasticsearch_sparse_field: str = "sparse_vector"
    rag_top_k: int = Field(default=5, ge=1, le=20)
    rag_candidate_multiplier: int = Field(default=4, ge=1, le=10)
    rag_rrf_k: int = Field(default=60, ge=1, le=500)
    rag_pit_keep_alive: str = "1m"
    rag_local_corpus_fallback: bool = True
    sql_dialect: Literal["postgres", "mysql"] = "postgres"
    bi_sql_role_tables_json: str = ""
    bi_database_uri: str = ""
    sql_max_rows: int = Field(default=50, ge=1, le=50)
    sql_timeout_ms: int = Field(default=5000, ge=250, le=60000)
    sql_max_retries: int = Field(default=3, ge=1, le=5)
    sql_default_score_version: str = "v2"
    request_timeout_seconds: float = Field(default=15.0, gt=0, le=120)

    @field_validator("trusted_proxy_cidrs", mode="before")
    @classmethod
    def proxy_networks(cls, value: Any) -> tuple[str, ...]:
        values = value.split(",") if isinstance(value, str) and value.strip() else (() if isinstance(value, str) else value)
        networks = tuple(ipaddress.ip_network(item.strip(), strict=False) for item in values)
        if len(networks) > 64 or any(network.prefixlen == 0 for network in networks):
            raise ValueError("trusted proxies require at most 64 explicit CIDRs; all-address ranges are forbidden")
        return tuple(str(network) for network in networks)

    @field_validator("model_gateway_url", "mcp_tools_url", "gateway_url", "identity_service_url", mode="before")
    @classmethod
    def strip_urls(cls, value: str) -> str:
        return value.strip().rstrip("/")

    @field_validator("langfuse_host", "elasticsearch_url", "bge_m3_url", "bge_reranker_url", mode="before")
    @classmethod
    def strip_optional_urls(cls, value: str) -> str:
        return value.strip().rstrip("/")

    @field_validator("embedding_dimension", mode="before")
    @classmethod
    def optional_embedding_dimension(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        if isinstance(value, bool):
            raise ValueError("BGE_M3_DIMENSION must be a positive integer")
        return value

    @field_validator("bi_database_uri", mode="before")
    @classmethod
    def check_bi_uri(cls, value: Any) -> Any:
        if isinstance(value, str) and value.strip():
            stripped = value.strip()
            allowed_prefixes = ("postgres://", "postgresql://", "mysql://", "mysql+pymysql://", "mysql+aiomysql://")
            if not stripped.startswith(allowed_prefixes):
                raise ValueError("BI_DATABASE_URI must start with postgres://, postgresql://, or mysql://")
            return stripped
        return "" if isinstance(value, str) else value

    @field_validator("agent_region_language_map_json", mode="before")
    @classmethod
    def check_region_language_map(cls, value: Any) -> str:
        if value is None:
            return ""
        text = str(value).strip()
        if not text:
            return ""
        import json as _json
        import re as _re
        parsed = _json.loads(text)
        if not isinstance(parsed, dict):
            raise ValueError("AGENT_REGION_LANGUAGE_MAP_JSON must be a JSON object")
        for region, lang in parsed.items():
            if not isinstance(region, str) or not _re.fullmatch(r"[A-Z]{2}", region):
                raise ValueError("region keys in AGENT_REGION_LANGUAGE_MAP_JSON must be 2-letter uppercase ISO codes")
            if not isinstance(lang, str) or not _re.fullmatch(r"[a-z]{2,3}(?:-[A-Z]{2})?", lang):
                raise ValueError("language values in AGENT_REGION_LANGUAGE_MAP_JSON must be valid BCP-47 tags")
        return text

    @model_validator(mode="after")
    def validate_selected_adapters(self) -> Settings:
        if self.trusted_proxy_cidrs and not self.gateway_api_key:
            raise ValueError("JAVA_GATEWAY_API_KEY is required when AGENT_TRUSTED_PROXY_CIDRS is configured")
        if self.environment == "production" and not self.agent_api_key:
            raise ValueError("AGENT_API_KEY is required in production")
        if self.environment == "production" and self.planner_backend != "model_gateway":
            raise ValueError("PLANNER_BACKEND=model_gateway is required in production")
        if self.environment == "production" and self.checkpointer_backend != "postgres":
            raise ValueError("CHECKPOINTER_BACKEND=postgres is required in production")
        if self.mcp_host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("the MCP tool bridge must bind to loopback; expose it through an authenticated gateway")
        if self.checkpointer_backend == "postgres" and not self.postgres_checkpointer_uri:
            raise ValueError("POSTGRES_CHECKPOINTER_URI is required when CHECKPOINTER_BACKEND=postgres")
        if self.bi_sql_role_tables_json:
            if not self.bi_database_uri:
                raise ValueError("BI_DATABASE_URI is required when BI SQL access is configured")
        if self.embedding_backend == "bge_m3_http":
            required = {
                "BGE_M3_URL": self.bge_m3_url,
                "BGE_M3_MODEL": self.bge_m3_model,
                "BGE_M3_DIMENSION": self.embedding_dimension,
                "BGE_RERANKER_URL": self.bge_reranker_url,
                "BGE_RERANKER_MODEL": self.bge_reranker_model,
                "ELASTICSEARCH_URL": self.elasticsearch_url,
                "ELASTICSEARCH_INDEX": self.elasticsearch_index,
            }
            missing = [name for name, value in required.items() if not value]
            if missing:
                raise ValueError(f"BGE_M3 requires: {', '.join(missing)}")
        tracing = (self.langfuse_host, self.langfuse_public_key, self.langfuse_secret_key)
        if any(tracing) and not all(tracing):
            raise ValueError("LANGFUSE_HOST, LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY must be set together")
        return self

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        if environ is None:
            from flmh_agent.managed_settings import SettingsSource
            return SettingsSource().initial
        else:
            source = environ
        values = {field: source[name] for field, name in SETTING_NAMES.items() if name in source}
        try:
            return cls.model_validate(values)
        except ValidationError as error:
            raise ConfigurationError(str(error)) from error

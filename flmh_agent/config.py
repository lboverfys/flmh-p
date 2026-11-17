from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal, Mapping

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


class ConfigurationError(ValueError):
    pass


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    environment: Literal["local", "test", "production"] = "local"
    agent_host: str = "127.0.0.1"
    agent_port: int = Field(default=8090, ge=1, le=65535)
    agent_api_key: str = ""
    planner_backend: Literal["rules", "model_gateway"] = "rules"
    model_gateway_url: str = "http://127.0.0.1:8082"
    model_gateway_api_key: str = ""
    mcp_tools_url: str = "http://127.0.0.1:8001/mcp"
    gateway_url: str = "http://127.0.0.1:8080"
    identity_service_url: str = "http://127.0.0.1:8085"
    gateway_api_key: str = ""
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
    sql_dialect: str = "postgres"
    bi_sql_role_api_keys_json: str = ""
    bi_sql_role_tables_json: str = ""
    bi_database_uri: str = ""
    sql_max_rows: int = Field(default=50, ge=1, le=50)
    request_timeout_seconds: float = Field(default=15.0, gt=0, le=120)

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

    @model_validator(mode="after")
    def validate_selected_adapters(self) -> Settings:
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
        if self.bi_sql_role_api_keys_json or self.bi_sql_role_tables_json:
            if not self.bi_database_uri:
                raise ValueError("BI_DATABASE_URI is required when BI SQL access is configured")
            if not self.bi_sql_role_api_keys_json or not self.bi_sql_role_tables_json:
                raise ValueError("BI_SQL_ROLE_API_KEYS_JSON and BI_SQL_ROLE_TABLES_JSON must be configured together")
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
            env_file = Path(os.environ.get("FLMH_ENV_FILE", ".env"))
            source = {**({key: value for key, value in dotenv_values(env_file).items() if value is not None} if env_file.is_file() else {}), **os.environ}
        else:
            source = environ
        mapping = {
            "environment": "APP_ENV", "agent_host": "AGENT_HTTP_HOST", "agent_port": "AGENT_HTTP_PORT",
            "agent_api_key": "AGENT_API_KEY",
            "planner_backend": "PLANNER_BACKEND", "model_gateway_url": "MODEL_GATEWAY_URL",
            "model_gateway_api_key": "MODEL_GATEWAY_API_KEY", "mcp_tools_url": "MCP_TOOLS_URL",
            "gateway_url": "JAVA_GATEWAY_URL", "mcp_host": "MCP_HTTP_HOST", "mcp_port": "MCP_HTTP_PORT",
            "gateway_api_key": "JAVA_GATEWAY_API_KEY",
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
            "sql_dialect": "SQL_DIALECT", "bi_sql_role_api_keys_json": "BI_SQL_ROLE_API_KEYS_JSON",
            "bi_sql_role_tables_json": "BI_SQL_ROLE_TABLES_JSON", "bi_database_uri": "BI_DATABASE_URI",
            "sql_max_rows": "SQL_MAX_ROWS", "request_timeout_seconds": "REQUEST_TIMEOUT_SECONDS",
        }
        values = {field: source[name] for field, name in mapping.items() if name in source}
        try:
            return cls.model_validate(values)
        except ValidationError as error:
            raise ConfigurationError(str(error)) from error

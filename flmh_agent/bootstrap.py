from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator

import httpx

from flmh_agent.adapters.outbound.bge_http import BgeM3HttpAdapter, BgeRerankerHttpAdapter, ElasticsearchHybridStore
from flmh_agent.adapters.outbound.checkpoint import open_checkpointer
from flmh_agent.adapters.outbound.coordination import open_run_coordinator
from flmh_agent.adapters.outbound.identity import JavaIdentityVerifier
from flmh_agent.adapters.outbound.mcp_client import McpProductContentAdapter
from flmh_agent.adapters.outbound.model_gateway import ModelGatewayClient, ModelGatewayPlanner, ModelGatewaySqlGenerator
from flmh_agent.adapters.outbound.observability import LangfuseCallbackFactory
from flmh_agent.adapters.outbound.postgres_sql import AsyncPostgresReadOnlyRunner
from flmh_agent.adapters.outbound.sql_permissions import ConfiguredSqlPermissionPolicy
from flmh_agent.application.service import AgentApplication
from flmh_agent.application.rag import HybridKnowledgeService
from flmh_agent.application.text2sql import TextToSqlService
from flmh_agent.config import Settings
from flmh_agent.domain.planning import RuleBasedPlanner


@dataclass(frozen=True)
class Runtime:
    agent: AgentApplication
    text_to_sql: TextToSqlService | None
    sql_permissions: ConfiguredSqlPermissionPolicy | None
    rag: dict[str, Any] | None
    identity_verifier: JavaIdentityVerifier


def runtime_loop_factory(settings: Settings) -> Callable[[], asyncio.AbstractEventLoop] | None:
    if sys.platform == "win32" and settings.checkpointer_backend == "postgres":
        return asyncio.SelectorEventLoop
    return None


@asynccontextmanager
async def open_runtime(settings: Settings) -> AsyncIterator[Runtime]:
    async with httpx.AsyncClient(
        base_url=settings.model_gateway_url,
        timeout=settings.request_timeout_seconds,
        trust_env=False,
    ) as model_http, httpx.AsyncClient(
        base_url=settings.identity_service_url, timeout=settings.request_timeout_seconds, trust_env=False,
    ) as identity_http:
        async with open_checkpointer(settings) as checkpointer, open_run_coordinator(settings) as coordinator:
            model_gateway = ModelGatewayClient(settings, client=model_http)
            planner = ModelGatewayPlanner(model_gateway) if settings.planner_backend == "model_gateway" else RuleBasedPlanner()
            runner = AsyncPostgresReadOnlyRunner(settings) if settings.bi_database_uri else None
            sql_permissions = ConfiguredSqlPermissionPolicy(settings) if runner is not None else None
            text_to_sql = TextToSqlService(
                permissions=sql_permissions,
                model=ModelGatewaySqlGenerator(model_gateway),
                runner=runner,
                dialect=settings.sql_dialect,
                max_rows=settings.sql_max_rows,
            ) if runner is not None else None
            rag = {
                "embedder": BgeM3HttpAdapter(settings),
                "store": ElasticsearchHybridStore(settings),
                "reranker": BgeRerankerHttpAdapter(settings),
            } if settings.embedding_backend == "bge_m3_http" else None
            trace_factory = LangfuseCallbackFactory(settings)
            agent = AgentApplication(
                settings=settings,
                planner=planner,
                checkpointer=checkpointer,
                tool_session_factory=lambda: McpProductContentAdapter(settings),
                trace_factory=trace_factory,
                run_coordinator=coordinator,
                bi_query=text_to_sql,
                knowledge_query=HybridKnowledgeService(**rag) if rag else None,
            )
            try:
                yield Runtime(agent, text_to_sql, sql_permissions, rag, JavaIdentityVerifier(settings, identity_http))
            finally:
                if runner is not None:
                    await runner.close()
                trace_factory.close()

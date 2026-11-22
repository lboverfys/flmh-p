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
from flmh_agent.adapters.outbound.mysql_sql import AsyncMysqlReadOnlyRunner
from flmh_agent.adapters.outbound.postgres_sql import AsyncPostgresReadOnlyRunner
from flmh_agent.adapters.outbound.sql_permissions import ConfiguredSqlPermissionPolicy
from flmh_agent.adapters.outbound.sessions import open_session_directory
from flmh_agent.application.service import AgentApplication
from flmh_agent.application.rag import HybridKnowledgeService, LocalCorpusFallbackKnowledgeService
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
    knowledge_service: Any | None = None


def runtime_loop_factory(settings: Settings) -> Callable[[], asyncio.AbstractEventLoop] | None:
    if sys.platform == "win32" and settings.checkpointer_backend == "postgres":
        return asyncio.SelectorEventLoop
    return None


@asynccontextmanager
async def open_session_state(settings: Settings):
    async with open_checkpointer(settings) as checkpointer, open_run_coordinator(settings) as coordinator, \
            open_session_directory(settings) as directory:
        yield checkpointer, coordinator, directory


@asynccontextmanager
async def open_runtime(settings: Settings, session_state=None) -> AsyncIterator[Runtime]:
    if session_state is None:
        async with open_session_state(settings) as storage, open_runtime(settings, storage) as runtime:
            yield runtime
        return
    checkpointer, coordinator, session_directory = session_state
    async with httpx.AsyncClient(
        base_url=settings.model_gateway_url,
        timeout=settings.request_timeout_seconds,
        trust_env=False,
    ) as model_http, httpx.AsyncClient(
        base_url=settings.identity_service_url, timeout=settings.request_timeout_seconds, trust_env=False,
    ) as identity_http:
        model_gateway = ModelGatewayClient(settings, client=model_http)
        planner = (
            ModelGatewayPlanner(model_gateway)
            if settings.planner_backend == "model_gateway"
            else RuleBasedPlanner(
                default_region=settings.agent_default_region,
                max_queries=settings.agent_max_queries_per_plan,
            )
        )
        if settings.bi_database_uri:
            runner = (
                AsyncMysqlReadOnlyRunner(settings)
                if settings.bi_database_uri.casefold().startswith("mysql") or settings.sql_dialect == "mysql"
                else AsyncPostgresReadOnlyRunner(settings)
            )
        else:
            runner = None
        sql_permissions = ConfiguredSqlPermissionPolicy(settings) if runner is not None else None
        text_to_sql = TextToSqlService(
            permissions=sql_permissions,
            model=ModelGatewaySqlGenerator(model_gateway),
            runner=runner,
            dialect=settings.sql_dialect,
            max_rows=settings.sql_max_rows,
            timeout_ms=settings.sql_timeout_ms,
            max_retries=settings.sql_max_retries,
            default_score_version=settings.sql_default_score_version,
        ) if runner is not None else None
        rag = {
            "embedder": BgeM3HttpAdapter(settings),
            "store": ElasticsearchHybridStore(settings),
            "reranker": BgeRerankerHttpAdapter(settings),
        } if settings.embedding_backend == "bge_m3_http" else None
        knowledge_service = (
            HybridKnowledgeService(
                **rag,
                limit=settings.rag_top_k,
                candidate_multiplier=settings.rag_candidate_multiplier,
                rank_constant=settings.rag_rrf_k,
            )
            if rag
            else LocalCorpusFallbackKnowledgeService(
                limit=settings.rag_top_k,
                candidate_multiplier=settings.rag_candidate_multiplier,
                rank_constant=settings.rag_rrf_k,
            )
            if settings.rag_local_corpus_fallback
            else None
        )
        trace_factory = LangfuseCallbackFactory(settings)
        agent = AgentApplication(
            settings=settings,
            planner=planner,
            checkpointer=checkpointer,
            tool_session_factory=lambda: McpProductContentAdapter(settings),
            trace_factory=trace_factory,
            run_coordinator=coordinator,
            bi_query=text_to_sql,
            knowledge_query=knowledge_service,
            session_directory=session_directory,
        )
        try:
            yield Runtime(agent, text_to_sql, sql_permissions, rag, JavaIdentityVerifier(settings, identity_http), knowledge_service=knowledge_service)
        finally:
            if runner is not None:
                await runner.close()
            trace_factory.close()

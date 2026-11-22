from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from flmh_agent.application.service import RunConflictError, RunNotFoundError
from flmh_agent.application.rag import hybrid_retrieve
from flmh_agent.application.text2sql import SqlValidationError, validate_readonly_sql
from flmh_agent.adapters.outbound.sql_permissions import ConfiguredSqlPermissionPolicy
from flmh_agent.bootstrap import open_runtime, open_session_state, runtime_loop_factory
from flmh_agent.live_app import ManagedApp
from flmh_agent.config import Settings
from flmh_agent.domain.models import AgentStartRequest, ConfirmationInput, TextToSqlInput
from flmh_agent.domain.identity import AuthenticationError, IdentityUnavailableError, IdentityRateLimitedError
from flmh_agent.adapters.inbound.client_ip import client_address
from flmh_agent.adapters.inbound.recovery import recovery_routes
from flmh_agent.adapters.inbound.identity import IdentityMiddleware
from flmh_agent.adapters.inbound.accounts import account_routes
from flmh_agent.adapters.outbound.accounts import JavaUserAccounts
from flmh_agent.adapters.inbound.works import work_routes
from flmh_agent.adapters.inbound.sessions import session_routes
from flmh_agent.adapters.outbound.works import JavaWorkCatalog
from flmh_agent.adapters.inbound.workflows import workflow_routes
from flmh_agent.adapters.outbound.workflows import JavaWorkflowClient
from flmh_agent.adapters.inbound.products import product_routes
from flmh_agent.adapters.outbound.products import JavaProductBrowser
from flmh_agent.adapters.inbound.assets import asset_routes
from flmh_agent.adapters.outbound.assets import JavaMediaAssets
from flmh_agent.ports.identity import IdentityLoginPort, IdentityVerifierPort, IdentityRecoveryPort, UserAccountsPort


logger = logging.getLogger("flmh.agent.api")
MAX_JSON_BODY_BYTES = 64 * 1024


class LoginInput(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    email: str = Field(min_length=1, max_length=320)
    password: str = Field(min_length=1, max_length=1024, repr=False)


def create_app(settings: Settings | None = None, *, identity_verifier: IdentityVerifierPort | None = None,
               identity_login: IdentityLoginPort | None = None, accounts: UserAccountsPort | None = None,
               works: JavaWorkCatalog | None = None, workflows: JavaWorkflowClient | None = None,
               products: JavaProductBrowser | None = None, assets: JavaMediaAssets | None = None,
               identity_recovery: IdentityRecoveryPort | None = None, session_state=None) -> Starlette:
    config = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(application: Starlette):
        async with open_runtime(config, session_state) as runtime:
            application.state.agent = runtime.agent
            application.state.text_to_sql = runtime.text_to_sql
            application.state.sql_permissions = runtime.sql_permissions
            application.state.rag = runtime.rag
            application.state.knowledge_service = runtime.knowledge_service
            application.state.identity_verifier = identity_verifier or runtime.identity_verifier
            application.state.identity_login = identity_login or runtime.identity_verifier
            application.state.identity_recovery = identity_recovery or runtime.identity_verifier
            application.state.accounts = accounts or JavaUserAccounts(config)
            application.state.works = works or JavaWorkCatalog(config)
            application.state.workflows = workflows or JavaWorkflowClient(config)
            application.state.products = products or JavaProductBrowser(config)
            application.state.assets = assets or JavaMediaAssets(config)
            yield

    async def live(_request: Request) -> Response:
        return JSONResponse({"status": "alive"})

    async def ready(request: Request) -> Response:
        current: Settings = request.app.state.settings
        return JSONResponse({
            "status": "process_ready",
            "environment": current.environment,
            "adapters": {
                "planner": current.planner_backend,
                "contentGenerator": "java_content_service",
                "checkpointer": current.checkpointer_backend,
                "rag": (
                    "configured"
                    if current.embedding_backend
                    else "local_corpus_fallback"
                    if current.rag_local_corpus_fallback
                    else "not_configured"
                ),
                "tracing": "configured" if current.langfuse_host else "not_configured",
                "identity": "java_user_verification",
            },
        })

    async def login(request: Request) -> Response:
        headers = {"Cache-Control": "no-store"}
        try:
            payload = LoginInput.model_validate(await _json_body(request))
            result = await request.app.state.identity_login.login(payload.email.strip(), payload.password, client_ip=client_address(request, config))
        except ValidationError as error:
            return _validation_response(error)
        except ValueError as error:
            return JSONResponse({"error": "invalid_login_input"}, status_code=413 if "exceeds" in str(error) else 400, headers=headers)
        except AuthenticationError:
            return JSONResponse({"error": "invalid_credentials_or_inactive_user"}, status_code=401, headers=headers)
        except IdentityRateLimitedError:
            return JSONResponse({"error": "too_many_authentication_attempts"}, status_code=429, headers=headers)
        except IdentityUnavailableError:
            return JSONResponse({"error": "identity_service_unavailable"}, status_code=503, headers=headers)
        return JSONResponse(result, headers=headers)

    async def current_user(request: Request) -> Response:
        identity = request.state.identity
        return JSONResponse({"id": identity.user_id, "role": identity.role, "status": "ACTIVE", "source": identity.source},
                            headers={"Cache-Control": "no-store"})

    async def start_run(request: Request) -> Response:
        try:
            body = await _json_body(request)
            payload = AgentStartRequest.model_validate(body)
            run_id = payload.run_id or __import__("uuid").uuid4().hex
            result = await request.app.state.agent.start(
                run_id,
                payload.model_dump(exclude={"run_id"}),
                identity=request.state.identity,
            )
        except ValidationError as error:
            return _validation_response(error)
        except ValueError as error:
            status = 413 if "exceeds" in str(error) else 400 if "valid JSON" in str(error) or "JSON object" in str(error) else 422
            return JSONResponse({"error": str(error)}, status_code=status)
        except RunConflictError as error:
            return JSONResponse({"error": str(error)}, status_code=409)
        except RunNotFoundError as error:
            return JSONResponse({"error": str(error)}, status_code=404)
        except Exception as error:
            logger.exception("Agent run failed")
            return JSONResponse({"error": "agent_run_failed"}, status_code=502)
        status_code = 202 if result["status"] == "awaiting_confirmation" else 200
        return JSONResponse(result, status_code=status_code)

    async def confirm_run(request: Request) -> Response:
        try:
            body = await _json_body(request)
            ConfirmationInput.model_validate(body)
            result = await request.app.state.agent.confirm(
                request.path_params["run_id"], body, identity=request.state.identity,
            )
        except ValidationError as error:
            return _validation_response(error)
        except ValueError as error:
            status = 413 if "exceeds" in str(error) else 400 if "valid JSON" in str(error) or "JSON object" in str(error) else 422
            return JSONResponse({"error": str(error)}, status_code=status)
        except RunNotFoundError as error:
            return JSONResponse({"error": str(error)}, status_code=404)
        except RunConflictError as error:
            return JSONResponse({"error": str(error)}, status_code=409)
        except Exception as error:
            logger.exception("Agent confirmation failed")
            return JSONResponse({"error": "confirmation_failed"}, status_code=502)
        return JSONResponse(result, status_code=200)

    async def get_run(request: Request) -> Response:
        try:
            result = await request.app.state.agent.get(request.path_params["run_id"], identity=request.state.identity)
        except ValueError as error:
            return JSONResponse({"error": str(error)}, status_code=422)
        except RunNotFoundError as error:
            return JSONResponse({"error": str(error)}, status_code=404)
        except Exception:
            logger.exception("Agent status refresh failed")
            return JSONResponse({"error": "status_refresh_failed"}, status_code=502)
        if result is None:
            return JSONResponse({"error": "run_not_found"}, status_code=404)
        return JSONResponse(result)

    async def retry_run(request: Request) -> Response:
        try:
            result = await request.app.state.agent.retry(request.path_params["run_id"], identity=request.state.identity)
        except RunNotFoundError as error:
            return JSONResponse({"error": str(error)}, status_code=404)
        except RunConflictError as error:
            return JSONResponse({"error": str(error)}, status_code=409)
        except ValueError as error:
            return JSONResponse({"error": str(error)}, status_code=422)
        except Exception as error:
            logger.exception("Agent retry failed")
            return JSONResponse({"error": "retry_failed"}, status_code=502)
        return JSONResponse(result)

    async def revise_run(request: Request) -> Response:
        try:
            result = await request.app.state.agent.revise(request.path_params["run_id"], await _json_body(request), identity=request.state.identity)
        except ValidationError as error:
            return _validation_response(error)
        except RunNotFoundError as error:
            return JSONResponse({"error": str(error)}, status_code=404)
        except RunConflictError as error:
            return JSONResponse({"error": str(error)}, status_code=409)
        except ValueError as error:
            return JSONResponse({"error": str(error)}, status_code=422)
        except Exception:
            logger.exception("Agent revision failed")
            return JSONResponse({"error": "revision_failed"}, status_code=502)
        return JSONResponse(result, status_code=202 if result["status"] == "awaiting_confirmation" else 200)

    async def handoff_media_run(request: Request) -> Response:
        try:
            body = await _json_body(request)
            result = await request.app.state.agent.handoff_media(
                request.path_params["run_id"], body, identity=request.state.identity,
            )
        except RunNotFoundError as error:
            return JSONResponse({"error": str(error)}, status_code=404)
        except RunConflictError as error:
            return JSONResponse({"error": str(error)}, status_code=409)
        except ValueError as error:
            status = 413 if "exceeds" in str(error) else 400 if "valid JSON" in str(error) or "JSON object" in str(error) else 422
            return JSONResponse({"error": str(error)}, status_code=status)
        except Exception:
            logger.exception("Media handoff failed")
            return JSONResponse({"error": "media_handoff_failed"}, status_code=502)
        return JSONResponse(result, status_code=202 if result["status"] == "awaiting_confirmation" else 200)

    async def validate_bi_sql(request: Request) -> Response:
        permissions = request.app.state.sql_permissions
        if permissions is None:
            if not config.bi_sql_role_tables_json.strip():
                return JSONResponse({"error": "bi_sql_disabled"}, status_code=503)
            permissions = ConfiguredSqlPermissionPolicy(config)
        role = request.state.identity.role
        try:
            body = await _json_body(request)
            sql = body.get("sql")
            if not isinstance(sql, str) or not sql.strip():
                raise ValueError("sql must be a non-empty string")
            allow_cmp = bool(body.get("allow_score_version_comparison", False))
        except ValueError as error:
            status = 413 if "exceeds" in str(error) else 400
            return JSONResponse({"error": str(error)}, status_code=status)
        allowed = sorted(await permissions.allowed_tables(role))
        if not allowed:
            return JSONResponse({"error": "principal has no BI table permissions"}, status_code=403)
        try:
            normalized = validate_readonly_sql(
                sql,
                dialect=config.sql_dialect,
                allowed_tables=allowed,
                max_rows=config.sql_max_rows,
                allow_score_version_comparison=allow_cmp,
                default_score_version=config.sql_default_score_version,
            )
        except SqlValidationError as error:
            return JSONResponse({"valid": False, "error": str(error), "dialect": config.sql_dialect, "allowedTables": allowed}, status_code=422)
        return JSONResponse({"valid": True, "normalizedSql": normalized, "dialect": config.sql_dialect, "allowedTables": allowed})

    async def query_bi(request: Request) -> Response:
        permissions = request.app.state.sql_permissions
        if permissions is None:
            return JSONResponse({"error": "bi_sql_disabled"}, status_code=503)
        role = request.state.identity.role
        try:
            payload = TextToSqlInput.model_validate(await _json_body(request))
        except ValidationError as error:
            return _validation_response(error)
        except ValueError as error:
            status = 413 if "exceeds" in str(error) else 400
            return JSONResponse({"error": str(error)}, status_code=status)
        service = request.app.state.text_to_sql
        if service is None:
            return JSONResponse({"error": "bi_sql_not_ready"}, status_code=503)
        try:
            result = await service.query(payload.question, role)
        except PermissionError as error:
            return JSONResponse({"error": str(error)}, status_code=403)
        except Exception as error:
            logger.exception("BI SQL request failed")
            return JSONResponse({"error": "bi_sql_failed"}, status_code=502)
        return JSONResponse(result)

    async def advance_media(request: Request) -> Response:
        try:
            result = await request.app.state.agent.advance_media(
                request.path_params["run_id"], verify=request.path_params["action"] == "verify",
                identity=request.state.identity,
            )
        except RunNotFoundError as error:
            return JSONResponse({"error": str(error)}, status_code=404)
        except RunConflictError as error:
            return JSONResponse({"error": str(error)}, status_code=409)
        except ValueError as error:
            return JSONResponse({"error": str(error)}, status_code=422)
        except Exception:
            logger.exception("Media production step failed")
            return JSONResponse({"error": "media_step_failed"}, status_code=502)
        return JSONResponse(result)

    async def media_action(request: Request) -> Response:
        if request.path_params["action"] not in {"advance", "verify"}:
            return JSONResponse({"error": "not_found"}, status_code=404)
        return await advance_media(request)

    async def rag_search(request: Request) -> Response:
        rag = request.app.state.rag
        knowledge_service = getattr(request.app.state, "knowledge_service", None)
        if rag is None and knowledge_service is None:
            return JSONResponse({"error": "rag_not_configured"}, status_code=503)
        try:
            body = await _json_body(request)
            question = body.get("query")
            limit = body.get("limit", config.rag_top_k)
            if not isinstance(question, str) or not question.strip():
                raise ValueError("query must be a non-empty string")
            if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
                raise ValueError("limit must be an integer from 1 to 20")
            if rag is not None:
                chunks = await hybrid_retrieve(
                    question,
                    rag["embedder"],
                    rag["store"],
                    rag["reranker"],
                    limit=limit,
                    candidate_multiplier=config.rag_candidate_multiplier,
                    rank_constant=config.rag_rrf_k,
                )
                source_label = "BGE_M3_ELASTICSEARCH"
            else:
                chunks = knowledge_service.search_chunks(question, limit=limit)
                source_label = "LOCAL_CORPUS_FALLBACK"
        except ValueError as error:
            return JSONResponse({"error": str(error)}, status_code=400)
        except Exception:
            logger.exception("RAG query failed")
            return JSONResponse({"error": "rag_query_failed"}, status_code=502)
        return JSONResponse({"source": source_label, "chunks": [
            {"id": chunk.chunk_id, "parentId": chunk.parent_id, "text": chunk.text, "score": chunk.score,
             "sourceRef": chunk.source_ref, "documentId": chunk.document_id, "version": chunk.version}
            for chunk in chunks
        ]})

    routes = [
        Route("/health/live", live, methods=["GET"]),
        Route("/health/ready", ready, methods=["GET"]),
        Route("/v1/identity/login", login, methods=["POST"]),
        Route("/v1/identity/me", current_user, methods=["GET"]),
        Route("/v1/agent/runs", start_run, methods=["POST"]),
        Route("/v1/agent/runs/{run_id:str}", get_run, methods=["GET"]),
        Route("/v1/agent/runs/{run_id:str}/confirmation", confirm_run, methods=["POST"]),
        Route("/v1/agent/runs/{run_id:str}/retry", retry_run, methods=["POST"]),
        Route("/v1/agent/runs/{run_id:str}/revision", revise_run, methods=["POST"]),
        Route("/v1/agent/runs/{run_id:str}/handoff-media", handoff_media_run, methods=["POST"]),
        Route("/v1/agent/runs/{run_id:str}/media/{action:str}", media_action, methods=["POST"]),
        Route("/v1/bi/queries", query_bi, methods=["POST"]),
        Route("/v1/bi/validate", validate_bi_sql, methods=["POST"]),
        Route("/v1/rag/search", rag_search, methods=["POST"]),
    ]
    routes.extend(account_routes(_json_body))
    routes.extend(work_routes(_json_body))
    routes.extend(session_routes(_json_body))
    routes.extend(workflow_routes(_json_body))
    routes.extend(product_routes())
    routes.extend(asset_routes())
    routes.extend(recovery_routes(_json_body))
    public_paths = frozenset({"/health/live", "/health/ready"})
    app = Starlette(routes=routes, lifespan=lifespan, middleware=[
        Middleware(IdentityMiddleware, api_key=config.agent_api_key, public_paths=public_paths,
                   anonymous_routes=frozenset({("POST", "/v1/identity/login"), ("POST", "/v1/identity/forgot-password"),
                                               ("POST", "/v1/identity/reset-password")})),
    ])
    app.state.settings = config
    return app


async def _json_body(request: Request) -> dict[str, Any]:
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > MAX_JSON_BODY_BYTES:
        raise ValueError("request body exceeds the configured limit")
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > MAX_JSON_BODY_BYTES:
            raise ValueError("request body exceeds the configured limit")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError("request body must be valid JSON") from error
    if not isinstance(value, dict):
        raise ValueError("request body must be a JSON object")
    return value


def _validation_response(error: ValidationError) -> JSONResponse:
    details = [
        {"path": list(item["loc"]), "message": item["msg"], "type": item["type"]}
        for item in error.errors()
    ]
    return JSONResponse({"error": "validation_failed", "details": details}, status_code=422)


app = ManagedApp(lambda settings, session: create_app(settings, session_state=session), storage=open_session_state)


def main() -> None:
    settings = Settings.from_env()
    server = uvicorn.Server(uvicorn.Config(app, host=settings.agent_host, port=settings.agent_port, access_log=True, proxy_headers=False))
    asyncio.run(server.serve(), loop_factory=runtime_loop_factory(settings))


if __name__ == "__main__":
    main()

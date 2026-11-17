from __future__ import annotations

import asyncio
import hmac
import json
import logging
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from flmh_agent.application.service import RunConflictError, RunNotFoundError
from flmh_agent.application.rag import hybrid_retrieve
from flmh_agent.bootstrap import open_runtime, runtime_loop_factory
from flmh_agent.config import Settings
from flmh_agent.domain.models import AgentStartRequest, ConfirmationInput, TextToSqlInput


logger = logging.getLogger("flmh.agent.api")
MAX_JSON_BODY_BYTES = 64 * 1024


class ApiKeyMiddleware:
    def __init__(self, app: Any, *, api_key: str) -> None:
        self.app = app
        self.api_key = api_key

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or not self.api_key or path.startswith("/health/") or path == "/v1/bi/queries":
            await self.app(scope, receive, send)
            return
        headers = {key.decode("latin1").lower(): value.decode("latin1") for key, value in scope.get("headers", [])}
        expected = f"Bearer {self.api_key}"
        if not hmac.compare_digest(headers.get("authorization", ""), expected):
            response = JSONResponse({"error": "unauthorized"}, status_code=401)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def create_app(settings: Settings | None = None) -> Starlette:
    config = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(application: Starlette):
        async with open_runtime(config) as runtime:
            application.state.agent = runtime.agent
            application.state.text_to_sql = runtime.text_to_sql
            application.state.sql_permissions = runtime.sql_permissions
            application.state.rag = runtime.rag
            yield

    async def live(_request: Request) -> Response:
        return JSONResponse({"status": "alive"})

    async def ready(request: Request) -> Response:
        current: Settings = request.app.state.settings
        return JSONResponse({
            "status": "process_ready",
            "adapters": {
                "planner": current.planner_backend,
                "contentGenerator": "java_content_service",
                "checkpointer": current.checkpointer_backend,
                "rag": "configured" if current.embedding_backend else "not_configured",
                "tracing": "configured" if current.langfuse_host else "not_configured",
            },
        })

    async def start_run(request: Request) -> Response:
        try:
            body = await _json_body(request)
            payload = AgentStartRequest.model_validate(body)
            run_id = payload.run_id or __import__("uuid").uuid4().hex
            result = await request.app.state.agent.start(
                run_id,
                payload.model_dump(exclude={"run_id"}),
            )
        except ValidationError as error:
            return _validation_response(error)
        except ValueError as error:
            status = 413 if "exceeds" in str(error) else 400 if "valid JSON" in str(error) or "JSON object" in str(error) else 422
            return JSONResponse({"error": str(error)}, status_code=status)
        except RunConflictError as error:
            return JSONResponse({"error": str(error)}, status_code=409)
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
                request.path_params["run_id"], body
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
            result = await request.app.state.agent.get(request.path_params["run_id"])
        except ValueError as error:
            return JSONResponse({"error": str(error)}, status_code=422)
        except Exception:
            logger.exception("Agent status refresh failed")
            return JSONResponse({"error": "status_refresh_failed"}, status_code=502)
        if result is None:
            return JSONResponse({"error": "run_not_found"}, status_code=404)
        return JSONResponse(result)

    async def retry_run(request: Request) -> Response:
        try:
            result = await request.app.state.agent.retry(request.path_params["run_id"])
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
            result = await request.app.state.agent.revise(request.path_params["run_id"], await _json_body(request))
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

    async def query_bi(request: Request) -> Response:
        permissions = request.app.state.sql_permissions
        if permissions is None:
            return JSONResponse({"error": "bi_sql_disabled"}, status_code=503)
        presented = request.headers.get("authorization", "")
        role = permissions.authenticate(presented)
        if role is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
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

    async def rag_search(request: Request) -> Response:
        rag = request.app.state.rag
        if rag is None:
            return JSONResponse({"error": "rag_not_configured"}, status_code=503)
        try:
            body = await _json_body(request)
            question = body.get("query")
            limit = body.get("limit", 5)
            if not isinstance(question, str) or not question.strip():
                raise ValueError("query must be a non-empty string")
            if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
                raise ValueError("limit must be an integer from 1 to 20")
            chunks = await hybrid_retrieve(
                question,
                rag["embedder"],
                rag["store"],
                rag["reranker"],
                limit=limit,
            )
        except ValueError as error:
            return JSONResponse({"error": str(error)}, status_code=400)
        except Exception as error:
            logger.exception("RAG query failed")
            return JSONResponse({"error": "rag_query_failed"}, status_code=502)
        return JSONResponse({"source": "BGE_M3_ELASTICSEARCH", "chunks": [
            {"id": chunk.chunk_id, "parentId": chunk.parent_id, "text": chunk.text, "score": chunk.score,
             "sourceRef": chunk.source_ref, "documentId": chunk.document_id, "version": chunk.version}
            for chunk in chunks
        ]})

    routes = [
        Route("/health/live", live, methods=["GET"]),
        Route("/health/ready", ready, methods=["GET"]),
        Route("/v1/agent/runs", start_run, methods=["POST"]),
        Route("/v1/agent/runs/{run_id:str}", get_run, methods=["GET"]),
        Route("/v1/agent/runs/{run_id:str}/confirmation", confirm_run, methods=["POST"]),
        Route("/v1/agent/runs/{run_id:str}/retry", retry_run, methods=["POST"]),
        Route("/v1/agent/runs/{run_id:str}/revision", revise_run, methods=["POST"]),
        Route("/v1/bi/queries", query_bi, methods=["POST"]),
        Route("/v1/rag/search", rag_search, methods=["POST"]),
    ]
    app = Starlette(routes=routes, lifespan=lifespan, middleware=[Middleware(ApiKeyMiddleware, api_key=config.agent_api_key)])
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


app = create_app()


def main() -> None:
    settings = Settings.from_env()
    server = uvicorn.Server(uvicorn.Config(app, host=settings.agent_host, port=settings.agent_port, access_log=True))
    asyncio.run(server.serve(), loop_factory=runtime_loop_factory(settings))


if __name__ == "__main__":
    main()

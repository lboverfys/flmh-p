from __future__ import annotations

import asyncio
import hashlib
import json
import re

from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from flmh_agent.application.service import RunNotFoundError
from flmh_agent.domain.identity import AuthenticationError, IdentityUnavailableError
from flmh_agent.domain.sessions import SessionVersionConflict


SSE_POLL_SECONDS = 2.0
SSE_HEARTBEAT_SECONDS = 10.0
SSE_MAX_SECONDS = 60.0
_HEADERS = {"Cache-Control": "no-store"}


class SessionQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    limit: int = Field(default=20, ge=1, le=50)
    cursor: str | None = Field(default=None, min_length=1, max_length=512)


class RenameSession(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    title: str = Field(min_length=1, max_length=80)
    expectedVersion: StrictInt = Field(ge=1, le=9223372036854775807)


class DeleteSession(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    expectedVersion: int = Field(ge=1, le=9223372036854775807)


def session_routes(read_body) -> list[Route]:
    async def endpoint(request: Request):
        agent = request.app.state.agent
        identity = request.state.identity
        run_id = request.path_params.get("run_id")
        try:
            if len(request.query_params) != len(request.query_params.multi_items()):
                raise ValueError("duplicate query parameters")
            if run_id is None:
                query = SessionQuery.model_validate(dict(request.query_params))
                result = await agent.list_sessions(limit=query.limit, cursor=query.cursor, identity=identity)
            elif request.method == "DELETE":
                query = DeleteSession.model_validate(dict(request.query_params))
                result = await agent.delete_session(run_id, query.expectedVersion, identity=identity)
            else:
                if request.query_params:
                    raise ValueError("unexpected query parameters")
                if request.method == "GET":
                    result = await agent.get_session(run_id, identity=identity)
                else:
                    payload = RenameSession.model_validate(await read_body(request))
                    result = await agent.rename_session(run_id, payload.title, payload.expectedVersion, identity=identity)
            return JSONResponse(result, headers=_HEADERS)
        except RunNotFoundError:
            return JSONResponse({"error": "session_not_found"}, status_code=404, headers=_HEADERS)
        except SessionVersionConflict:
            return JSONResponse({"error": "session_version_conflict"}, status_code=409, headers=_HEADERS)
        except ValidationError:
            return JSONResponse({"error": "invalid_session_input"}, status_code=422, headers=_HEADERS)
        except ValueError as error:
            return JSONResponse({"error": "invalid_session_input"},
                                status_code=413 if "exceeds" in str(error) else 400, headers=_HEADERS)

    async def events(request: Request):
        run_id = request.path_params["run_id"]
        last_event_id = request.headers.get("last-event-id", "")
        try:
            if request.query_params or last_event_id and not re.fullmatch(r"[a-f0-9]{64}", last_event_id):
                raise ValueError("invalid event subscription")
            if await request.app.state.agent.observe(run_id, identity=request.state.identity) is None:
                raise RunNotFoundError("run does not exist")
        except RunNotFoundError:
            return JSONResponse({"error": "session_not_found"}, status_code=404, headers=_HEADERS)
        except ValueError:
            return JSONResponse({"error": "invalid_session_input"}, status_code=400, headers=_HEADERS)
        return StreamingResponse(
            _events(request, run_id, last_event_id), media_type="text/event-stream",
            headers={**_HEADERS, "X-Accel-Buffering": "no"},
        )

    return [
        Route("/v1/agent/sessions", endpoint, methods=["GET"]),
        Route("/v1/agent/sessions/{run_id}", endpoint, methods=["GET", "PATCH", "DELETE"]),
        Route("/v1/agent/runs/{run_id}/events", events, methods=["GET"]),
    ]


async def _events(request: Request, run_id: str, last_event_id: str = ""):
    agent = request.app.state.agent
    verifier = request.app.state.identity_verifier
    original = request.state.identity
    loop = asyncio.get_running_loop()
    deadline = loop.time() + SSE_MAX_SECONDS
    last_sent = loop.time()
    while True:
        try:
            async with asyncio.timeout_at(deadline):
                identity = await verifier.authenticate(original.authorization)
                if (identity.user_id, identity.role, identity.source) != (original.user_id, original.role, original.source):
                    raise AuthenticationError("identity changed")
                result = await agent.observe(run_id, identity=identity)
                if result is None:
                    raise RunNotFoundError("run does not exist")
        except AuthenticationError:
            yield _event("end", {"reason": "unauthorized", "reconnect": False})
            return
        except IdentityUnavailableError:
            yield _event("end", {"reason": "identity_service_unavailable", "reconnect": True})
            return
        except RunNotFoundError:
            yield _event("end", {"reason": "session_not_found", "reconnect": False})
            return
        except TimeoutError:
            yield _event("end", {"reason": "reconnect_required", "reconnect": True})
            return
        fingerprint = hashlib.sha256(json.dumps(result, sort_keys=True, ensure_ascii=False,
                                               separators=(",", ":")).encode("utf-8")).hexdigest()
        if fingerprint != last_event_id:
            event = {
                "runId": run_id, "status": result["status"],
                "revision": result["revision"], "slotVersion": result["slotVersion"],
                "taskId": result["contentTask"].get("taskId"),
                "taskStatus": result["taskStatus"].get("status"),
                "mediaRunId": result["mediaRun"].get("runId"),
                "mediaStatus": result["mediaStatus"].get("status"),
                "requiresConfirmation": result["status"] == "awaiting_confirmation",
            }
            yield _event("status", event, fingerprint)
            last_event_id = fingerprint
            last_sent = loop.time()
        elif loop.time() - last_sent >= SSE_HEARTBEAT_SECONDS:
            yield _event("heartbeat", {"runId": run_id})
            last_sent = loop.time()
        try:
            async with asyncio.timeout_at(deadline):
                await asyncio.sleep(SSE_POLL_SECONDS)
        except TimeoutError:
            yield _event("end", {"reason": "reconnect_required", "reconnect": True})
            return


def _event(kind: str, data: dict, event_id: str | None = None) -> str:
    identifier = f"id: {event_id}\n" if event_id else ""
    return f"event: {kind}\n{identifier}data: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n"

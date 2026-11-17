from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from flmh_agent.adapters.outbound.works import JavaWorkCatalog, MEDIA_EXTENSIONS, WorkRequestError
from flmh_agent.domain.identity import UserIdentity


HEADERS = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}
WorkType = Literal["report", "script", "image", "video"]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class WorkQuery(Input):
    module: str | None = Field(default=None, max_length=64)
    type: WorkType | None = None
    sort: Literal["createdAt_desc", "createdAt_asc", "title_asc", "title_desc", "module_asc", "module_desc", "type_asc", "type_desc"] = "createdAt_desc"
    page: int = Field(default=1, ge=1, le=2147483647)
    pageSize: int = Field(default=20, ge=1, le=50)
    limit: int | None = Field(default=None, ge=1, le=50)


class LineageQuery(Input):
    page: int = Field(default=1, ge=1, le=2147483647)
    pageSize: int = Field(default=20, ge=1, le=50)


class WorkSave(Input):
    module: str | None = Field(default=None, min_length=1, max_length=64)
    title: str | None = Field(default=None, min_length=1, max_length=256)
    type: Literal["report", "script"] | None = None
    content: str | None = Field(default=None, min_length=1, max_length=65536)
    taskId: str | None = Field(default=None, min_length=1, max_length=96)
    runId: str | None = Field(default=None, min_length=1, max_length=96)
    nodeKey: Literal["image", "video"] | None = None

    @model_validator(mode="after")
    def validate_mode(self):
        if self.taskId is not None or self.runId is not None:
            if self.taskId is not None and self.runId is not None or any(value is not None for value in (self.module, self.title, self.type, self.content)):
                raise ValueError("mixed_work_modes")
            if self.runId is not None and self.nodeKey is None or self.taskId is not None and self.nodeKey is not None:
                raise ValueError("invalid_work_reference")
        elif self.nodeKey is not None or any(value is None or not value.strip() for value in (self.module, self.title, self.type, self.content)):
            raise ValueError("invalid_manual_work")
        return self


class WorkView(BaseModel):
    id: str
    module: str
    title: str
    type: WorkType
    content: str | None = None
    mediaUrl: str | None = None
    provider: str | None = None
    source: str | None = None
    createdAt: str
    lineageType: str | None = None


class WorkPage(BaseModel):
    records: list[WorkView] = Field(max_length=50)
    total: int
    page: int
    pageSize: int
    totalPages: int
    availableModules: list[str] = Field(max_length=100)
    availableModulesTruncated: bool


class LineageEntry(BaseModel):
    kind: Literal["CONTENT_TASK", "MEDIA_NODE", "WORKFLOW_NODE"]
    originId: str
    nodeKey: str
    attempt: int
    requestId: str | None = None
    providerTaskId: str | None = None
    inputFingerprint: str | None = None
    originRunId: str | None = None
    currentStatus: str | None = None
    sourceExists: bool
    createdAt: str


class LineagePage(BaseModel):
    workId: str
    entries: list[LineageEntry] = Field(max_length=50)
    total: int
    page: int
    pageSize: int
    totalPages: int


class SavedWork(BaseModel):
    record: WorkView
    reused: bool


class DeletedWork(BaseModel):
    success: Literal[True]


class WorkFileResponse(Response):
    def __init__(self, catalog: JavaWorkCatalog, work_id: str, identity: UserIdentity) -> None:
        super().__init__()
        self.catalog, self.work_id, self.identity = catalog, work_id, identity

    async def __call__(self, scope, receive, send) -> None:
        started = False
        try:
            async with self.catalog.open_file(self.work_id, self.identity) as (upstream, content_type, size):
                async def chunks():
                    received = 0
                    async for chunk in upstream.aiter_bytes(chunk_size=65536):
                        received += len(chunk)
                        if received > size:
                            raise WorkRequestError(502, "invalid_work_file_length")
                        yield chunk
                    if received != size:
                        raise WorkRequestError(502, "invalid_work_file_length")

                async def tracked_send(message):
                    nonlocal started
                    if message["type"] == "http.response.start":
                        started = True
                    await send(message)

                headers = {**HEADERS, "Content-Length": str(size),
                           "Content-Disposition": f'attachment; filename="{self.work_id}{MEDIA_EXTENSIONS[content_type]}"'}
                await StreamingResponse(chunks(), media_type=content_type, headers=headers)(scope, receive, tracked_send)
        except WorkRequestError as error:
            if started:
                raise
            await JSONResponse({"error": str(error)}, status_code=error.status, headers=HEADERS)(scope, receive, send)


def work_routes(read_json) -> list[Route]:
    async def endpoint(request: Request):
        work_id = request.path_params.get("work_id")
        action = request.path_params.get("action")
        if work_id is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,95}", work_id):
            return JSONResponse({"error": "invalid_work_id"}, status_code=400, headers=HEADERS)
        if action not in {None, "lineage", "file"}:
            return JSONResponse({"error": "work_not_found"}, status_code=404, headers=HEADERS)
        catalog = request.app.state.works
        identity = request.state.identity
        try:
            params = payload = key = None
            if request.method == "GET" and work_id is None:
                params = WorkQuery.model_validate(dict(request.query_params)).model_dump(exclude_none=True)
                output = WorkPage
            elif request.method == "GET" and action == "lineage":
                params = LineageQuery.model_validate(dict(request.query_params)).model_dump()
                output = LineagePage
            else:
                if request.query_params:
                    raise ValueError("unexpected_work_query")
                output = WorkView
                if request.method == "POST":
                    keys = request.headers.getlist("idempotency-key")
                    if len(keys) != 1 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,139}", keys[0]):
                        raise ValueError("invalid_work_idempotency_key")
                    key = keys[0]
                    payload = WorkSave.model_validate(await read_json(request)).model_dump(exclude_none=True)
                    output = SavedWork
                elif request.method == "DELETE":
                    output = DeletedWork
                elif action == "file":
                    return WorkFileResponse(catalog, work_id, identity)
        except ValidationError:
            return JSONResponse({"error": "invalid_work_input"}, status_code=422, headers=HEADERS)
        except ValueError as error:
            return JSONResponse({"error": "invalid_work_input"}, status_code=413 if "exceeds" in str(error) else 400, headers=HEADERS)
        path = "/api/v1/content/works" + (f"/{work_id}" if work_id else "") + (f"/{action}" if action else "")
        try:
            result = await catalog.request(request.method, path, identity, params=params, payload=payload, key=key)
            safe = output.model_validate(result).model_dump()
        except WorkRequestError as error:
            return JSONResponse({"error": str(error)}, status_code=error.status, headers=HEADERS)
        except ValidationError:
            return JSONResponse({"error": "invalid_work_response"}, status_code=502, headers=HEADERS)
        return JSONResponse(safe, headers=HEADERS)

    return [Route("/v1/works", endpoint, methods=["GET", "POST"]),
            Route("/v1/works/{work_id:str}", endpoint, methods=["GET", "DELETE"]),
            Route("/v1/works/{work_id:str}/{action:str}", endpoint, methods=["GET"])]

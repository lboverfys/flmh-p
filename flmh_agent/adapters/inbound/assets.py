from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from flmh_agent.adapters.outbound.assets import AssetRequestError, MEDIA_EXTENSIONS, MEDIA_LIMITS


HEADERS = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}
ASSET_ID = r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}"


class AssetView(BaseModel):
    id: str = Field(pattern=f"^{ASSET_ID}$")
    type: Literal["image", "video"]
    fileName: str
    contentType: Literal["image/png", "image/jpeg", "video/mp4", "video/webm"]
    bytes: int = Field(gt=0, le=50 * 1024 * 1024)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source: Literal["USER_UPLOAD"]
    storageMode: Literal["LOCAL_FILESYSTEM", "PRIVATE_OSS"]
    status: Literal["AVAILABLE"]
    createdAt: str


class AssetPage(BaseModel):
    assets: list[AssetView] = Field(max_length=50)
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    pageSize: int = Field(ge=1, le=50)
    totalPages: int = Field(ge=0)


class SavedAsset(BaseModel):
    asset: AssetView
    reused: bool


class AssetQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    page: int = Field(default=1, ge=1, le=100000)
    pageSize: int = Field(default=20, ge=1, le=50)
    type: Literal["image", "video"] | None = None


class UploadQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fileName: str = Field(min_length=1, max_length=255)


class AssetFileResponse(Response):
    def __init__(self, catalog, asset_id, identity):
        super().__init__()
        self.catalog, self.asset_id, self.identity = catalog, asset_id, identity

    async def __call__(self, scope, receive, send):
        started = False
        try:
            async with self.catalog.open_file(self.asset_id, self.identity) as (response, content_type, size):
                async def chunks():
                    received = 0
                    async for chunk in response.aiter_bytes(chunk_size=65536):
                        received += len(chunk)
                        if received > size:
                            raise AssetRequestError(502, "invalid_asset_file_length")
                        yield chunk
                    if received != size:
                        raise AssetRequestError(502, "invalid_asset_file_length")

                async def tracked_send(message):
                    nonlocal started
                    if message["type"] == "http.response.start":
                        started = True
                    await send(message)

                headers = {**HEADERS, "Content-Length": str(size),
                           "Content-Disposition": f'attachment; filename="{self.asset_id}{MEDIA_EXTENSIONS[content_type]}"'}
                await StreamingResponse(chunks(), media_type=content_type, headers=headers)(scope, receive, tracked_send)
        except AssetRequestError as error:
            if started:
                raise
            await JSONResponse({"error": str(error)}, status_code=error.status, headers=HEADERS)(scope, receive, send)


def asset_routes() -> list[Route]:
    async def endpoint(request: Request):
        asset_id = request.path_params.get("asset_id")
        if asset_id and re.fullmatch(ASSET_ID, asset_id) is None:
            return JSONResponse({"error": "asset_not_found"}, status_code=404, headers=HEADERS)
        catalog, identity = request.app.state.assets, request.state.identity
        try:
            if len(request.query_params) != len(request.query_params.multi_items()):
                raise ValueError("duplicate_asset_query")
            if asset_id:
                if request.query_params:
                    raise ValueError("unexpected_asset_query")
                if request.path_params.get("action") == "file":
                    return AssetFileResponse(catalog, asset_id, identity)
                if request.path_params.get("action"):
                    raise AssetRequestError(404, "asset_not_found")
                result = await catalog.request("GET", f"/api/v1/content/assets/{asset_id}", identity)
                output = AssetView
            elif request.method == "GET":
                params = AssetQuery.model_validate(dict(request.query_params)).model_dump(exclude_none=True)
                result = await catalog.request("GET", "/api/v1/content/assets", identity, params=params)
                output = AssetPage
            else:
                params = UploadQuery.model_validate(dict(request.query_params)).model_dump()
                for header in ("content-length", "content-type", "idempotency-key"):
                    if len(request.headers.getlist(header)) != 1:
                        raise ValueError("missing_or_duplicate_upload_header")
                content_type, length, key = (request.headers[name] for name in ("content-type", "content-length", "idempotency-key"))
                if content_type not in MEDIA_LIMITS or request.headers.get("content-encoding", "identity") != "identity":
                    raise AssetRequestError(415, "asset_type_unsupported")
                if not length.isascii() or not length.isdecimal() or not 0 < int(length) <= MEDIA_LIMITS[content_type]:
                    raise AssetRequestError(413, "asset_size_exceeded")
                if not key.strip() or len(key) > 140 or key != key.strip():
                    raise ValueError("invalid_asset_key")
                size = int(length)

                async def chunks():
                    received = 0
                    async for chunk in request.stream():
                        received += len(chunk)
                        if received > size:
                            raise AssetRequestError(413, "asset_size_exceeded")
                        for offset in range(0, len(chunk), 65536):
                            yield chunk[offset:offset + 65536]
                    if received != size:
                        raise AssetRequestError(400, "asset_upload_incomplete")

                result = await catalog.request("POST", "/api/v1/content/assets", identity, params=params, body=chunks(),
                                               content_type=content_type, size=size, key=key)
                output = SavedAsset
        except (ValidationError, ValueError):
            return JSONResponse({"error": "invalid_asset_input"}, status_code=422, headers=HEADERS)
        except AssetRequestError as error:
            return JSONResponse({"error": str(error)}, status_code=error.status, headers=HEADERS)
        except ClientDisconnect:
            return JSONResponse({"error": "asset_upload_disconnected"}, status_code=400, headers=HEADERS)
        try:
            return JSONResponse(output.model_validate(result).model_dump(mode="json"), headers=HEADERS)
        except ValidationError:
            return JSONResponse({"error": "invalid_asset_response"}, status_code=502, headers=HEADERS)

    return [Route("/v1/assets", endpoint, methods=["GET", "POST"]),
            Route("/v1/assets/{asset_id}", endpoint, methods=["GET"]),
            Route("/v1/assets/{asset_id}/{action}", endpoint, methods=["GET"])]

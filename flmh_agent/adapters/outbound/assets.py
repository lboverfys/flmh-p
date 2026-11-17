from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx

from flmh_agent.config import Settings
from flmh_agent.domain.identity import UserIdentity


MEDIA_LIMITS = {"image/png": 25 * 1024 * 1024, "image/jpeg": 25 * 1024 * 1024,
                "video/mp4": 50 * 1024 * 1024, "video/webm": 50 * 1024 * 1024}
MEDIA_EXTENSIONS = {"image/png": ".png", "image/jpeg": ".jpg", "video/mp4": ".mp4", "video/webm": ".webm"}


class AssetRequestError(RuntimeError):
    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status


class JavaMediaAssets:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings, self.client = settings, client

    @asynccontextmanager
    async def _connection(self):
        if self.client is not None:
            yield self.client
        else:
            async with httpx.AsyncClient(base_url=self.settings.gateway_url,
                                        timeout=self.settings.request_timeout_seconds, trust_env=False) as client:
                yield client

    def _headers(self, identity: UserIdentity) -> dict[str, str]:
        return {"Authorization": identity.authorization, "Accept-Encoding": "identity",
                **({"X-Service-Key": self.settings.gateway_api_key} if self.settings.gateway_api_key else {})}

    @staticmethod
    def _status(response: httpx.Response) -> None:
        if response.status_code == 200:
            return
        codes = {400: "invalid_asset_input", 401: "unauthorized", 403: "asset_forbidden", 404: "asset_not_found",
                 409: "asset_conflict", 413: "asset_size_exceeded", 415: "asset_type_unsupported",
                 422: "invalid_asset_input", 429: "too_many_requests"}
        raise AssetRequestError(response.status_code if response.status_code in codes else 503,
                                codes.get(response.status_code, "asset_service_unavailable"))

    async def request(self, method: str, path: str, identity: UserIdentity, *, params: dict | None = None,
                      body: AsyncIterator[bytes] | None = None, content_type: str | None = None,
                      size: int | None = None, key: str | None = None) -> dict:
        headers = self._headers(identity)
        if body is not None:
            headers.update({"Content-Type": content_type, "Content-Length": str(size), "Idempotency-Key": key})
        try:
            async with self._connection() as client:
                async with client.stream(method, path, params=params, headers=headers, content=body,
                                         follow_redirects=False) as response:
                    self._status(response)
                    raw = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=65536):
                        if len(raw) + len(chunk) > 1024 * 1024:
                            raise AssetRequestError(502, "invalid_asset_response")
                        raw.extend(chunk)
            result = json.loads(raw)
        except httpx.HTTPError as error:
            raise AssetRequestError(503, "asset_service_unavailable") from error
        except ValueError as error:
            raise AssetRequestError(502, "invalid_asset_response") from error
        if not isinstance(result, dict):
            raise AssetRequestError(502, "invalid_asset_response")
        return result

    @asynccontextmanager
    async def open_file(self, asset_id: str, identity: UserIdentity):
        try:
            async with self._connection() as client:
                async with client.stream("GET", f"/api/v1/content/assets/{asset_id}/file",
                                         headers=self._headers(identity), follow_redirects=False) as response:
                    self._status(response)
                    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    size = response.headers.get("content-length", "")
                    if (content_type not in MEDIA_LIMITS or not size.isascii() or not size.isdecimal()
                            or not 0 < int(size) <= MEDIA_LIMITS[content_type]
                            or response.headers.get("content-encoding", "identity") != "identity"):
                        raise AssetRequestError(502, "invalid_asset_file")
                    yield response, content_type, int(size)
        except httpx.HTTPError as error:
            raise AssetRequestError(503, "asset_service_unavailable") from error

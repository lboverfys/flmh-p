from __future__ import annotations

import json
from contextlib import asynccontextmanager

import httpx

from flmh_agent.config import Settings
from flmh_agent.domain.identity import UserIdentity


MAX_MEDIA_BYTES = 200 * 1024 * 1024
MEDIA_EXTENSIONS = {"image/png": ".png", "image/jpeg": ".jpg", "video/mp4": ".mp4", "video/webm": ".webm"}


class WorkRequestError(RuntimeError):
    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status


class JavaWorkCatalog:
    """Authenticated workbench transport; ownership and archive transactions remain in Java."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self.client = client

    @asynccontextmanager
    async def _connection(self):
        if self.client is not None:
            yield self.client
        else:
            async with httpx.AsyncClient(base_url=self.settings.gateway_url,
                                        timeout=self.settings.request_timeout_seconds, trust_env=False) as client:
                yield client

    def _headers(self, identity: UserIdentity, key: str | None = None) -> dict[str, str]:
        headers = {"Authorization": identity.authorization, "Accept-Encoding": "identity"}
        if self.settings.gateway_api_key:
            headers["X-Service-Key"] = self.settings.gateway_api_key
        if key is not None:
            headers["Idempotency-Key"] = key
        return headers

    @staticmethod
    def _status(response: httpx.Response) -> None:
        if response.status_code == 200:
            return
        codes = {400: "work_input_rejected", 401: "unauthorized", 403: "work_forbidden",
                 404: "work_not_found", 409: "work_conflict", 429: "too_many_requests"}
        status = response.status_code if response.status_code in codes else 503
        raise WorkRequestError(status, codes.get(status, "work_service_unavailable"))

    async def request(self, method: str, path: str, identity: UserIdentity, *, params: dict | None = None,
                      payload: dict | None = None, key: str | None = None) -> dict:
        try:
            async with self._connection() as client:
                async with client.stream(method, path, headers=self._headers(identity, key), params=params, json=payload,
                                         follow_redirects=False) as response:
                    self._status(response)
                    raw = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=65536):
                        raw.extend(chunk)
                        if len(raw) > 4 * 1024 * 1024:
                            raise WorkRequestError(502, "invalid_work_response")
            data = json.loads(raw)
        except httpx.HTTPError as error:
            raise WorkRequestError(503, "work_service_unavailable") from error
        except ValueError as error:
            raise WorkRequestError(502, "invalid_work_response") from error
        if not isinstance(data, dict):
            raise WorkRequestError(502, "invalid_work_response")
        return data

    @asynccontextmanager
    async def open_file(self, work_id: str, identity: UserIdentity):
        try:
            async with self._connection() as client:
                async with client.stream("GET", f"/api/v1/content/works/{work_id}/file",
                                         headers=self._headers(identity), follow_redirects=False) as response:
                    self._status(response)
                    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    length = response.headers.get("content-length", "")
                    if content_type not in MEDIA_EXTENSIONS or not length.isascii() or not length.isdecimal():
                        raise WorkRequestError(502, "invalid_work_file")
                    if response.headers.get("content-encoding", "identity").lower() != "identity":
                        raise WorkRequestError(502, "invalid_work_file")
                    size = int(length)
                    if not 0 < size <= MAX_MEDIA_BYTES:
                        raise WorkRequestError(502, "work_file_size_exceeded")
                    yield response, content_type, size
        except httpx.HTTPError as error:
            raise WorkRequestError(503, "work_service_unavailable") from error

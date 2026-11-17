from __future__ import annotations

import json
from contextlib import asynccontextmanager

import httpx

from flmh_agent.config import Settings
from flmh_agent.domain.identity import UserIdentity


class WorkflowRequestError(RuntimeError):
    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status


class JavaWorkflowClient:
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

    async def request(self, method: str, path: str, identity: UserIdentity, *, params=None, payload=None, key=None) -> dict:
        headers = {"Authorization": identity.authorization, "Accept-Encoding": "identity"}
        if self.settings.gateway_api_key:
            headers["X-Service-Key"] = self.settings.gateway_api_key
        if key:
            headers["Idempotency-Key"] = key
        try:
            async with self._connection() as client:
                async with client.stream(method, path, params=params, json=payload, headers=headers, follow_redirects=False) as response:
                    if response.status_code != 200:
                        codes = {400: "workflow_input_rejected", 401: "unauthorized", 403: "workflow_forbidden",
                                 404: "workflow_not_found", 409: "workflow_conflict", 422: "workflow_validation_failed",
                                 429: "too_many_requests"}
                        status = response.status_code if response.status_code in codes else 503
                        raise WorkflowRequestError(status, codes.get(status, "workflow_service_unavailable"))
                    raw = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=65536):
                        if len(raw) + len(chunk) > 4 * 1024 * 1024:
                            raise WorkflowRequestError(502, "invalid_workflow_response")
                        raw.extend(chunk)
            result = json.loads(raw)
        except httpx.HTTPError as error:
            raise WorkflowRequestError(503, "workflow_service_unavailable") from error
        except ValueError as error:
            raise WorkflowRequestError(502, "invalid_workflow_response") from error
        if not isinstance(result, dict):
            raise WorkflowRequestError(502, "invalid_workflow_response")
        return result

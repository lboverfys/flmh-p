from __future__ import annotations

import json
from contextlib import asynccontextmanager

import httpx

from flmh_agent.config import Settings
from flmh_agent.domain.identity import UserIdentity


class ProductBrowseError(RuntimeError):
    def __init__(self, status: int, code: str):
        super().__init__(code)
        self.status = status


class JavaProductBrowser:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.settings, self.client = settings, client

    @asynccontextmanager
    async def _connection(self):
        if self.client is not None:
            yield self.client
        else:
            async with httpx.AsyncClient(base_url=self.settings.gateway_url,
                                        timeout=self.settings.request_timeout_seconds, trust_env=False) as client:
                yield client

    async def browse(self, params: dict, identity: UserIdentity) -> dict:
        headers = {"Authorization": identity.authorization, "Accept-Encoding": "identity"}
        if self.settings.gateway_api_key:
            headers["X-Service-Key"] = self.settings.gateway_api_key
        try:
            async with self._connection() as client:
                async with client.stream("GET", "/api/v1/products/catalog", params=params, headers=headers,
                                         follow_redirects=False) as response:
                    if response.status_code != 200:
                        status = response.status_code if response.status_code in (400, 401, 403, 429) else 503
                        raise ProductBrowseError(status, "product_catalog_request_failed")
                    body = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=65536):
                        if len(body) + len(chunk) > 4 * 1024 * 1024:
                            raise ProductBrowseError(502, "invalid_product_catalog_response")
                        body.extend(chunk)
            value = json.loads(body)
        except httpx.HTTPError as error:
            raise ProductBrowseError(503, "product_catalog_unavailable") from error
        except ValueError as error:
            raise ProductBrowseError(502, "invalid_product_catalog_response") from error
        if not isinstance(value, dict):
            raise ProductBrowseError(502, "invalid_product_catalog_response")
        return value

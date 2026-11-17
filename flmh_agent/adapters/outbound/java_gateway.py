from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

import httpx

from flmh_agent.config import Settings
from flmh_agent.domain.models import ProductHistoryQuery, ProductLookupQuery


class JavaGatewayClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._client = client

    async def search_products(self, query: Mapping[str, Any]) -> dict[str, Any]:
        params = {"region": query["region"], "limit": query.get("limit", 20)}
        if query.get("keyword"):
            params["keyword"] = query["keyword"]
        if query.get("category"):
            params["category"] = query["category"]
        if query.get("max_price") is not None:
            params["maxPrice"] = query["max_price"]
        return await self._request("GET", "/api/v1/products", params=params)

    async def create_quote(self, request: Mapping[str, Any]) -> dict[str, Any]:
        payload = {
            "productId": request["product_id"], "contentType": request["content_type"],
            "brief": request["brief"], "budgetMax": request.get("budget_max"),
        }
        return await self._request("POST", "/api/v1/content/quotes", json=payload)

    async def get_product_history(self, product_id: str, *, limit: int = 30, before_version: int | None = None) -> dict[str, Any]:
        query = ProductHistoryQuery(product_id=product_id, limit=limit, before_version=before_version)
        params = {"limit": query.limit}
        if query.before_version is not None:
            params["beforeVersion"] = query.before_version
        return await self._request("GET", f"/api/v1/products/{quote(query.product_id, safe='')}/history", params=params)

    async def get_product_detail(self, product_id: str) -> dict[str, Any]:
        query = ProductLookupQuery(product_id=product_id)
        return await self._request("GET", f"/api/v1/products/{quote(query.product_id, safe='')}/detail")

    async def confirm_quote(self, quote_id: str) -> dict[str, Any]:
        response = await self._request(
            "POST", f"/api/v1/content/quotes/{quote(quote_id, safe='')}/confirm", json={"confirmed": True}
        )
        return {**response, "confirmed": response.get("status") == "CONFIRMED"}

    async def submit_task(self, request: Mapping[str, Any], idempotency_key: str) -> dict[str, Any]:
        payload = {
            "quoteId": request["quote_id"], "productId": request["product_id"],
            "contentType": request["content_type"], "brief": request["brief"],
            "idempotencyKey": idempotency_key,
        }
        return await self._request(
            "POST", "/api/v1/content/tasks", json=payload,
            headers={"Idempotency-Key": idempotency_key},
        )

    async def get_task(self, task_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/api/v1/content/tasks/{quote(task_id, safe='')}")

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        headers = kwargs.pop("headers", {})
        if self.settings.gateway_api_key:
            headers["X-Service-Key"] = self.settings.gateway_api_key
            headers["X-Client-Id"] = "flmh-agent"
        kwargs["headers"] = headers
        if self._client is not None:
            response = await self._client.request(method, path, **kwargs)
        else:
            async with httpx.AsyncClient(
                base_url=self.settings.gateway_url,
                timeout=self.settings.request_timeout_seconds,
                trust_env=False,
            ) as client:
                response = await client.request(method, path, **kwargs)
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, dict):
            raise TypeError("Java Gateway returned a non-object response")
        return value

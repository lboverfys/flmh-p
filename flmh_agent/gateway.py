from __future__ import annotations

from typing import Any

import httpx

from .config import settings


class JavaGatewayClient:
    def __init__(self, base_url: str | None = None, timeout_seconds: float = 10.0) -> None:
        self.base_url = (base_url or settings.gateway_url).rstrip("/")
        self.timeout_seconds = timeout_seconds

    async def search_products(
        self,
        *,
        region: str,
        keyword: str | None = None,
        category: str | None = None,
        max_price: float | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"region": region}
        if keyword:
            params["keyword"] = keyword
        if category:
            params["category"] = category
        if max_price is not None:
            params["maxPrice"] = max_price
        return await self._request("GET", "/api/v1/products", params=params)

    async def create_content_quote(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/api/v1/content/quotes", json=payload)

    async def confirm_content_quote(self, quote_id: str, confirmed: bool) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/api/v1/content/quotes/{quote_id}/confirm",
            json={"confirmed": confirmed},
        )

    async def create_content_task(
        self,
        payload: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/api/v1/content/tasks",
            json=payload,
            headers={"Idempotency-Key": idempotency_key},
        )

    async def get_content_task(self, task_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/api/v1/content/tasks/{task_id}")

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        async with httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.timeout_seconds,
            trust_env=False,
        ) as client:
            response = await client.request(method, path, **kwargs)
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, dict):
            raise TypeError(f"Java Gateway returned a non-object response for {path}")
        return value

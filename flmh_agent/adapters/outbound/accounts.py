from __future__ import annotations

import httpx

from flmh_agent.config import Settings
from flmh_agent.domain.identity import UserIdentity


class AccountRequestError(RuntimeError):
    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status


class JavaUserAccounts:
    """Workbench transport; account rules and transactions remain in identity-service."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self.client = client

    async def request(self, method: str, path: str, identity: UserIdentity, *, params: dict | None = None,
                      payload: dict | None = None) -> dict:
        headers = {"Authorization": identity.authorization}
        if self.settings.gateway_api_key:
            headers["X-Service-Key"] = self.settings.gateway_api_key
        try:
            if self.client is not None:
                response = await self.client.request(method, path, headers=headers, params=params, json=payload)
            else:
                async with httpx.AsyncClient(base_url=self.settings.identity_service_url,
                                            timeout=self.settings.request_timeout_seconds, trust_env=False) as client:
                    response = await client.request(method, path, headers=headers, params=params, json=payload)
        except httpx.HTTPError as error:
            raise AccountRequestError(503, "account_service_unavailable") from error
        if response.status_code not in {200, 201}:
            code = {400: "account_input_rejected", 401: "unauthorized", 403: "admin_required",
                    404: "account_not_found", 409: "account_conflict", 429: "too_many_requests"}
            status = response.status_code if response.status_code in code else 503
            raise AccountRequestError(status, code.get(status, "account_service_unavailable"))
        try:
            data = response.json()
        except ValueError as error:
            raise AccountRequestError(502, "invalid_account_response") from error
        if not isinstance(data, dict):
            raise AccountRequestError(502, "invalid_account_response")
        return data

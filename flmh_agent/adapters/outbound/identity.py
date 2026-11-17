from __future__ import annotations

import httpx
from pydantic import BaseModel, Field, ValidationError
from typing import Literal

from flmh_agent.config import Settings
from flmh_agent.domain.identity import AuthenticationError, IdentityUnavailableError, UserIdentity


class _VerifiedUser(BaseModel):
    id: str = Field(min_length=1, max_length=128)
    role: Literal["USER", "ADMIN", "SUPER_ADMIN"]
    status: Literal["ACTIVE"]
    source: Literal["LOCAL_MOCK", "LEGACY_JDBC"]


class JavaIdentityVerifier:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._client = client

    async def authenticate(self, authorization: str) -> UserIdentity:
        parts = authorization.split(" ")
        if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1] or len(authorization) > 16384 or any(char.isspace() for char in parts[1]):
            raise AuthenticationError("Bearer user token is required")
        headers = {"Authorization": authorization}
        if self.settings.gateway_api_key:
            headers["X-Service-Key"] = self.settings.gateway_api_key
        try:
            if self._client is not None:
                response = await self._client.get("/api/v1/identity/me", headers=headers)
            else:
                async with httpx.AsyncClient(base_url=self.settings.identity_service_url,
                                            timeout=self.settings.request_timeout_seconds, trust_env=False) as client:
                    response = await client.get("/api/v1/identity/me", headers=headers)
        except httpx.HTTPError as error:
            raise IdentityUnavailableError("identity service is unavailable") from error
        if response.status_code in {401, 403}:
            raise AuthenticationError("user token is invalid, expired, or disabled")
        if response.status_code != 200:
            raise IdentityUnavailableError("identity service rejected verification")
        try:
            user = _VerifiedUser.model_validate(response.json())
        except (ValidationError, ValueError) as error:
            raise AuthenticationError("identity service did not return an active user") from error
        if self.settings.environment == "production" and user.source != "LEGACY_JDBC":
            raise AuthenticationError("Mock identities cannot authenticate production requests")
        return UserIdentity(user.id, user.role, user.source, authorization)

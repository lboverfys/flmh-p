from __future__ import annotations

import httpx
import json
from contextlib import asynccontextmanager
from pydantic import BaseModel, Field, ValidationError
from typing import Literal

from flmh_agent.config import Settings
from flmh_agent.domain.identity import AuthenticationError, IdentityUnavailableError, IdentityRateLimitedError, IdentityInputError, UserIdentity


class _VerifiedUser(BaseModel):
    id: str = Field(min_length=1, max_length=128)
    role: Literal["USER", "ADMIN", "SUPER_ADMIN"]
    status: Literal["ACTIVE"]
    source: Literal["LOCAL_MOCK", "LEGACY_JDBC"]


class _LoginResponse(BaseModel):
    token: str = Field(min_length=1, max_length=16377, pattern=r"^\S+$", repr=False)
    tokenType: Literal["Bearer"]
    expiresAt: str = Field(min_length=1)
    user: _VerifiedUser
    source: Literal["LOCAL_MOCK", "LEGACY_JDBC"]


class _RecoveryRequested(BaseModel):
    message: Literal["如果该邮箱已注册，将收到密码重置邮件，请查收。"]


class _PasswordReset(BaseModel):
    message: Literal["密码重置成功，请使用新密码登录。"]


class JavaIdentityVerifier:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._client = client

    @asynccontextmanager
    async def _connection(self):
        if self._client is not None:
            yield self._client
        else:
            async with httpx.AsyncClient(base_url=self.settings.identity_service_url,
                                        timeout=self.settings.request_timeout_seconds, trust_env=False) as client:
                yield client

    async def _anonymous_request(self, path: str, payload: dict, client_ip: str) -> dict:
        headers = {"X-Flmh-Client-IP": client_ip, "Accept-Encoding": "identity"}
        if self.settings.gateway_api_key:
            headers["X-Service-Key"] = self.settings.gateway_api_key
        try:
            async with self._connection() as client:
                async with client.stream("POST", path, json=payload, headers=headers, follow_redirects=False) as response:
                    if response.status_code in {401, 403}:
                        raise AuthenticationError("invalid credentials or inactive user")
                    if response.status_code == 429:
                        raise IdentityRateLimitedError("too_many_authentication_attempts")
                    if response.status_code in {400, 422}:
                        raise IdentityInputError("invalid_identity_input")
                    if response.status_code != 200:
                        raise IdentityUnavailableError("identity service rejected the request")
                    raw = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=16384):
                        if len(raw) + len(chunk) > 65536:
                            raise IdentityUnavailableError("identity response exceeds the allowed size")
                        raw.extend(chunk)
            result = json.loads(raw)
        except httpx.HTTPError as error:
            raise IdentityUnavailableError("identity service is unavailable") from error
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise IdentityUnavailableError("identity service returned invalid JSON") from error
        if not isinstance(result, dict):
            raise IdentityUnavailableError("identity service returned an invalid response")
        return result

    async def login(self, email: str, password: str, *, client_ip: str) -> dict:
        payload = await self._anonymous_request("/api/v1/identity/login", {"email": email, "password": password}, client_ip)
        try:
            result = _LoginResponse.model_validate(payload)
        except (ValidationError, ValueError) as error:
            raise IdentityUnavailableError("identity service returned an invalid login response") from error
        if result.source != result.user.source or self.settings.environment == "production" and result.source != "LEGACY_JDBC":
            raise AuthenticationError("Mock identities cannot authenticate production requests")
        return result.model_dump()

    async def forgot_password(self, email: str, *, client_ip: str) -> dict:
        result = await self._anonymous_request("/api/v1/identity/forgot-password", {"email": email}, client_ip)
        try:
            return _RecoveryRequested.model_validate(result).model_dump()
        except ValidationError as error:
            raise IdentityUnavailableError("invalid password recovery response") from error

    async def reset_password(self, email: str, code: str, new_password: str, *, client_ip: str) -> dict:
        result = await self._anonymous_request("/api/v1/identity/reset-password",
                                               {"email": email, "code": code, "newPassword": new_password}, client_ip)
        try:
            return _PasswordReset.model_validate(result).model_dump()
        except ValidationError as error:
            raise IdentityUnavailableError("invalid password reset response") from error

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

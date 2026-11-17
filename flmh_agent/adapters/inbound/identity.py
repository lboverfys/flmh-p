from __future__ import annotations

import hmac
from typing import Any

from starlette.responses import JSONResponse

from flmh_agent.domain.identity import AuthenticationError, IdentityUnavailableError, bind_identity
from flmh_agent.ports.identity import IdentityVerifierPort


class IdentityMiddleware:
    def __init__(self, app: Any, *, api_key: str = "", verifier: IdentityVerifierPort | None = None,
                 public_paths: frozenset[str] = frozenset()) -> None:
        self.app = app
        self.api_key = api_key.encode("utf-8")
        self.verifier = verifier
        self.public_paths = public_paths

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope.get("method") in {"GET", "HEAD"} and scope.get("path") in self.public_paths:
            await self.app(scope, receive, send)
            return
        headers: dict[str, str] = {}
        duplicate_auth = False
        for raw_name, raw_value in scope.get("headers", []):
            name = raw_name.decode("latin1").lower()
            if name in {"authorization", "x-agent-key"} and name in headers:
                duplicate_auth = True
            headers[name] = raw_value.decode("latin1")
        if duplicate_auth or self.api_key and not hmac.compare_digest(headers.get("x-agent-key", "").encode("utf-8"), self.api_key):
            await self._reject(scope, receive, send, 401, "unauthorized")
            return
        verifier = self.verifier or scope["app"].state.identity_verifier
        try:
            identity = await verifier.authenticate(headers.get("authorization", ""))
        except AuthenticationError:
            await self._reject(scope, receive, send, 401, "unauthorized")
            return
        except IdentityUnavailableError:
            await self._reject(scope, receive, send, 503, "identity_service_unavailable")
            return
        scope.setdefault("state", {})["identity"] = identity
        with bind_identity(identity):
            await self.app(scope, receive, send)

    @staticmethod
    async def _reject(scope: dict[str, Any], receive: Any, send: Any, status: int, error: str) -> None:
        headers = {"Cache-Control": "no-store"}
        if status == 401:
            headers["WWW-Authenticate"] = "Bearer"
        await JSONResponse({"error": error}, status_code=status, headers=headers)(scope, receive, send)

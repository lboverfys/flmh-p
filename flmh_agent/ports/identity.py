from typing import Any, Protocol

from flmh_agent.domain.identity import UserIdentity


class IdentityVerifierPort(Protocol):
    async def authenticate(self, authorization: str) -> UserIdentity: ...


class IdentityLoginPort(Protocol):
    async def login(self, email: str, password: str) -> dict[str, Any]: ...

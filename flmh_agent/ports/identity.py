from typing import Any, Protocol

from flmh_agent.domain.identity import UserIdentity


class IdentityVerifierPort(Protocol):
    async def authenticate(self, authorization: str) -> UserIdentity: ...


class IdentityLoginPort(Protocol):
    async def login(self, email: str, password: str, *, client_ip: str) -> dict[str, Any]: ...


class IdentityRecoveryPort(Protocol):
    async def forgot_password(self, email: str, *, client_ip: str) -> dict[str, Any]: ...
    async def reset_password(self, email: str, code: str, new_password: str, *, client_ip: str) -> dict[str, Any]: ...

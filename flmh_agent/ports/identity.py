from typing import Protocol

from flmh_agent.domain.identity import UserIdentity


class IdentityVerifierPort(Protocol):
    async def authenticate(self, authorization: str) -> UserIdentity: ...

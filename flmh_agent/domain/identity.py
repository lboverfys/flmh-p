from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Iterator


class AuthenticationError(PermissionError):
    pass


class IdentityUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class UserIdentity:
    user_id: str
    role: str
    source: str
    authorization: str = field(repr=False, compare=False)


_identity: ContextVar[UserIdentity | None] = ContextVar("flmh_verified_user", default=None)


def current_identity() -> UserIdentity:
    identity = _identity.get()
    if identity is None:
        raise AuthenticationError("verified user identity is required")
    return identity


@contextmanager
def bind_identity(identity: UserIdentity) -> Iterator[None]:
    token = _identity.set(identity)
    try:
        yield
    finally:
        _identity.reset(token)


def run_resource_key(identity: UserIdentity, run_id: str) -> str:
    value = json.dumps([identity.user_id, run_id], ensure_ascii=False, separators=(",", ":"))
    return "agent-user-v1:" + hashlib.sha256(value.encode("utf-8")).hexdigest()

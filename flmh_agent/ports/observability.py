from __future__ import annotations

from typing import Any, Protocol


class TraceHandlerFactory(Protocol):
    def create(self) -> Any | None: ...

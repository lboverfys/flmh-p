from contextlib import AbstractAsyncContextManager
from typing import Protocol


class RunCoordinatorPort(Protocol):
    def acquire(self, run_id: str) -> AbstractAsyncContextManager[None]: ...

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import TypeVar


T = TypeVar("T")


async def complete_cleanup(operation: Awaitable[T], original: BaseException | None = None) -> T:
    task = asyncio.ensure_future(operation)
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as interrupted:
            cancellation = interrupted
        except BaseException:
            break
    try:
        result = task.result()
    except BaseException as failure:
        preceding = original or cancellation
        if preceding is not None and preceding is not failure:
            raise BaseExceptionGroup("operation and resource cleanup failed", [preceding, failure]) from None
        raise
    if cancellation is not None:
        raise cancellation
    return result

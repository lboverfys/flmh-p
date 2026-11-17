from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from langgraph.checkpoint.memory import InMemorySaver

from flmh_agent.config import Settings


@asynccontextmanager
async def open_checkpointer(settings: Settings) -> AsyncIterator[Any]:
    if settings.checkpointer_backend == "memory":
        yield InMemorySaver()
        return
    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    except ImportError as error:
        raise RuntimeError("install the project 'postgres' extra to use PostgreSQL checkpoints") from error
    async with AsyncPostgresSaver.from_conn_string(settings.postgres_checkpointer_uri) as checkpointer:
        if settings.postgres_checkpoint_setup:
            await checkpointer.setup()
        yield checkpointer

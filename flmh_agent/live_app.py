"""Swap immutable ASGI app generations between requests; drain in-flight requests before cleanup."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, nullcontext

from starlette.applications import Starlette
from starlette.responses import JSONResponse

from flmh_agent.managed_settings import SettingsSource


class Generation:
    def __init__(self, app, settings):
        self.app = app
        self.settings = settings
        self.users = 0
        self.retired = False
        self.stop = asyncio.Event()
        self.ready = asyncio.get_running_loop().create_future()
        self.task = asyncio.create_task(self._run())

    async def _run(self):
        # MCP's AnyIO task group must enter and exit lifespan in the same task.
        try:
            async with self.app.router.lifespan_context(self.app):
                self.ready.set_result(None)
                await self.stop.wait()
        except Exception as failure:
            if self.ready.done():
                raise
            self.ready.set_exception(failure)

    def retire(self):
        self.retired = True
        if not self.users:
            self.stop.set()


class ManagedApp(Starlette):
    def __init__(self, factory, *, storage=None):
        self.source = SettingsSource()
        self.factory = factory
        self.storage = storage
        self.active = None
        self.generations = set()
        super().__init__(lifespan=self._lifespan)
        self.state.settings = self.source.initial

    @asynccontextmanager
    async def _lifespan(self, _app):
        self.lock = asyncio.Lock()
        session = self.storage(self.source.initial) if self.storage else nullcontext(None)
        async with session as self.session:
            try:
                await self._select()
                yield
            finally:
                for generation in self.generations:
                    generation.retire()
                await asyncio.gather(*(g.task for g in self.generations))
                self.generations.clear()
                self.active = None

    async def _select(self):
        async with self.lock:
            for old in tuple(self.generations):
                if old.retired and old.task.done():
                    old.task.result()
                    self.generations.remove(old)
            settings = self.source.refresh()
            if self.active is None or self.active.settings != settings:
                fresh = Generation(self.factory(settings, self.session), settings)
                self.generations.add(fresh)
                try:
                    await asyncio.shield(fresh.ready)
                except BaseException:
                    fresh.retire()
                    raise
                if self.active:
                    self.active.retire()
                self.active = fresh
                self.state.settings = settings
            return self.active

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            return await super().__call__(scope, receive, send)
        try:
            generation = await self._select()
        except (ValueError, OSError):
            return await JSONResponse({"error": "managed_settings_invalid"}, status_code=503)(scope, receive, send)
        generation.users += 1
        try:
            await generation.app(scope, receive, send)
        finally:
            generation.users -= 1
            if generation.retired and not generation.users:
                generation.stop.set()

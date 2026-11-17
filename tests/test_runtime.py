import asyncio
import unittest
from unittest.mock import patch

from flmh_agent.bootstrap import runtime_loop_factory
from flmh_agent.config import Settings


class RuntimeTests(unittest.TestCase):
    def test_windows_postgres_entrypoints_use_psycopg_compatible_event_loop(self):
        settings = Settings(checkpointer_backend="postgres", postgres_checkpointer_uri="postgresql://localhost/agent")
        with patch("flmh_agent.bootstrap.sys.platform", "win32"):
            factory = runtime_loop_factory(settings)
            self.assertIs(factory, asyncio.SelectorEventLoop)
            loop = factory()
            try:
                self.assertIsInstance(loop, asyncio.SelectorEventLoop)
            finally:
                loop.close()

    def test_local_memory_entrypoints_keep_the_platform_event_loop(self):
        self.assertIsNone(runtime_loop_factory(Settings()))

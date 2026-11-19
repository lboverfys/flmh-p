import base64
import json
import os
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from flmh_agent.config import Settings
from flmh_agent.live_app import ManagedApp
from flmh_agent.managed_settings import SettingsSource


class ManagedSettingsTest(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.dir = root / "local"
        self.dir.mkdir()
        self.key = AESGCM.generate_key(bit_length=256)
        self.enterContext(patch.dict(os.environ, {"APP_ENV": "local", "FLMH_SETTINGS_DIR": str(root),
            "FLMH_ENV_FILE": str(root / "unused.env"), "FLMH_SETTINGS_MASTER_KEY": base64.b64encode(self.key).decode()}, clear=True))

    def write(self, revision, **settings):
        nonce = os.urandom(12)
        payload = json.dumps({"revision": revision, "values": {"python." + k: v for k, v in settings.items()}}).encode()
        temp = self.dir / "snapshot.tmp"
        temp.write_bytes(nonce + AESGCM(self.key).encrypt(nonce, payload, None))
        temp.replace(self.dir / "settings.enc")

    def test_hot_updates_keep_session_storage_and_stage_listener_until_restart(self):
        @asynccontextmanager
        async def storage(_):
            yield []

        def application(settings, history):
            async def query(_):
                history.append(settings.sql_max_rows)
                return JSONResponse({"history": history, "port": settings.agent_port})
            return Starlette(routes=[Route("/test", query)])

        app = ManagedApp(application, storage=storage)
        with TestClient(app) as client:
            self.assertEqual(client.get("/test").json()["history"], [50])
            self.write(1, SQL_MAX_ROWS="12", AGENT_HTTP_PORT="18090")
            app.source.next_check = 0
            self.assertEqual(client.get("/test").json(), {"history": [50, 12], "port": 8090})
            self.assertEqual(Settings.from_env().agent_port, 18090)
            self.write(2)
            app.source.next_check = 0
            self.assertEqual(client.get("/test").json()["history"], [50, 12, 50])

    def test_invalid_snapshot_fails_closed_and_can_be_recovered(self):
        source = SettingsSource()
        (self.dir / "settings.enc").write_bytes(b"bad-data")
        with self.assertRaises(ValueError):
            source.refresh()
        self.write(1, REQUEST_TIMEOUT_SECONDS="6")
        self.assertEqual(source.refresh().request_timeout_seconds, 6)

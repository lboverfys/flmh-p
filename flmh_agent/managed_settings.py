from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from time import monotonic

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.exceptions import InvalidTag
from dotenv import dotenv_values

from flmh_agent.config import ConfigurationError, Settings

RESTART_KEYS = frozenset({"APP_ENV", "AGENT_HTTP_HOST", "AGENT_HTTP_PORT", "MCP_HTTP_HOST", "MCP_HTTP_PORT",
                          "CHECKPOINTER_BACKEND", "POSTGRES_CHECKPOINTER_URI", "POSTGRES_CHECKPOINT_SETUP"})


class SettingsSource:
    def __init__(self) -> None:
        env_file = Path(os.environ.get("FLMH_ENV_FILE", ".env"))
        defaults = dotenv_values(env_file) if env_file.is_file() else {}
        self.base = {**{key: value for key, value in defaults.items() if value is not None}, **os.environ}
        namespace = self.base.get("APP_ENV", "local")
        if namespace not in {"local", "test", "production"}:
            raise ConfigurationError("APP_ENV must be local, test or production")
        self.root = Path(self.base.get("FLMH_SETTINGS_DIR", "D:/rubbish/zhongjian/state/flmh/settings")) / namespace
        self.file = self.root / "settings.enc"
        self.stamp = self._stamp()
        overrides = self._read()
        self.cold = {key: value for key, value in overrides.items() if key in RESTART_KEYS}
        self.initial = self.current = Settings.from_env({**self.base, **overrides})
        self.next_check = 0.0

    def _stamp(self):
        try:
            stat = self.file.stat()
            return stat.st_ino, stat.st_mtime_ns, stat.st_size
        except FileNotFoundError:
            return None

    def _read(self) -> dict[str, str]:
        try:
            payload = self.file.read_bytes()
        except FileNotFoundError:
            return {}
        try:
            key = self.base.get("FLMH_SETTINGS_MASTER_KEY")
            if key:
                secret = base64.b64decode(key, validate=True)
            elif self.root.name != "production":
                secret = (self.root / "master.key").read_bytes()
            else:
                raise ConfigurationError("production requires FLMH_SETTINGS_MASTER_KEY")
            if len(secret) != 32:
                raise ConfigurationError("invalid settings key length")
            clear = AESGCM(secret).decrypt(payload[:12], payload[12:], None)
            return {k.removeprefix("python."): v for k, v in json.loads(clear)["values"].items() if k.startswith("python.")}
        except (OSError, ValueError, InvalidTag, KeyError) as error:
            raise ConfigurationError("无法读取或验证管理员配置快照") from error

    def refresh(self) -> Settings:
        now = monotonic()
        if now >= self.next_check:
            stamp = self._stamp()
            if stamp != self.stamp:
                live = {key: val for key, val in self._read().items() if key not in RESTART_KEYS}
                self.current = Settings.from_env({**self.base, **live, **self.cold})
                self.stamp = stamp
            self.next_check = now + 0.25
        return self.current

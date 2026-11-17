from __future__ import annotations

from typing import Any

from flmh_agent.config import Settings


class LangfuseCallbackFactory:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client = None

    def create(self) -> Any | None:
        if not all((self.settings.langfuse_host, self.settings.langfuse_public_key, self.settings.langfuse_secret_key)):
            return None
        try:
            from langfuse import Langfuse
            from langfuse.langchain import CallbackHandler
        except ImportError as error:
            raise RuntimeError("install the project 'observability' extra to enable Langfuse") from error
        if self._client is None:
            self._client = Langfuse(
                public_key=self.settings.langfuse_public_key,
                secret_key=self.settings.langfuse_secret_key,
                host=self.settings.langfuse_host,
            )
        return CallbackHandler(public_key=self.settings.langfuse_public_key)

    def close(self) -> None:
        if self._client is not None:
            self._client.shutdown()
            self._client = None

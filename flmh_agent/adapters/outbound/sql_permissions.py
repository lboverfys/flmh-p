from __future__ import annotations

import json
import hmac

from flmh_agent.application.text2sql import PRODUCT_RADAR_TABLES
from flmh_agent.config import Settings


class ConfiguredSqlPermissionPolicy:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        try:
            raw = json.loads(settings.bi_sql_role_tables_json or "{}")
        except json.JSONDecodeError as error:
            raise ValueError("BI_SQL_ROLE_TABLES_JSON must be valid JSON") from error
        if not isinstance(raw, dict) or any(
            not isinstance(role, str) or not isinstance(tables, list)
            or any(not isinstance(table, str) for table in tables)
            for role, tables in raw.items()
        ):
            raise ValueError("BI_SQL_ROLE_TABLES_JSON must map role names to arrays of table names")
        invalid_tables = {table for tables in raw.values() for table in tables} - PRODUCT_RADAR_TABLES
        if invalid_tables:
            raise ValueError(f"BI SQL permissions contain unsupported tables: {', '.join(sorted(invalid_tables))}")
        self._by_role = {role: frozenset(tables) for role, tables in raw.items()}
        try:
            credential_map = json.loads(settings.bi_sql_role_api_keys_json or "{}")
        except json.JSONDecodeError as error:
            raise ValueError("BI_SQL_ROLE_API_KEYS_JSON must be valid JSON") from error
        if not isinstance(credential_map, dict) or any(
            not isinstance(role, str) or not isinstance(key, str) or not key
            for role, key in credential_map.items()
        ):
            raise ValueError("BI_SQL_ROLE_API_KEYS_JSON must map role names to non-empty keys")
        if set(credential_map) != set(self._by_role):
            raise ValueError("BI SQL credential roles and table permission roles must match exactly")
        self._credentials = credential_map

    def authenticate(self, authorization: str) -> str | None:
        for role, key in self._credentials.items():
            if hmac.compare_digest(authorization, f"Bearer {key}"):
                return role
        return None

    async def allowed_tables(self, principal: str) -> frozenset[str]:
        return self._by_role.get(principal, frozenset())

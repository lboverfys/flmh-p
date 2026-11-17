from __future__ import annotations

import json

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
        if set(raw) - {"USER", "ADMIN", "SUPER_ADMIN"}:
            raise ValueError("BI SQL permission roles must be verified USER, ADMIN or SUPER_ADMIN roles")
        invalid_tables = {table for tables in raw.values() for table in tables} - PRODUCT_RADAR_TABLES
        if invalid_tables:
            raise ValueError(f"BI SQL permissions contain unsupported tables: {', '.join(sorted(invalid_tables))}")
        self._by_role = {role: frozenset(tables) for role, tables in raw.items()}

    async def allowed_tables(self, principal: str) -> frozenset[str]:
        return self._by_role.get(principal, frozenset())

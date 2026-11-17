from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError
from sqlglot.optimizer.scope import traverse_scope


PRODUCT_RADAR_TABLES = frozenset({
    "market_products",
    "market_product_daily_stats",
    "market_radar_metrics",
    "market_canonical_categories",
    "market_source_categories",
})
DEFAULT_SCORE_VERSION = "v2"
MAX_SQL_ROWS = 50
DEFAULT_SQL_ROWS = 20
FORBIDDEN_NODES = (
    exp.Alter, exp.Command, exp.Commit, exp.Copy, exp.Create, exp.Delete, exp.Drop,
    exp.Insert, exp.Lock, exp.Merge, exp.Rollback, exp.Set, exp.Transaction,
    exp.TruncateTable, exp.Update,
)
FORBIDDEN_FUNCTIONS = {
    "pg_sleep", "dblink", "lo_import", "lo_export", "pg_read_file", "pg_write_file", "pg_ls_dir",
    "current_setting",
}
ALLOWED_ANONYMOUS_FUNCTIONS = {"date_trunc", "date_part", "to_char"}


class SqlValidationError(ValueError):
    pass


class SqlPermissionPort(Protocol):
    async def allowed_tables(self, principal: str) -> Sequence[str]: ...


class SqlModelPort(Protocol):
    async def generate_sql(
        self, question: str, schema_context: str, *, previous_sql: str = "", error: str = ""
    ) -> Mapping[str, Any]: ...


class SqlRunnerPort(Protocol):
    async def fetch(self, query: str, *, max_rows: int, timeout_ms: int) -> Sequence[Mapping[str, Any]]: ...


class TextToSqlService:
    def __init__(
        self,
        *,
        permissions: SqlPermissionPort,
        model: SqlModelPort,
        runner: SqlRunnerPort,
        dialect: str = "postgres",
        max_rows: int = MAX_SQL_ROWS,
        timeout_ms: int = 5000,
    ) -> None:
        self.permissions = permissions
        self.model = model
        self.runner = runner
        self.dialect = dialect
        self.max_rows = min(max_rows, MAX_SQL_ROWS)
        self.timeout_ms = timeout_ms

    async def query(self, question: str, principal: str) -> dict[str, Any]:
        if not question.strip() or not principal.strip():
            raise ValueError("question and authenticated principal are required")
        allowed = set(await self.permissions.allowed_tables(principal)) & PRODUCT_RADAR_TABLES
        if not allowed:
            raise PermissionError("principal has no BI table permissions")
        context = _schema_context(sorted(allowed))
        allow_score_comparison = bool(re.search(r"评分版本.{0,8}(?:对比|比较)|(?:对比|比较).{0,8}评分版本", question))
        attempts: list[dict[str, str]] = []
        previous_sql = ""
        last_error = ""
        for attempt in range(1, 4):
            generated = await self.model.generate_sql(
                question, context, previous_sql=previous_sql, error=last_error
            )
            sql = str(generated.get("sql", "")).strip()
            try:
                validated = validate_readonly_sql(
                    sql,
                    dialect=self.dialect,
                    allowed_tables=allowed,
                    max_rows=self.max_rows,
                    allow_score_version_comparison=allow_score_comparison,
                )
                rows = await self.runner.fetch(validated, max_rows=self.max_rows, timeout_ms=self.timeout_ms)
                return {
                    "sql": validated,
                    "reason": str(generated.get("reason", "")),
                    "attempts": [{"attempt": item["attempt"], "status": "error", "error": item["error"]} for item in attempts]
                    + [{"attempt": str(attempt), "status": "success"}],
                    "rows": list(rows),
                    "source": "POSTGRES_READ_ONLY",
                }
            except Exception as error:
                previous_sql = sql
                last_error = " ".join(str(error).split())[:1200]
                attempts.append({"attempt": str(attempt), "error": last_error})
                if attempt == 3:
                    raise RuntimeError(f"BI SQL failed after 3 attempts: {last_error}") from error
        raise AssertionError("unreachable SQL retry state")


def validate_readonly_sql(
    query: str,
    *,
    dialect: str = "postgres",
    allowed_tables: Sequence[str] = (),
    max_rows: int = MAX_SQL_ROWS,
    allow_score_version_comparison: bool = False,
) -> str:
    if not query.strip():
        raise SqlValidationError("SQL query is required")
    if not 1 <= max_rows <= MAX_SQL_ROWS:
        raise SqlValidationError(f"max_rows must be between 1 and {MAX_SQL_ROWS}")
    permitted = set(allowed_tables) & PRODUCT_RADAR_TABLES
    if not permitted:
        raise SqlValidationError("no BI tables are authorized for this principal")
    try:
        statements = sqlglot.parse(query, read=dialect, error_level="RAISE")
    except ParseError as error:
        raise SqlValidationError(f"SQL parse failed: {error}") from error
    if len(statements) != 1 or statements[0] is None:
        raise SqlValidationError("exactly one SQL statement is allowed")
    tree = statements[0]
    if not isinstance(tree, exp.Query):
        raise SqlValidationError("only SELECT queries are allowed")
    if any(isinstance(node, FORBIDDEN_NODES) for node in tree.walk()):
        raise SqlValidationError("read-only SELECT statements only")
    if tree.find(exp.Into):
        raise SqlValidationError("SELECT INTO is not allowed")
    if any(_is_unauthorized_wildcard(star) for star in tree.find_all(exp.Star)):
        raise SqlValidationError("SELECT * is not allowed; name the required columns")
    for function in tree.find_all(exp.Func):
        name = (function.name if isinstance(function, exp.Anonymous) else function.sql_name()).casefold()
        if name in FORBIDDEN_FUNCTIONS:
            raise SqlValidationError(f"SQL function {name} is not allowed")
        if isinstance(function, exp.Anonymous) and name not in ALLOWED_ANONYMOUS_FUNCTIONS:
            raise SqlValidationError(f"SQL function {name} is not in the analytics function allowlist")
    table_nodes = [source for scope in traverse_scope(tree) for source in scope.sources.values() if isinstance(source, exp.Table)]
    tables = {table.name.casefold() for table in table_nodes}
    if not tables:
        raise SqlValidationError("SQL must reference an authorized business table")
    if any(table.catalog or table.db not in {"", "public"} for table in table_nodes):
        raise SqlValidationError("only the configured public schema is allowed")
    unauthorized = tables - permitted
    if unauthorized:
        raise SqlValidationError(f"SQL references unauthorized tables: {', '.join(sorted(unauthorized))}")
    if "market_radar_metrics" in tables and not allow_score_version_comparison and not _has_default_score_filter(tree):
        raise SqlValidationError(f"market_radar_metrics queries must filter score_version = '{DEFAULT_SCORE_VERSION}'")
    tree = _enforce_limit(tree, min(max_rows, MAX_SQL_ROWS))
    tree = _normalize_postgres_round(tree, dialect)
    return tree.sql(dialect=dialect)


def _is_unauthorized_wildcard(star: exp.Star) -> bool:
    parent = star.parent
    return not isinstance(parent, exp.Count)


def _has_default_score_filter(tree: exp.Expression) -> bool:
    scoped_queries: list[tuple[exp.Select, list[exp.Table]]] = []
    for query in tree.find_all(exp.Select):
        local_tables = [table for table in query.find_all(exp.Table) if table.find_ancestor(exp.Select) is query]
        metrics_tables = [table for table in local_tables if table.name.casefold() == "market_radar_metrics"]
        if metrics_tables:
            scoped_queries.append((query, metrics_tables))
    if not scoped_queries:
        return True
    for query, metrics_tables in scoped_queries:
        where = query.args.get("where")
        if where is None:
            return False
        conjuncts = list(_conjuncts(where.this))
        for table in metrics_tables:
            aliases = {table.alias_or_name.casefold(), table.name.casefold()}
            if not any(
                _is_default_score_equality(term, aliases, allow_unqualified=len(metrics_tables) == 1)
                for term in conjuncts
                if isinstance(term, exp.EQ)
            ):
                return False
    return True


def _conjuncts(expression: exp.Expression):
    if isinstance(expression, exp.Paren):
        yield from _conjuncts(expression.this)
    elif isinstance(expression, exp.And):
        yield from _conjuncts(expression.this)
        yield from _conjuncts(expression.expression)
    else:
        yield expression


def _is_default_score_equality(node: exp.EQ, aliases: set[str], *, allow_unqualified: bool) -> bool:
    for column in node.find_all(exp.Column):
        if column.name.casefold() != "score_version":
            continue
        qualifier = column.table.casefold()
        if qualifier and qualifier not in aliases:
            continue
        if not qualifier and not allow_unqualified:
            continue
        other = node.expression if node.this is column else node.this
        return isinstance(other, exp.Literal) and not other.is_number and other.this == DEFAULT_SCORE_VERSION
    return False


def _enforce_limit(tree: exp.Query, max_rows: int) -> exp.Query:
    limit = tree.args.get("limit")
    if limit is None:
        return tree.limit(min(DEFAULT_SQL_ROWS, max_rows))
    amount = limit.expression
    if not isinstance(amount, exp.Literal) or not amount.is_int:
        raise SqlValidationError("LIMIT must be a positive integer literal")
    value = int(amount.this)
    if value < 1:
        raise SqlValidationError("LIMIT must be positive")
    if value > max_rows:
        raise SqlValidationError(f"LIMIT must not exceed {max_rows}")
    return tree


def _normalize_postgres_round(tree: exp.Query, dialect: str) -> exp.Query:
    if dialect.casefold() not in {"postgres", "postgresql"}:
        return tree
    for call in tree.find_all(exp.Round):
        precision = call.args.get("decimals")
        if precision is None:
            continue
        value = call.this
        if isinstance(value, exp.Cast) and value.args.get("to") and value.args["to"].this == exp.DataType.Type.NUMERIC:
            continue
        call.set("this", exp.Cast(this=value.copy(), to=exp.DataType.build("numeric")))
    return tree


def _schema_context(tables: Sequence[str]) -> str:
    columns = {
        "market_products": "id, region, title, brand_name, canonical_category_id, status, first_seen_at, last_seen_at",
        "market_product_daily_stats": "product_id, region, stat_date, canonical_category_id, price_avg, sold_count, sale_amount, day7_sold_count, day7_sale_amount, growth_rate_7d, growth_rate_30d",
        "market_radar_metrics": "product_id, region, stat_date, score_version, growth_score, gmv_score, creator_score, content_score, competition_score, price_score, trend_score, quality_score, risk_score, opportunity_score, risk_factors",
        "market_canonical_categories": "id, parent_id, code, name, level, status",
        "market_source_categories": "id, source, external_category_id, external_parent_id, parent_id, name, level, rank, raw_payload, first_seen_at, last_seen_at",
    }
    return json.dumps({"tables": {name: columns[name] for name in tables}, "maxRows": MAX_SQL_ROWS}, ensure_ascii=False)

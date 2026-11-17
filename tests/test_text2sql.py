import unittest

from flmh_agent.adapters.outbound.sql_permissions import ConfiguredSqlPermissionPolicy
from flmh_agent.application.text2sql import (
    PRODUCT_RADAR_TABLES,
    SqlValidationError,
    TextToSqlService,
    validate_readonly_sql,
)
from flmh_agent.config import Settings


class SqlValidationTests(unittest.TestCase):
    def test_join_and_cte_are_allowed_for_authorized_prisma_tables(self):
        query = """
            WITH latest AS (
              SELECT p.id, s.day7_sale_amount
              FROM market_products p
              JOIN market_product_daily_stats s ON s.product_id = p.id
              WHERE s.region = 'US'
              ORDER BY s.stat_date DESC
            )
            SELECT id, day7_sale_amount FROM latest
        """
        normalized = validate_readonly_sql(
            query,
            allowed_tables=["market_products", "market_product_daily_stats"],
        )
        self.assertIn("JOIN market_product_daily_stats", normalized)
        self.assertIn("LIMIT 20", normalized)

    def test_table_allowlist_is_fail_closed_and_rejects_schema_qualification(self):
        with self.assertRaisesRegex(SqlValidationError, "no BI tables"):
            validate_readonly_sql("SELECT id FROM market_products")
        with self.assertRaisesRegex(SqlValidationError, "unauthorized"):
            validate_readonly_sql("SELECT id FROM users", allowed_tables=PRODUCT_RADAR_TABLES)
        with self.assertRaisesRegex(SqlValidationError, "configured public schema"):
            validate_readonly_sql(
                "SELECT id FROM private.market_products", allowed_tables=PRODUCT_RADAR_TABLES
            )
        self.assertIn("public.market_products", validate_readonly_sql(
            "SELECT id FROM public.market_products", allowed_tables=PRODUCT_RADAR_TABLES
        ))

    def test_market_radar_requires_v2_filter_in_each_cte_branch(self):
        allowed = ["market_radar_metrics"]
        valid = "WITH m AS (SELECT product_id FROM market_radar_metrics WHERE score_version = 'v2') SELECT product_id FROM m"
        self.assertIn("score_version = 'v2'", validate_readonly_sql(valid, allowed_tables=allowed))
        invalid = "WITH m AS (SELECT product_id FROM market_radar_metrics) SELECT product_id FROM m"
        with self.assertRaisesRegex(SqlValidationError, "score_version"):
            validate_readonly_sql(invalid, allowed_tables=allowed)

    def test_mutation_multi_statement_wildcard_functions_and_limits_are_rejected(self):
        allowed = ["market_products"]
        invalid = [
            "DELETE FROM market_products",
            "SELECT id FROM market_products; SELECT id FROM market_products",
            "SELECT * FROM market_products",
            "SELECT pg_sleep(20) FROM market_products",
            "SELECT custom_side_effect(id) FROM market_products",
            "SELECT id FROM market_products LIMIT 51",
            "SELECT id INTO temp_table FROM market_products",
        ]
        for query in invalid:
            with self.subTest(query=query), self.assertRaises(SqlValidationError):
                validate_readonly_sql(query, allowed_tables=allowed)

    def test_score_version_filter_must_hold_for_all_metrics_branches(self):
        query = """
          WITH a AS (SELECT product_id FROM market_radar_metrics WHERE score_version = 'v2'),
               b AS (SELECT product_id FROM market_radar_metrics)
          SELECT a.product_id FROM a JOIN b ON b.product_id = a.product_id
        """
        with self.assertRaisesRegex(SqlValidationError, "score_version"):
            validate_readonly_sql(query, allowed_tables=["market_radar_metrics"])

    def test_score_version_filter_cannot_be_bypassed_with_or_or_wrong_alias(self):
        invalid = [
            "SELECT product_id FROM market_radar_metrics WHERE score_version = 'v2' OR 1 = 1",
            "SELECT m.product_id FROM market_radar_metrics m JOIN market_radar_metrics n ON n.product_id=m.product_id WHERE m.score_version = 'v2'",
            "SELECT product_id FROM market_radar_metrics WHERE 'v2' = COALESCE(score_version, 'v2')",
        ]
        for query in invalid:
            with self.subTest(query=query), self.assertRaisesRegex(SqlValidationError, "score_version"):
                validate_readonly_sql(query, allowed_tables=["market_radar_metrics"])

    def test_round_is_cast_to_numeric_for_postgres_precision_overload(self):
        result = validate_readonly_sql(
            "SELECT round(opportunity_score, 2) FROM market_radar_metrics WHERE score_version = 'v2'",
            allowed_tables=["market_radar_metrics"],
        )
        self.assertIn("ROUND(CAST(OPPORTUNITY_SCORE AS DECIMAL), 2)", result.upper())


class PermissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_role_permissions_are_intersected_with_fixed_table_allowlist(self):
        settings = Settings(
            bi_sql_role_tables_json='{"USER":["market_products","users"]}',
            bi_database_uri="postgresql://user:pass@localhost/agent",
        )
        with self.assertRaisesRegex(ValueError, "unsupported tables"):
            ConfiguredSqlPermissionPolicy(settings)

    async def test_verified_role_permissions_and_unknown_roles_fail_closed(self):
        settings = Settings(
            bi_sql_role_tables_json='{"USER":["market_products"],"ADMIN":["market_products","market_product_daily_stats"]}',
            bi_database_uri="postgresql://user:pass@localhost/agent",
        )
        policy = ConfiguredSqlPermissionPolicy(settings)
        self.assertEqual(await policy.allowed_tables("USER"), frozenset({"market_products"}))
        self.assertEqual(await policy.allowed_tables("ADMIN"), frozenset({"market_products", "market_product_daily_stats"}))
        service = TextToSqlService(permissions=policy, model=None, runner=None)
        with self.assertRaisesRegex(PermissionError, "no BI table permissions"):
            await service.query("查商品", "unknown")

    async def test_arbitrary_or_legacy_shared_roles_cannot_be_configured(self):
        settings = Settings(bi_sql_role_tables_json='{"analyst":["market_products"]}',
                            bi_database_uri="postgresql://user:pass@localhost/agent")
        with self.assertRaisesRegex(ValueError, "verified USER"):
            ConfiguredSqlPermissionPolicy(settings)

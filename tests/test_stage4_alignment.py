import json
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock

from langgraph.checkpoint.memory import InMemorySaver
from starlette.testclient import TestClient

from flmh_agent.adapters.outbound.coordination import InMemoryRunCoordinator
from flmh_agent.adapters.outbound.mysql_sql import AsyncMysqlReadOnlyRunner
from flmh_agent.adapters.outbound.sessions import InMemorySessionDirectory
from flmh_agent.application.rag import LocalCorpusFallbackKnowledgeService
from flmh_agent.application.service import AgentApplication
from flmh_agent.application.text2sql import SqlValidationError, validate_readonly_sql
from flmh_agent.config import Settings
from flmh_agent.domain.planning import RuleBasedPlanner
from flmh_agent.domain.slots import invalidated_conclusions
from identity_support import FixtureIdentityVerifier, IdentityTestCase


class _TraceFactory:
    def create(self):
        return None


class _FakeTools:
    def __init__(self) -> None:
        self.search_calls: list[dict] = []
        self.trend_calls: list[tuple[str, int]] = []
        self.creator_calls: list[tuple[str, int]] = []
        self.quote_calls: list[dict] = []
        self.media_quote_calls: list[dict] = []
        self.submit_calls: list[dict] = []

    async def search_products(self, query):
        self.search_calls.append(dict(query))
        return {
            "source": "LOCAL_FIXTURE",
            "products": [
                {
                    "id": f"prod-{query['region'].lower()}-1",
                    "title": f"{query.get('keyword') or '旅行杯'}",
                    "region": query["region"],
                    "price": 19.9,
                    "currency": "USD",
                    "source": "LOCAL_FIXTURE",
                    "sourceRef": "fixture:1",
                }
            ],
        }

    async def get_product_trend(self, product_id: str, *, days: int = 30):
        self.trend_calls.append((product_id, days))
        return {"productId": product_id, "days": days, "points": [{"statDate": "2026-11-20", "sales7d": 120}]}

    async def get_product_creators(self, product_id: str, *, limit: int = 20):
        self.creator_calls.append((product_id, limit))
        return {"productId": product_id, "creators": [{"creatorId": "cr-1", "handle": "@outdoor_pro"}]}

    async def create_quote(self, request):
        self.quote_calls.append(dict(request))
        return {"quoteId": f"q-{len(self.quote_calls)}", "totalAmount": 5.0, "currency": "USD"}

    async def confirm_quote(self, quote_id: str):
        return {"quoteId": quote_id, "status": "CONFIRMED", "confirmed": True}

    async def submit_task(self, request, idempotency_key: str):
        self.submit_calls.append(dict(request))
        return {"taskId": f"t-{len(self.submit_calls)}", "status": "SUCCEEDED", "generatedContent": "OK"}

    async def get_task(self, task_id: str):
        return {"taskId": task_id, "status": "SUCCEEDED", "generatedContent": "已生成的带货脚本"}

    async def create_media_quote(self, request):
        self.media_quote_calls.append(dict(request))
        return {"quoteId": f"mq-{len(self.media_quote_calls)}", "totalAmount": 6.0, "currency": "USD"}


class Stage4AlignmentTests(IdentityTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.tools = _FakeTools()

        @asynccontextmanager
        async def factory():
            yield self.tools

        self.app = AgentApplication(
            settings=Settings(agent_default_region="US"),
            planner=RuleBasedPlanner(default_region="US", max_queries=6),
            checkpointer=InMemorySaver(),
            tool_session_factory=factory,
            trace_factory=_TraceFactory(),
            run_coordinator=InMemoryRunCoordinator(),
            session_directory=InMemorySessionDirectory(),
        )

    async def test_target_language_slot_invalidation_and_parallel_trend_creator_dag(self):
        first = await self.app.start(
            "dag-lang-1",
            {"user_request": "查美国旅行保温杯并分析近30天走势和带货达人，写一份英文口播脚本，预算25"},
        )
        self.assertEqual(first["status"], "awaiting_confirmation")
        self.assertEqual(first["targetLanguage"], "en-US")
        self.assertEqual(first["executionBatches"], [["q_catalog"], ["q_trend", "q_creators"]])
        self.assertIn("prod-us-1", first["productTrends"])
        self.assertIn("prod-us-1", first["productCreators"])
        self.assertEqual(self.tools.trend_calls, [("prod-us-1", 30)])
        self.assertEqual(self.tools.creator_calls, [("prod-us-1", 20)])
        self.assertEqual(self.tools.quote_calls[0]["target_language"], "en-US")

        # Switching ONLY target_language preserves product_results/trends/creators without re-searching catalog
        switched = await self.app.revise(
            "dag-lang-1",
            {"expected_revision": 0, "user_request": "换成印尼语脚本"},
        )
        self.assertEqual(switched["slotValues"]["target_language"], "id-ID")
        self.assertEqual(switched["targetLanguage"], "id-ID")
        self.assertIn("content_brief", switched["invalidated"])
        self.assertNotIn("product_results", switched["invalidated"])
        self.assertNotIn("selected_product", switched["invalidated"])
        self.assertEqual(len(self.tools.search_calls), 1)
        self.assertEqual(self.tools.quote_calls[1]["target_language"], "id-ID")

    async def test_four_slot_checkpoint_upgrades_cleanly_to_five_slots(self):
        first = await self.app.start("legacy-4slot", {"user_request": "查美国保温杯写英文口播，预算15"})
        graph = self.app._graph(self.tools)
        config = self.app._config("legacy-4slot")
        old_slots = {k: v for k, v in first["slotValues"].items() if k != "target_language"}
        old_versions = {k: v for k, v in first["slotVersions"].items() if k != "target_language"}
        await graph.aupdate_state(
            config,
            {"slot_values": old_slots, "slot_versions": old_versions},
            as_node="create_quote",
        )
        upgraded = await self.app.observe("legacy-4slot")
        self.assertIn("target_language", upgraded["slotValues"])
        self.assertEqual(upgraded["slotVersions"]["target_language"], 0)
        self.assertEqual(upgraded["slotValues"]["target_language"], "en-US")

    async def test_script_run_handoffs_cleanly_to_new_media_production_run(self):
        script_run = await self.app.start("script-done", {"user_request": "查美国保温杯写英文口播，预算20"})
        await self.app.confirm("script-done", {"quote_id": script_run["quote"]["quoteId"], "confirmed": True})

        handed = await self.app.handoff_media(
            "script-done",
            {"aspect_ratio": "9:16", "duration_seconds": 8, "budget_max": 30, "new_run_id": "media-from-script"},
        )
        self.assertEqual(handed["sourceRunId"], "script-done")
        self.assertEqual(handed["runId"], "media-from-script")
        self.assertEqual(handed["status"], "awaiting_confirmation")
        self.assertEqual(handed["plan"]["content_type"], "product_media")
        self.assertEqual(len(self.tools.media_quote_calls), 1)
        self.assertEqual(self.tools.media_quote_calls[0]["duration_seconds"], 8)

    def test_mysql_text2sql_validation_and_runner(self):
        sql = validate_readonly_sql(
            "SELECT p.product_id, p.title, c.handle FROM flmh_product.product_master p "
            "JOIN product_creator_link l ON p.product_id = l.product_id "
            "JOIN creator_profile c ON l.creator_id = c.creator_id WHERE p.region = 'US'",
            dialect="mysql",
            allowed_tables=["product_master", "product_creator_link", "creator_profile"],
            max_rows=50,
        )
        self.assertIn("LIMIT 20", sql)

        for dangerous in (
            "SELECT SLEEP(5) FROM product_master LIMIT 1",
            "SELECT BENCHMARK(1000, MD5('a')) FROM product_master LIMIT 1",
            "SELECT * FROM product_master LIMIT 5",
        ):
            with self.subTest(dangerous=dangerous), self.assertRaises(SqlValidationError):
                validate_readonly_sql(dangerous, dialect="mysql", allowed_tables=["product_master"], max_rows=50)

    async def test_mysql_runner_enforces_readonly_and_max_execution_time(self):
        executed: list[str] = []

        class _FakeCursor:
            description = [("product_id",), ("title",)]

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def execute(self, sql):
                executed.append(sql)

            async def fetchall(self):
                return [("p-1", "Travel Mug")]

        class _FakeConn:
            def cursor(self, *_args):
                return _FakeCursor()

        class _ConnCtx:
            async def __aenter__(self):
                return _FakeConn()

            async def __aexit__(self, *args):
                return False

        class _FakePool:
            def acquire(self):
                return _ConnCtx()

        runner = AsyncMysqlReadOnlyRunner(Settings(sql_dialect="mysql"), pool=_FakePool())
        rows = await runner.fetch("SELECT product_id, title FROM product_master LIMIT 5", max_rows=10, timeout_ms=2500)
        self.assertEqual(rows, [{"product_id": "p-1", "title": "Travel Mug"}])
        self.assertEqual(executed[0], "SET SESSION TRANSACTION READ ONLY")
        self.assertEqual(executed[1], "SET SESSION MAX_EXECUTION_TIME = 2500")

    def test_rag_local_fallback_api_and_bi_validate_route(self):
        from flmh_agent.api import create_app

        # 1. Default Settings stays 503 for RAG (fail-closed)
        with TestClient(create_app(Settings(), identity_verifier=FixtureIdentityVerifier()), headers={"authorization": "Bearer unit-user"}) as client:
            self.assertEqual(client.post("/v1/rag/search", json={"query": "选品雷达"}).status_code, 503)

        # 2. Explicit opt-in enables local corpus fallback and /v1/bi/validate
        enabled = Settings(
            rag_local_corpus_fallback=True,
            sql_dialect="mysql",
            bi_database_uri="mysql://reader:secret@127.0.0.1:3306/flmh_product",
            bi_sql_role_tables_json=json.dumps({"USER": ["product_master", "creator_profile"]}),
        )
        with TestClient(create_app(enabled, identity_verifier=FixtureIdentityVerifier()), headers={"authorization": "Bearer unit-user"}) as client:
            rag_resp = client.post("/v1/rag/search", json={"query": "选品雷达", "limit": 3})
            self.assertEqual(rag_resp.status_code, 200)
            body = rag_resp.json()
            self.assertEqual(body["source"], "LOCAL_CORPUS_FALLBACK")
            self.assertGreaterEqual(len(body["chunks"]), 1)

            val_ok = client.post("/v1/bi/validate", json={"sql": "SELECT product_id, title FROM product_master LIMIT 10"})
            self.assertEqual(val_ok.status_code, 200)
            self.assertTrue(val_ok.json()["valid"])

            val_bad = client.post("/v1/bi/validate", json={"sql": "SELECT SLEEP(1) FROM product_master LIMIT 1"})
            self.assertEqual(val_bad.status_code, 422)
            self.assertFalse(val_bad.json()["valid"])

    def test_extended_dag_fixture_exists_and_is_valid(self):
        path = Path(__file__).resolve().parents[1] / "examples" / "agent-evaluation" / "planning_dag_extended.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(data["tasks"]), 8)


if __name__ == "__main__":
    unittest.main()

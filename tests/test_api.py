import unittest

from starlette.testclient import TestClient

from flmh_agent.api import create_app
from flmh_agent.config import Settings


class ApiTests(unittest.TestCase):
    def test_workbench_entry_and_assets_support_get_and_head(self):
        with TestClient(create_app(Settings())) as client:
            page = client.get("/")
            self.assertEqual(page.status_code, 200)
            self.assertIn("text/html", page.headers["content-type"])
            self.assertIn("FLMH", page.text)

            page_head = client.head("/")
            self.assertEqual(page_head.status_code, 200)
            self.assertEqual(page_head.content, b"")

            asset = client.get("/assets/app.js")
            self.assertEqual(asset.status_code, 200)
            self.assertIn("text/javascript", asset.headers["content-type"])
            self.assertIn("use strict", asset.text)

            asset_head = client.head("/assets/app.css")
            self.assertEqual(asset_head.status_code, 200)
            self.assertEqual(asset_head.content, b"")

    def test_unknown_workbench_asset_returns_not_found(self):
        with TestClient(create_app(Settings())) as client:
            response = client.get("/assets/missing.css")
            self.assertEqual(response.status_code, 404)

    def test_api_key_keeps_workbench_public_and_agent_routes_protected(self):
        with TestClient(create_app(Settings(agent_api_key="agent-secret"))) as client:
            self.assertEqual(client.get("/").status_code, 200)
            self.assertEqual(client.head("/").status_code, 200)
            self.assertEqual(client.get("/assets/app.css").status_code, 200)
            self.assertEqual(client.head("/assets/app.js").status_code, 200)

            self.assertEqual(client.get("/v1/agent/runs/missing").status_code, 401)
            self.assertEqual(
                client.get(
                    "/v1/agent/runs/missing",
                    headers={"authorization": "Bearer agent-secret"},
                ).status_code,
                404,
            )

    def test_lifespan_starts_without_opening_external_services(self):
        with TestClient(create_app(Settings())) as client:
            self.assertEqual(client.get("/health/live").status_code, 200)
            ready = client.get("/health/ready")
            self.assertEqual(ready.status_code, 200)
            self.assertEqual(ready.json()["adapters"]["planner"], "rules")

    def test_json_and_validation_errors_return_client_error_responses(self):
        with TestClient(create_app(Settings())) as client:
            invalid = client.post("/v1/agent/runs", content="{", headers={"content-type": "application/json"})
            self.assertEqual(invalid.status_code, 400)
            bad_request = client.post("/v1/agent/runs", json={"user_request": ""})
            self.assertEqual(bad_request.status_code, 422)
            oversized = client.post("/v1/agent/runs", content="x" * (65536 + 1))
            self.assertEqual(oversized.status_code, 413)

    def test_api_key_protects_agent_routes_but_health_is_public(self):
        with TestClient(create_app(Settings(agent_api_key="agent-secret"))) as client:
            self.assertEqual(client.get("/health/live").status_code, 200)
            self.assertEqual(client.get("/v1/agent/runs/missing").status_code, 401)
            self.assertEqual(client.get("/v1/agent/runs/missing", headers={"authorization": "Bearer agent-secret"}).status_code, 404)

    def test_unconfigured_bi_and_rag_are_explicitly_disabled(self):
        with TestClient(create_app(Settings())) as client:
            self.assertEqual(client.post("/v1/bi/queries", json={"question": "查询商品"}).status_code, 503)
            self.assertEqual(client.post("/v1/rag/search", json={"query": "query"}).status_code, 503)

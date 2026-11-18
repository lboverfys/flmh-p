import unittest

from starlette.testclient import TestClient

from flmh_agent.api import create_app
from flmh_agent.config import Settings
from identity_support import FixtureIdentityVerifier


class ApiTests(unittest.TestCase):
    def test_api_does_not_serve_workbench_pages_or_assets(self):
        with TestClient(create_app(Settings(), identity_verifier=FixtureIdentityVerifier()), headers={"authorization": "Bearer unit-user"}) as client:
            for path in ("/", "/assets/app.js", "/assets/app.css"):
                self.assertEqual(client.get(path).status_code, 404, path)

    def test_lifespan_starts_without_opening_external_services(self):
        with TestClient(create_app(Settings(), identity_verifier=FixtureIdentityVerifier())) as client:
            self.assertEqual(client.get("/health/live").status_code, 200)
            ready = client.get("/health/ready")
            self.assertEqual(ready.status_code, 200)
            self.assertEqual(ready.json()["adapters"]["planner"], "rules")

    def test_json_and_validation_errors_return_client_error_responses(self):
        with TestClient(create_app(Settings(), identity_verifier=FixtureIdentityVerifier()), headers={"authorization": "Bearer unit-user"}) as client:
            invalid = client.post("/v1/agent/runs", content="{", headers={"content-type": "application/json"})
            self.assertEqual(invalid.status_code, 400)
            bad_request = client.post("/v1/agent/runs", json={"user_request": ""})
            self.assertEqual(bad_request.status_code, 422)
            oversized = client.post("/v1/agent/runs", content="x" * (65536 + 1))
            self.assertEqual(oversized.status_code, 413)

    def test_api_key_protects_agent_routes_but_health_is_public(self):
        with TestClient(create_app(Settings(agent_api_key="agent-secret"), identity_verifier=FixtureIdentityVerifier())) as client:
            self.assertEqual(client.get("/health/live").status_code, 200)
            self.assertEqual(client.get("/v1/agent/runs/missing").status_code, 401)
            self.assertEqual(client.get("/v1/agent/runs/missing", headers={"authorization": "Bearer unit-user", "x-agent-key": "agent-secret"}).status_code, 404)

    def test_unconfigured_bi_and_rag_are_explicitly_disabled(self):
        with TestClient(create_app(Settings(), identity_verifier=FixtureIdentityVerifier()), headers={"authorization": "Bearer unit-user"}) as client:
            self.assertEqual(client.post("/v1/bi/queries", json={"question": "查询商品"}).status_code, 503)
            self.assertEqual(client.post("/v1/rag/search", json={"query": "query"}).status_code, 503)

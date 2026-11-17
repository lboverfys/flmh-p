import json
import unittest

import httpx
from starlette.testclient import TestClient

from flmh_agent.adapters.outbound.workflows import JavaWorkflowClient, WorkflowRequestError
from flmh_agent.api import create_app
from flmh_agent.config import Settings
from flmh_agent.domain.identity import AuthenticationError, UserIdentity


GRAPH = {"nodes": [{"key": "image", "capability": "gemini:image", "prompt": "商品主图",
                    "params": {"aspectRatio": "9:16"}, "assets": []}], "edges": []}
DETAIL = {"id": "w-1", "name": "商品素材", "description": None, "version": 1, "definition": GRAPH,
          "definitionFingerprint": "fingerprint", "createdAt": "2026-11-17T00:00:00Z", "updatedAt": "2026-11-17T00:00:00Z",
          "executability": {"demoMode": "MOCK_ONLY", "demoSupported": True, "demoProducesAssets": False,
                            "realAdapterSupported": True, "nodes": [{"nodeKey": "image", "realAdapterSupported": True, "reasons": []}],
                            "configurationTodos": ["TODO: provider configuration"]}}
SNAPSHOT = {"workflowId": "w-1", "name": "商品素材", "definitionVersion": 1, "definitionFingerprint": "fp", "definition": GRAPH,
            "productId": "p-1", "executionMode": "MOCK_ONLY", "configurationFingerprint": "local", "nodeFingerprints": {"image": "fp"},
            "nodePrices": {"image": 0}, "reusedNodes": [], "originRunId": None}
RUN = {"runId": "r-1", "quoteId": "q-1", "status": "RUNNING", "cancelRequested": False, "snapshot": SNAPSHOT,
       "nodes": [{"nodeKey": "image", "status": "QUEUED", "attempt": 0, "requestId": "r-1:image:0", "inputFingerprint": "fp", "reused": False}],
       "artifacts": [], "createdAt": DETAIL["createdAt"], "updatedAt": DETAIL["updatedAt"], "idempotentReplay": False}


class Verifier:
    async def authenticate(self, authorization):
        if authorization != "Bearer session":
            raise AuthenticationError("invalid")
        return UserIdentity("alice", "USER", "LOCAL_MOCK", authorization)


class Workflows:
    def __init__(self):
        self.calls = []
        self.result = DETAIL

    async def request(self, method, path, identity, **kwargs):
        self.calls.append((method, path, identity, kwargs))
        return self.result


class WorkflowApiTests(unittest.TestCase):
    def setUp(self):
        self.workflows = Workflows()
        self.client = self.enterContext(TestClient(create_app(Settings(), workflows=self.workflows, identity_verifier=Verifier())))
        self.headers = {"Authorization": "Bearer session"}

    def test_independent_portrait_keeps_typed_source_and_revision_cannot_replace_it(self):
        source = {"originalAssetId": "1429c1dd-3b99-4f46-8fd6-89fa04d29d74", "aspectRatio": "3:4"}
        settings = {"face": {"gender": "auto", "hairStyle": "short"}, "weights": {"faceWeight": 80, "hairWeight": 50, "backgroundWeight": 30}}
        inputs = {**settings, "budgetMax": "10", "currency": "USD"}
        snapshot = {**SNAPSHOT, "productId": None, "imageAdaptation": {**source, **settings}}
        quote = {"quoteId": "q-1", "status": "PENDING_CONFIRMATION", "amount": "0", "currency": "USD", "budgetMax": "10", "pricingSource": "LOCAL_ZERO_COST", "snapshot": snapshot, "createdAt": RUN["createdAt"]}
        self.workflows.result = quote
        base = "/v1/image-adaptations/quotes"
        self.assertEqual(self.client.post(base, json={**source, **inputs}).status_code, 401)
        response = self.client.post(base, headers=self.headers, json={**source, **inputs})
        self.assertEqual(response.status_code, 200); self.assertIsNone(response.json()["snapshot"]["productId"])
        self.assertEqual(self.workflows.calls[-1][2].user_id, "alice")
        for bad in (source, {"mask": "external"}, {"productId": "p"}, {"weights": {**settings["weights"], "faceWeight": 2.5}}):
            with self.subTest(bad=bad):
                self.assertEqual(self.client.post(base+"/q-1/revisions", headers=self.headers, json={**inputs, **bad}).status_code, 422)
        self.assertEqual(len(self.workflows.calls), 1)
        self.assertEqual(self.client.post(base+"/q-1/revisions", headers=self.headers, json=inputs).status_code, 200)
        self.workflows.result = {"quote": quote, "nextQuoteId": "q-2", "runId": None, "ownerId": "private"}
        restored = self.client.get(base+"/q-1",headers=self.headers)
        self.assertEqual(restored.json()["nextQuoteId"], "q-2"); self.assertNotIn("ownerId",restored.json())
        self.workflows.result = {**quote, "snapshot": {**snapshot, "imageAdaptation": None}}
        self.assertEqual(self.client.post(base, headers=self.headers, json={**source, **inputs}).status_code, 502)

    def test_definition_uses_verified_identity_and_strips_upstream_private_fields(self):
        self.workflows.result = {**DETAIL, "ownerId": "private-owner", "secret": "do-not-return"}
        response = self.client.post("/v1/workflows", headers={**self.headers, "X-User-Id": "bob"},
                                    json={"name": "商品素材", "definition": GRAPH})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.workflows.calls[0][2].user_id, "alice")
        self.assertNotIn("private-owner", response.text)
        self.assertNotIn("do-not-return", response.text)
        self.assertEqual(response.headers["cache-control"], "private, no-store")
        self.assertEqual(self.client.get("/v1/workflows").status_code, 401)

    def test_definition_unknown_fields_strict_versions_and_limits_fail_before_java(self):
        for payload in ({"name": "workflow", "definition": GRAPH, "ownerId": "bob"},
                        {"name": "workflow", "definition": {"nodes": GRAPH["nodes"] * 21}},
                        {"name": "workflow", "definition": {"nodes": GRAPH["nodes"], "edges": [{"parent": "image", "child": "image", "order": True}]}}):
            self.assertEqual(self.client.post("/v1/workflows", headers=self.headers, json=payload).status_code, 422)
        self.assertEqual(self.client.put("/v1/workflows/w-1", headers=self.headers,
                                        json={"name": "workflow", "definition": GRAPH, "expectedVersion": True}).status_code, 422)
        self.assertEqual(self.workflows.calls, [])

    def test_paging_and_historical_version_paths_are_bounded(self):
        self.workflows.result = {"workflows": [DETAIL], "page": 1, "pageSize": 10, "total": 1, "totalPages": 1}
        self.assertEqual(self.client.get("/v1/workflows?pageSize=10", headers=self.headers).status_code, 200)
        self.assertEqual(self.workflows.calls[-1][3]["params"]["pageSize"], 10)
        for query in ("pageSize=51", "page=0", "ownerId=bob"):
            self.assertEqual(self.client.get("/v1/workflows?" + query, headers=self.headers).status_code, 422)
        self.workflows.result = DETAIL
        self.assertEqual(self.client.get("/v1/workflows/w-1/versions/1", headers=self.headers).status_code, 200)
        self.assertEqual(self.workflows.calls[-1][1], "/api/v1/content/workflows/w-1/versions/1")

    def test_update_passes_optimistic_version_without_automatically_retrying(self):
        self.workflows.result = {**DETAIL, "version": 2}
        response = self.client.put("/v1/workflows/w-1", headers=self.headers,
                                    json={"name": "商品素材", "definition": GRAPH, "expectedVersion": 1})
        self.assertEqual(response.json()["version"], 2)
        self.assertEqual(len(self.workflows.calls), 1)
        self.assertEqual(self.workflows.calls[0][3]["payload"]["expectedVersion"], 1)

    def test_quote_start_and_confirmation_are_separate_and_require_real_boolean(self):
        self.workflows.result = {"quoteId": "q-1", "status": "PENDING_CONFIRMATION", "amount": 0, "currency": "USD",
                                 "budgetMax": 10, "pricingSource": "LOCAL_ZERO_COST", "snapshot": SNAPSHOT, "createdAt": DETAIL["createdAt"]}
        quote = self.client.post("/v1/workflows/w-1/quotes", headers=self.headers,
                                 json={"definitionVersion": 1, "productId": "p-1", "budgetMax": "10"})
        self.assertEqual(quote.status_code, 200)
        self.assertEqual(len(self.workflows.calls), 1)
        self.assertEqual(self.client.post("/v1/workflow-quotes/q-1/confirmation", headers=self.headers, json={"confirmed": "true"}).status_code, 422)
        self.workflows.result = {"quoteId": "q-1", "status": "CONFIRMED"}
        self.assertEqual(self.client.post("/v1/workflow-quotes/q-1/confirmation", headers=self.headers, json={"confirmed": True}).status_code, 200)
        self.assertEqual(self.workflows.calls[-1][1], "/api/v1/content/quotes/q-1/confirm")
        self.workflows.result = RUN
        self.assertEqual(self.client.post("/v1/workflows/w-1/runs", headers=self.headers, json={"quoteId": "q-1"}).status_code, 400)
        response = self.client.post("/v1/workflows/w-1/runs", headers={**self.headers, "Idempotency-Key": "same-key"}, json={"quoteId": "q-1"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.workflows.calls[-1][3]["key"], "same-key")

    def test_only_supported_runtime_actions_are_forwarded_and_get_has_no_side_effect(self):
        self.workflows.result = RUN
        self.assertEqual(self.client.get("/v1/workflow-runs/r-1", headers=self.headers).status_code, 200)
        self.assertEqual(self.workflows.calls[-1][0], "GET")
        for action in ("advance", "verify", "cancel"):
            self.assertEqual(self.client.post(f"/v1/workflow-runs/r-1/{action}", headers=self.headers).status_code, 200)
        count = len(self.workflows.calls)
        self.assertEqual(self.client.post("/v1/workflow-runs/r-1/advance", headers=self.headers, json={"owner": "bob"}).status_code, 400)
        self.assertEqual(self.client.post("/v1/workflow-runs/r-1/resubmit", headers=self.headers).status_code, 404)
        self.assertEqual(len(self.workflows.calls), count)

    def test_invalid_response_is_explicit_failure_not_empty_success(self):
        self.workflows.result = {"id": "partial"}
        response = self.client.get("/v1/workflows/w-1", headers=self.headers)
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"], "invalid_workflow_response")


class WorkflowTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_forwards_jwt_service_key_and_idempotency_once(self):
        requests = []

        async def respond(request):
            requests.append(request)
            return httpx.Response(200, json={"result": "fixture"})

        async with httpx.AsyncClient(base_url="http://127.0.0.1:8080", transport=httpx.MockTransport(respond)) as client:
            result = await JavaWorkflowClient(Settings(gateway_api_key="service-key"), client).request("POST", "/api/v1/content/workflows/w/runs",
                         UserIdentity("alice", "USER", "LOCAL_MOCK", "Bearer session"), payload={"quoteId": "q"}, key="same")
        self.assertEqual(result, {"result": "fixture"})
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].headers["Authorization"], "Bearer session")
        self.assertEqual(requests[0].headers["X-Service-Key"], "service-key")
        self.assertEqual(requests[0].headers["Idempotency-Key"], "same")
        self.assertNotIn("alice", requests[0].content.decode())

    async def test_errors_and_redirects_do_not_retry_or_expose_upstream_secrets(self):
        for code in (302, 409, 503):
            requests = []

            async def respond(request):
                requests.append(request)
                return httpx.Response(code, headers={"Location": "https://never.invalid"}, json={"secret": "do-not-expose"})

            async with httpx.AsyncClient(base_url="http://127.0.0.1", transport=httpx.MockTransport(respond)) as client:
                with self.assertRaises(WorkflowRequestError) as error:
                    await JavaWorkflowClient(Settings(), client).request("POST", "/api/v1/content/workflow-runs/r/advance",
                               UserIdentity("alice", "USER", "LOCAL_MOCK", "Bearer session"))
            self.assertNotIn("do-not-expose", str(error.exception))
            self.assertEqual(len(requests), 1)

    async def test_oversized_or_non_object_response_fails(self):
        for body in (b"[]", b" " * (4 * 1024 * 1024 + 1)):
            async with httpx.AsyncClient(base_url="http://127.0.0.1", transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body))) as client:
                with self.assertRaises(WorkflowRequestError):
                    await JavaWorkflowClient(Settings(), client).request("GET", "/api/v1/content/workflows",
                               UserIdentity("alice", "USER", "LOCAL_MOCK", "Bearer session"))


if __name__ == "__main__":
    unittest.main()

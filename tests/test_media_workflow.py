from __future__ import annotations

import json
from types import SimpleNamespace
import unittest
from identity_support import IdentityTestCase
from unittest.mock import AsyncMock, patch

import httpx

from flmh_agent.adapters.inbound.mcp_tools import create_mcp_server
from flmh_agent.adapters.outbound.java_gateway import JavaGatewayClient
from flmh_agent.adapters.outbound.mcp_client import McpProductContentAdapter
from flmh_agent.config import Settings
from flmh_agent.application.media_workflow import build_media_workflow


class MediaWorkflowAdapterTests(IdentityTestCase):
    async def test_java_gateway_start_uses_stable_idempotency_header_and_local_route(self):
        requests = []

        async def handler(request: httpx.Request):
            requests.append(request)
            return httpx.Response(200, json={"runId": "run-1", "status": "QUEUED", "executionMode": "MOCK_ONLY"})

        async with httpx.AsyncClient(base_url="http://gateway", transport=httpx.MockTransport(handler)) as client:
            result = await JavaGatewayClient(Settings(), client).start_media_workflow(
                {"product_id": "camping-lamp-001", "prompt": "lamp media", "mock_execution_confirmed": True}, "run-key-1"
            )

        self.assertEqual(result["status"], "QUEUED")
        self.assertEqual(requests[0].url.path, "/api/v1/content/media-workflows/runs")
        self.assertEqual(requests[0].headers["Idempotency-Key"], "run-key-1")
        self.assertEqual(json.loads(requests[0].content), {
            "productId": "camping-lamp-001", "prompt": "lamp media", "mockExecutionConfirmed": True,
        })

    async def test_gateway_encodes_run_id_for_get_simulate_and_cancel(self):
        paths = []

        async def handler(request: httpx.Request):
            paths.append((request.method, request.url.raw_path.decode("ascii")))
            return httpx.Response(200, json={"runId": "run/a b", "status": "MOCKED", "artifacts": []})

        async with httpx.AsyncClient(base_url="http://gateway", transport=httpx.MockTransport(handler)) as client:
            gateway = JavaGatewayClient(Settings(), client)
            await gateway.get_media_workflow("run/a b")
            await gateway.simulate_media_workflow("run/a b")
            await gateway.cancel_media_workflow("run/a b")

        self.assertEqual(paths, [
            ("GET", "/api/v1/content/media-workflows/runs/run%2Fa%20b"),
            ("POST", "/api/v1/content/media-workflows/runs/run%2Fa%20b/simulate"),
            ("POST", "/api/v1/content/media-workflows/runs/run%2Fa%20b/cancel"),
        ])

    async def test_mcp_exposes_explicit_mock_start_and_simulation_tools(self):
        calls = []

        async def start(_gateway, request, idempotency_key):
            calls.append(("start", request, idempotency_key))
            return {"runId": "run-1", "status": "QUEUED", "executionMode": "MOCK_ONLY"}

        async def simulate(_gateway, run_id):
            calls.append(("simulate", run_id))
            return {"runId": run_id, "status": "MOCKED", "artifacts": []}

        with patch("flmh_agent.adapters.inbound.mcp_tools.JavaGatewayClient.start_media_workflow", start), \
             patch("flmh_agent.adapters.inbound.mcp_tools.JavaGatewayClient.simulate_media_workflow", simulate):
            server = create_mcp_server(Settings())
            created = await server.call_tool("media_workflow_start", {
                "product_id": "camping-lamp-001", "prompt": "make an image and short video", "idempotency_key": "key-1",
                "mock_execution_confirmed": True,
            })
            completed = await server.call_tool("media_workflow_simulate", {"run_id": "run-1"})

        self.assertEqual(calls[0], ("start", {
            "product_id": "camping-lamp-001", "prompt": "make an image and short video", "mock_execution_confirmed": True,
        }, "key-1"))
        self.assertEqual(calls[1], ("simulate", "run-1"))
        self.assertEqual(created[1]["status"], "QUEUED")
        self.assertEqual(completed[1]["artifacts"], [])

    async def test_mcp_client_calls_media_tools_without_claiming_real_assets(self):
        adapter = object.__new__(McpProductContentAdapter)
        adapter._session = None
        adapter._stack = None

        class FakeSession:
            async def call_tool(self, name, arguments):
                self.name = name
                self.arguments = arguments
                return type("Result", (), {"isError": False, "structuredContent": {
                    "executionMode": "MOCK_ONLY", "artifacts": [], "producesMediaAssets": False,
                }})()

        adapter._session = FakeSession()
        response = await adapter.start_media_workflow(
            {"product_id": "camping-lamp-001", "prompt": "media", "mock_execution_confirmed": True}, "media-key"
        )

        self.assertEqual(adapter._session.name, "media_workflow_start")
        self.assertEqual(adapter._session.arguments, {
            "product_id": "camping-lamp-001", "prompt": "media", "idempotency_key": "media-key", "mock_execution_confirmed": True,
        })
        self.assertFalse(response["producesMediaAssets"])
        self.assertEqual(response["artifacts"], [])

    async def test_mcp_rejects_missing_false_and_coerced_confirmation_before_calling_gateway(self):
        with patch("flmh_agent.adapters.inbound.mcp_tools.JavaGatewayClient.start_media_workflow", new_callable=AsyncMock) as start:
            server = create_mcp_server(Settings())
            base = {"product_id": "p-1", "prompt": "media", "idempotency_key": "key-1"}
            for extra in ({}, {"mock_execution_confirmed": False}, {"mock_execution_confirmed": "true"},
                          {"mock_execution_confirmed": 1}):
                with self.subTest(extra=extra), self.assertRaises(Exception):
                    await server.call_tool("media_workflow_start", {**base, **extra})
            start.assert_not_awaited()

    async def test_langgraph_media_flow_validates_confirmation_and_reports_empty_mock_result(self):
        result = {"runId": "run-1", "executionMode": "MOCK_ONLY", "status": "MOCKED", "artifacts": [],
                  "nodes": [{"nodeKey": key, "status": "MOCKED", "outputSnapshot": {
                      "artifactCreated": False, "outputUrl": None,
                  }} for key in ("image", "video")]}
        tools = SimpleNamespace(
            media_workflow_capabilities=AsyncMock(return_value={"executionMode": "MOCK_ONLY", "producesMediaAssets": False}),
            start_media_workflow=AsyncMock(return_value={"runId": "run-1", "executionMode": "MOCK_ONLY"}),
            simulate_media_workflow=AsyncMock(return_value=result),
            get_media_workflow=AsyncMock(return_value=result),
        )
        request = {"product_id": "p-1", "prompt": "media", "idempotency_key": "key-1", "mock_execution_confirmed": False}
        with self.assertRaisesRegex(ValueError, "confirmation"):
            await build_media_workflow(tools).ainvoke({"request": request})
        tools.start_media_workflow.assert_not_awaited()
        tools.media_workflow_capabilities.assert_not_awaited()

        completed = await build_media_workflow(tools).ainvoke({"request": {**request, "mock_execution_confirmed": True}})
        self.assertEqual(completed["result"], result)
        self.assertIn("未生成文件或 URL", completed["summary"])
        tools.get_media_workflow.assert_awaited_once_with("run-1")

    async def test_langgraph_refuses_to_describe_real_asset_or_missing_node_as_mock_success(self):
        base = {"runId": "run-1", "executionMode": "MOCK_ONLY", "status": "MOCKED", "artifacts": [],
                "nodes": [{"nodeKey": key, "status": "MOCKED", "outputSnapshot": {
                    "artifactCreated": False, "outputUrl": None,
                }} for key in ("image", "video")]}
        for invalid in ({**base, "artifacts": ["unverified"]}, {**base, "nodes": base["nodes"][:1]},
                        {**base, "executionMode": "PRODUCTION"}):
            tools = SimpleNamespace(
                media_workflow_capabilities=AsyncMock(return_value={"executionMode": "MOCK_ONLY", "producesMediaAssets": False}),
                start_media_workflow=AsyncMock(return_value={"runId": "run-1", "executionMode": "MOCK_ONLY"}),
                simulate_media_workflow=AsyncMock(return_value=invalid), get_media_workflow=AsyncMock(return_value=invalid),
            )
            with self.subTest(result=invalid), self.assertRaises(ValueError):
                await build_media_workflow(tools).ainvoke({"request": {
                    "product_id": "p-1", "prompt": "media", "idempotency_key": "key-1", "mock_execution_confirmed": True,
                }})


if __name__ == "__main__":
    unittest.main()

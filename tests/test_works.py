import json
import unittest
from contextlib import asynccontextmanager

import httpx
from starlette.testclient import TestClient

from flmh_agent.adapters.inbound.works import WorkFileResponse
from flmh_agent.adapters.outbound.works import JavaWorkCatalog, MAX_MEDIA_BYTES, WorkRequestError
from flmh_agent.api import create_app
from flmh_agent.config import Settings
from flmh_agent.domain.identity import AuthenticationError, UserIdentity


WORK = {"id": "work-1", "module": "notes", "title": "用户文字", "type": "report", "content": "保存的正文",
        "mediaUrl": None, "provider": None, "source": "USER_SAVED", "createdAt": "2026-11-17T00:00:00Z", "lineageType": None}
PAGE = {"records": [WORK], "total": 1, "page": 1, "pageSize": 20, "totalPages": 1,
        "availableModules": ["notes"], "availableModulesTruncated": False}
IDENTITY = UserIdentity("alice", "USER", "LOCAL_MOCK", "Bearer alice")


class Verifier:
    async def authenticate(self, authorization):
        if authorization not in {"Bearer alice", "Bearer bob"}:
            raise AuthenticationError("invalid")
        return UserIdentity(authorization.split()[1], "USER", "LOCAL_MOCK", authorization)


class Catalog:
    def __init__(self):
        self.calls = []
        self.result = PAGE
        self.error = None
        self.file_calls = []

    async def request(self, method, path, identity, *, params=None, payload=None, key=None):
        self.calls.append((method, path, identity, params, payload, key))
        if self.error:
            raise self.error
        return self.result

    @asynccontextmanager
    async def open_file(self, work_id, identity):
        self.file_calls.append((work_id, identity.user_id))
        if identity.user_id != "alice":
            raise WorkRequestError(404, "work_not_found")
        response = httpx.Response(200, content=b"1234")
        yield response, "video/webm", 4


class WorkHttpTests(unittest.TestCase):
    def setUp(self):
        self.catalog = Catalog()
        self.client = self.enterContext(TestClient(create_app(Settings(), works=self.catalog, identity_verifier=Verifier())))
        self.headers = {"Authorization": "Bearer alice"}

    def test_list_validates_bounds_and_strips_unpublished_fields(self):
        self.catalog.result = {**PAGE, "records": [{**WORK, "ownerId": "alice", "fingerprint": "secret"}]}
        response = self.client.get("/v1/works?type=report&module=notes&pageSize=20", headers={**self.headers, "X-User-Id": "bob"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), PAGE)
        self.assertEqual(response.headers["cache-control"], "private, no-store")
        self.assertEqual(self.catalog.calls[0][2].user_id, "alice")
        self.assertEqual(self.catalog.calls[0][3]["type"], "report")
        for query in ["pageSize=51", "page=0", "sort=sql-injection", "target=http://localhost", "type=audio"]:
            self.assertEqual(self.client.get("/v1/works?" + query, headers=self.headers).status_code, 422)
        self.assertEqual(len(self.catalog.calls), 1)

    def test_save_requires_same_key_and_preserves_only_supported_fields(self):
        self.catalog.result = {"record": WORK, "reused": True}
        payload = {"module": "notes", "title": "用户文字", "type": "report", "content": "保存的正文"}
        for _ in range(2):
            response = self.client.post("/v1/works", json=payload, headers={**self.headers, "Idempotency-Key": "request-1"})
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()["reused"])
        self.assertEqual([call[5] for call in self.catalog.calls], ["request-1", "request-1"])
        self.assertEqual(self.catalog.calls[0][4], payload)
        invalid = [{**payload, "userId": "bob"}, {**payload, "imageUrl": "http://localhost"}, {**payload, "title": 12},
                   {"taskId": "task-1", "content": "injected"}, {"runId": "run-1"}, {"type": "video"}]
        for data in invalid:
            self.assertEqual(self.client.post("/v1/works", json=data, headers={**self.headers, "Idempotency-Key": "invalid"}).status_code, 422)
        self.assertEqual(self.client.post("/v1/works", json=payload, headers=self.headers).status_code, 400)
        self.assertEqual(len(self.catalog.calls), 2)

    def test_detail_lineage_and_delete_use_verified_identity_and_safe_shape(self):
        self.catalog.result = WORK
        self.assertEqual(self.client.get("/v1/works/work-1", headers=self.headers).json(), WORK)
        entry = {"kind": "WORKFLOW_NODE", "originId": "run-1", "nodeKey": "image", "attempt": 1,
                 "requestId": "req-1", "providerTaskId": None, "inputFingerprint": "sha", "originRunId": None,
                 "currentStatus": "SUCCEEDED", "sourceExists": True, "createdAt": WORK["createdAt"]}
        data = {"workId": "work-1", "entries": [entry], "total": 1, "page": 1, "pageSize": 20, "totalPages": 1}
        self.catalog.result = {**data, "entries": [{**entry, "ownerId": "secret"}]}
        self.assertEqual(self.client.get("/v1/works/work-1/lineage", headers=self.headers).json(), data)
        self.catalog.result = {"success": True}
        self.assertEqual(self.client.delete("/v1/works/work-1", headers=self.headers).json(), {"success": True})
        self.assertEqual([call[0] for call in self.catalog.calls], ["GET", "GET", "DELETE"])
        self.assertTrue(all(call[2].user_id == "alice" for call in self.catalog.calls))

    def test_authentication_and_foreign_owner_fail_without_media_redirect(self):
        self.assertEqual(self.client.get("/v1/works").status_code, 401)
        self.assertEqual(self.client.get("/v1/works/work-1/file").status_code, 401)
        self.assertEqual(self.catalog.calls, [])
        self.assertEqual(self.catalog.file_calls, [])
        response = self.client.get("/v1/works/work-1/file", headers={"Authorization": "Bearer bob"})
        self.assertEqual(response.status_code, 404)
        response = self.client.get("/v1/works/work-1/file?url=http://localhost/private", headers=self.headers)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(len(self.catalog.file_calls), 1)

    def test_file_has_private_headers_and_actual_type_extension(self):
        response = self.client.get("/v1/works/work-1/file", headers=self.headers)
        self.assertEqual(response.content, b"1234")
        self.assertEqual(response.headers["content-type"], "video/webm")
        self.assertEqual(response.headers["content-disposition"], 'attachment; filename="work-1.webm"')
        self.assertEqual(response.headers["cache-control"], "private, no-store")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")

    def test_upstream_error_or_missing_fields_never_returns_fake_success(self):
        self.catalog.result = {"secret": "do-not-leak"}
        response = self.client.get("/v1/works", headers=self.headers)
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("do-not-leak", response.text)
        self.catalog.error = WorkRequestError(409, "work_conflict")
        self.assertEqual(self.client.delete("/v1/works/work-1", headers=self.headers).status_code, 409)


class GeneratedStream(httpx.AsyncByteStream):
    def __init__(self, chunks=3, size=65536):
        self.chunks, self.size, self.generated, self.closed = chunks, size, 0, False

    async def __aiter__(self):
        for _ in range(self.chunks):
            self.generated += 1
            yield b"x" * self.size

    async def aclose(self):
        self.closed = True


class WorkTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_json_preserves_verified_bearer_service_key_idempotency_and_has_no_retry(self):
        calls = []
        async def handler(request):
            calls.append(request)
            return httpx.Response(409, json={"secret": "do-not-reflect"})
        async with httpx.AsyncClient(base_url="http://gateway", transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(WorkRequestError) as failure:
                await JavaWorkCatalog(Settings(gateway_api_key="internal"), client).request(
                    "POST", "/api/v1/content/works", IDENTITY, payload={"content": "text"}, key="request-1")
        self.assertEqual(failure.exception.status, 409)
        self.assertEqual(str(failure.exception), "work_conflict")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].headers["authorization"], "Bearer alice")
        self.assertEqual(calls[0].headers["x-service-key"], "internal")
        self.assertEqual(calls[0].headers["idempotency-key"], "request-1")
        self.assertNotIn("x-user-id", calls[0].headers)
        self.assertEqual(json.loads(calls[0].content), {"content": "text"})

    async def test_large_file_stream_is_lazy_bounded_and_closed_on_downstream_disconnect(self):
        stream = GeneratedStream(chunks=MAX_MEDIA_BYTES // 65536)
        async def handler(request):
            self.assertEqual(request.url.path, "/api/v1/content/works/work-1/file")
            self.assertEqual(request.headers["authorization"], "Bearer alice")
            return httpx.Response(200, headers={"Content-Type": "video/mp4", "Content-Length": str(MAX_MEDIA_BYTES)}, stream=stream)
        async with httpx.AsyncClient(base_url="http://gateway", transport=httpx.MockTransport(handler)) as client:
            adapter = JavaWorkCatalog(Settings(), client)
            sent = []
            async def send(message):
                sent.append(message)
                if message["type"] == "http.response.body":
                    self.assertLessEqual(len(message["body"]), 65536)
                    raise OSError("downstream disconnected")
            async def receive():
                return {"type": "http.disconnect"}
            with self.assertRaises(Exception):
                await WorkFileResponse(adapter, "work-1", IDENTITY)({"type": "http", "method": "GET", "asgi": {"spec_version": "2.4"}}, receive, send)
            self.assertEqual(sent[0]["status"], 200)
        self.assertEqual(stream.generated, 1)
        self.assertTrue(stream.closed)

    async def test_invalid_size_encoding_and_redirect_are_rejected_before_body_read(self):
        for headers, status in [({"Content-Type": "video/mp4", "Content-Length": str(MAX_MEDIA_BYTES + 1)}, 200),
                                ({"Content-Type": "text/html", "Content-Length": "4"}, 200),
                                ({"Content-Type": "video/mp4"}, 200),
                                ({"Content-Type": "video/mp4", "Content-Length": "4", "Content-Encoding": "gzip"}, 200),
                                ({"Location": "http://forbidden.invalid/file"}, 302)]:
            stream = GeneratedStream()
            async def handler(_request):
                return httpx.Response(status, headers=headers, stream=stream)
            async with httpx.AsyncClient(base_url="http://gateway", transport=httpx.MockTransport(handler)) as client:
                with self.assertRaises(WorkRequestError):
                    async with JavaWorkCatalog(Settings(), client).open_file("work-1", IDENTITY):
                        self.fail("invalid file accepted")
            self.assertEqual(stream.generated, 0)
            self.assertTrue(stream.closed)

    async def test_short_file_is_reported_as_failure_and_closed(self):
        stream = GeneratedStream(chunks=1, size=4)
        async def handler(_request):
            return httpx.Response(200, headers={"Content-Type": "image/png", "Content-Length": "8"}, stream=stream)
        async with httpx.AsyncClient(base_url="http://gateway", transport=httpx.MockTransport(handler)) as client:
            async def send(_message):
                pass
            async def receive():
                return {"type": "http.disconnect"}
            with self.assertRaises(WorkRequestError):
                await WorkFileResponse(JavaWorkCatalog(Settings(), client), "work-1", IDENTITY)(
                    {"type": "http", "method": "GET", "asgi": {"spec_version": "2.4"}}, receive, send)
        self.assertTrue(stream.closed)

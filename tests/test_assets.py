import copy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
from starlette.testclient import TestClient

from flmh_agent.adapters.inbound.workflows import Asset
from flmh_agent.adapters.inbound.assets import AssetFileResponse
from flmh_agent.adapters.outbound.assets import JavaMediaAssets, AssetRequestError
from flmh_agent.api import create_app
from flmh_agent.config import Settings
from identity_support import FIXTURE_IDENTITY, FixtureIdentityVerifier


ASSET = {"id": "184ec5e4-214a-4bf3-8134-a9f157608a14", "type": "image", "fileName": "原图.png",
         "contentType": "image/png", "bytes": 8, "sha256": "a" * 64, "source": "USER_UPLOAD",
         "storageMode": "LOCAL_FILESYSTEM", "status": "AVAILABLE", "createdAt": "2026-11-17T00:00:00Z"}
PAGE = {"assets": [ASSET], "total": 1, "page": 1, "pageSize": 20, "totalPages": 1}
HEADERS = {"Authorization": FIXTURE_IDENTITY.authorization}
UPLOAD = {**HEADERS, "Content-Type": "image/png", "Idempotency-Key": "same-upload-key"}


class AssetApiTests(unittest.TestCase):
    def setUp(self):
        self.catalog = SimpleNamespace(request=AsyncMock(return_value=copy.deepcopy(PAGE)))
        self.client = self.enterContext(TestClient(create_app(Settings(), assets=self.catalog,
                                                             identity_verifier=FixtureIdentityVerifier())))

    def test_requires_verified_identity_and_ignores_self_reported_owner(self):
        self.assertEqual(self.client.get("/v1/assets").status_code, 401)
        self.catalog.request.assert_not_awaited()
        result = self.client.get("/v1/assets?page=2&pageSize=10&type=image", headers={**HEADERS, "X-User-Id": "other"})
        self.assertEqual(result.status_code, 200)
        self.catalog.request.assert_awaited_once_with("GET", "/api/v1/content/assets", FIXTURE_IDENTITY,
                                                     params={"page": 2, "pageSize": 10, "type": "image"})

    def test_query_and_response_boundaries_do_not_expose_storage_or_owner(self):
        for query in ("page=1&page=2", "ownerId=other", "pageSize=51", "type=script", "page=0"):
            self.assertEqual(self.client.get("/v1/assets?" + query, headers=HEADERS).status_code, 422)
        self.catalog.request.assert_not_awaited()
        self.catalog.request.return_value = {**copy.deepcopy(PAGE), "secret": "x"}
        self.catalog.request.return_value["assets"][0].update(ownerId="owner", stableUrl="private", objectKey="private")
        response = self.client.get("/v1/assets", headers=HEADERS)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("secret", response.json())
        self.assertNotIn("stableUrl", response.json()["assets"][0])
        self.assertEqual(response.headers["cache-control"], "private, no-store")
        self.catalog.request.return_value = {**PAGE, "assets": [{**ASSET, "source": "MODEL_GENERATED"}]}
        self.assertEqual(self.client.get("/v1/assets", headers=HEADERS).status_code, 502)

    def test_raw_upload_streams_in_bounded_chunks_with_original_key_and_user(self):
        received = []

        async def upload(method, path, identity, **kwargs):
            self.assertEqual((method, path, identity), ("POST", "/api/v1/content/assets", FIXTURE_IDENTITY))
            self.assertEqual(kwargs["key"], "same-upload-key")
            self.assertEqual(kwargs["params"], {"fileName": "原图.png"})
            async for chunk in kwargs["body"]:
                self.assertLessEqual(len(chunk), 65536)
                received.append(chunk)
            self.assertEqual(sum(map(len, received)), kwargs["size"])
            return {"asset": ASSET, "reused": False}

        self.catalog.request.side_effect = upload
        payload = b"a" * 150000
        result = self.client.post("/v1/assets?fileName=%E5%8E%9F%E5%9B%BE.png", headers=UPLOAD, content=payload)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(b"".join(received), payload)
        self.assertEqual(len(received), 3)

    def test_upload_rejects_type_size_duplicate_headers_and_missing_key_before_java(self):
        for headers, expected in (({**UPLOAD, "Content-Type": "text/html"}, 415),
                                  ({**UPLOAD, "Content-Length": str(25 * 1024 * 1024 + 1)}, 413),
                                  ({**UPLOAD, "Content-Encoding": "gzip"}, 415),
                                  ({**UPLOAD, "Content-Length": "0"}, 413),
                                  ({**UPLOAD, "Idempotency-Key": " "}, 422)):
            self.assertEqual(self.client.post("/v1/assets?fileName=x.png", headers=headers, content=b"x").status_code, expected)
        headers = [("Authorization", FIXTURE_IDENTITY.authorization), ("Content-Type", "image/png"),
                   ("Idempotency-Key", "one"), ("Idempotency-Key", "two")]
        self.assertEqual(self.client.post("/v1/assets?fileName=x.png", headers=headers, content=b"x").status_code, 422)
        self.catalog.request.assert_not_awaited()

    def test_mismatched_stream_lengths_fail_without_claiming_upload_success(self):
        async def consume(*args, **kwargs):
            async for _chunk in kwargs["body"]:
                pass
            return {"asset": ASSET, "reused": False}

        self.catalog.request.side_effect = consume
        for size, expected in ((1, 413), (9, 400)):
            response = self.client.post("/v1/assets?fileName=x.png", content=b"12345", headers={**UPLOAD, "Content-Length": str(size)})
            self.assertEqual(response.status_code, expected)

    def test_asset_id_and_download_actions_are_fixed_routes(self):
        for path in ("bad-id", ASSET["id"] + "/secret", ASSET["id"] + "/file?ownerId=x"):
            self.assertIn(self.client.get("/v1/assets/" + path, headers=HEADERS).status_code, (404, 422))
        self.catalog.request.assert_not_awaited()
        self.catalog.request.return_value = ASSET
        self.assertEqual(self.client.get("/v1/assets/" + ASSET["id"], headers=HEADERS).status_code, 200)

    def test_workflow_asset_accepts_one_owned_reference_and_keeps_legacy_url_schema(self):
        self.assertEqual(Asset.model_validate({"type": "image", "assetId": ASSET["id"]}).assetId, ASSET["id"])
        self.assertIsNone(Asset.model_validate({"type": "image", "url": "https://example.invalid/a.png"}).assetId)
        for data in ({"type": "image"}, {"type": "image", "assetId": ASSET["id"], "url": "https://example.invalid/a.png"},
                     {"type": "image", "assetId": "../../other"}):
            with self.assertRaises(ValueError):
                Asset.model_validate(data)


class AssetTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_download_is_bounded_and_closes_when_recipient_disconnects(self):
        class Stream(httpx.AsyncByteStream):
            generated, closed = 0, False

            async def __aiter__(self):
                for _ in range(400):
                    self.generated += 1
                    yield b"x" * 65536

            async def aclose(self):
                self.closed = True

        stream = Stream()
        async def handle(request):
            self.assertEqual(request.url.path, f"/api/v1/content/assets/{ASSET['id']}/file")
            self.assertEqual(request.headers["authorization"], FIXTURE_IDENTITY.authorization)
            return httpx.Response(200, headers={"Content-Type": "image/png", "Content-Length": str(25 * 1024 * 1024)}, stream=stream)

        async def send(message):
            if message["type"] == "http.response.body":
                self.assertLessEqual(len(message["body"]), 65536)
                raise OSError("recipient disconnected")

        async def receive():
            return {"type": "http.disconnect"}

        async with httpx.AsyncClient(base_url="http://gateway", transport=httpx.MockTransport(handle)) as client:
            with self.assertRaises(Exception):
                await AssetFileResponse(JavaMediaAssets(Settings(), client), ASSET["id"], FIXTURE_IDENTITY)(
                    {"type": "http", "method": "GET", "asgi": {"spec_version": "2.4"}}, receive, send)
        self.assertEqual(stream.generated, 1)
        self.assertTrue(stream.closed)

    async def test_truncated_download_does_not_emit_a_successful_final_body(self):
        messages = []
        async def handle(request):
            return httpx.Response(200, headers={"Content-Type": "image/png", "Content-Length": "8"}, content=b"short")

        async def send(message):
            messages.append(message)

        async def receive():
            return {"type": "http.disconnect"}

        async with httpx.AsyncClient(base_url="http://gateway", transport=httpx.MockTransport(handle)) as client:
            with self.assertRaises(AssetRequestError):
                await AssetFileResponse(JavaMediaAssets(Settings(), client), ASSET["id"], FIXTURE_IDENTITY)(
                    {"type": "http", "method": "GET", "asgi": {"spec_version": "2.4"}}, receive, send)
        self.assertFalse(any(m["type"] == "http.response.body" and not m.get("more_body", False) for m in messages))

    async def test_upload_forwards_raw_body_and_auth_without_redirect(self):
        requests = []

        async def handle(request):
            requests.append(request)
            self.assertEqual(await request.aread(), b"bytes")
            self.assertEqual(request.headers["authorization"], FIXTURE_IDENTITY.authorization)
            self.assertEqual(request.headers["x-service-key"], "service-key")
            self.assertEqual(request.headers["idempotency-key"], "original-key")
            self.assertEqual(request.headers["content-length"], "5")
            return httpx.Response(307, headers={"location": "http://other.invalid"})

        async def body():
            yield b"bytes"

        async with httpx.AsyncClient(base_url="http://gateway", transport=httpx.MockTransport(handle)) as client:
            adapter = JavaMediaAssets(Settings(gateway_api_key="service-key"), client)
            with self.assertRaises(AssetRequestError) as error:
                await adapter.request("POST", "/api/v1/content/assets", FIXTURE_IDENTITY, body=body(), content_type="image/png", size=5, key="original-key")
            self.assertEqual(error.exception.status, 503)
        self.assertEqual(len(requests), 1)

    async def test_large_json_stops_reading_and_closes_response(self):
        class Stream(httpx.AsyncByteStream):
            def __init__(self):
                self.count, self.closed = 0, False

            async def __aiter__(self):
                for _ in range(100):
                    self.count += 1
                    yield b"x" * 65536

            async def aclose(self):
                self.closed = True

        stream = Stream()
        async with httpx.AsyncClient(base_url="http://gateway", transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=stream))) as client:
            with self.assertRaises(AssetRequestError) as error:
                await JavaMediaAssets(Settings(), client).request("GET", "/api/v1/content/assets", FIXTURE_IDENTITY)
            self.assertEqual(error.exception.status, 502)
        self.assertEqual(stream.count, 17)
        self.assertTrue(stream.closed)

    async def test_download_rejects_unknown_length_encoding_and_oversized_media(self):
        for headers in ({"content-type": "image/png"}, {"content-type": "image/png", "content-length": "26214401"},
                        {"content-type": "text/html", "content-length": "2"},
                        {"content-type": "image/png", "content-length": "2", "content-encoding": "gzip"}):
            async with httpx.AsyncClient(base_url="http://gateway", transport=httpx.MockTransport(lambda r: httpx.Response(200, headers=headers, stream=httpx.ByteStream(b"")))) as client:
                with self.assertRaises(AssetRequestError):
                    async with JavaMediaAssets(Settings(), client).open_file(ASSET["id"], FIXTURE_IDENTITY):
                        self.fail("invalid file must not be handed off")

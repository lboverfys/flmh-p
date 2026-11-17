import copy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
from starlette.testclient import TestClient

from flmh_agent.adapters.outbound.products import JavaProductBrowser, ProductBrowseError
from flmh_agent.api import create_app
from flmh_agent.config import Settings
from identity_support import FIXTURE_IDENTITY, FixtureIdentityVerifier


PRODUCT = {
    "id": "fixture-product", "title": "目录测试夹具", "region": "US",
    "source": "LOCAL_FIXTURE", "sourceRef": "fixture:catalog",
    "currency": "USD", "price": "12.50", "sales7d": 8,
    "freeShipping": None, "offShelf": None, "firstObservedAt": None,
    "scores": {"opportunityScore": "61.25", "growthScore": None},
}
PAGE = {
    "products": [PRODUCT], "total": 1, "page": 1, "pageSize": 20, "totalPages": 1,
    "metricScope": "CURRENT_SNAPSHOT_WITH_MATCHING_DAILY",
    "limitations": ["LOCAL_FIXTURE: 测试数据，未配置真实数据来源。"],
}
HEADERS = {"Authorization": FIXTURE_IDENTITY.authorization, "X-Agent-Key": "catalog-service-key"}


class ProductCatalogApiTests(unittest.TestCase):
    def setUp(self):
        self.browser = SimpleNamespace(browse=AsyncMock(return_value=copy.deepcopy(PAGE)))
        self.client = self.enterContext(TestClient(create_app(
            Settings(agent_api_key="catalog-service-key"),
            products=self.browser, identity_verifier=FixtureIdentityVerifier(),
        )))

    def test_catalog_requires_both_verified_user_and_configured_service_key(self):
        for headers in ({}, {"X-Agent-Key": "catalog-service-key"},
                        {"Authorization": FIXTURE_IDENTITY.authorization},
                        {**HEADERS, "Authorization": "Bearer invalid"},
                        [("Authorization", FIXTURE_IDENTITY.authorization),
                         ("Authorization", FIXTURE_IDENTITY.authorization),
                         ("X-Agent-Key", "catalog-service-key")]):
            with self.subTest(headers=headers):
                response = self.client.get("/v1/products/catalog", headers=headers)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.headers["www-authenticate"], "Bearer")
        self.browser.browse.assert_not_awaited()
        response = self.client.get("/v1/products/catalog", headers={**HEADERS, "X-User-Id": "spoofed"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.browser.browse.await_args.args[1], FIXTURE_IDENTITY)

    def test_all_supported_filters_forward_once_with_bounded_defaults(self):
        params = {
            "region": "US", "source": "LOCAL_FIXTURE", "keyword": "灯 & 杯", "category": "camping",
            "currency": "USD", "priceAmount": "10:30", "soldCount": "100:", "saleAmount": ":999.5",
            "day7SoldCount": "0:100", "day7SaleAmount": "10:", "relateAuthorCount": "1:5",
            "commission": "0.1:0.2", "authorOrderRate": "0:1", "newProduct": "true",
            "freeShipping": "true", "hotProduct": "false", "productStatus": "on_sale",
            "shopType": "sshop", "sort": "growthScore", "direction": "asc", "page": "2", "pageSize": "10",
        }
        response = self.client.get("/v1/products/catalog", params=params, headers=HEADERS)
        self.assertEqual(response.status_code, 200)
        self.browser.browse.assert_awaited_once_with({**params, "page": 2, "pageSize": 10}, FIXTURE_IDENTITY)
        self.assertEqual(response.headers["cache-control"], "private, no-store")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.browser.browse.reset_mock()
        self.assertEqual(self.client.get("/v1/products/catalog", headers=HEADERS).status_code, 200)
        forwarded = self.browser.browse.await_args.args[0]
        self.assertEqual((forwarded["page"], forwarded["pageSize"], forwarded["sort"]), (1, 20, "opportunityScore"))

    def test_unknown_duplicate_and_out_of_bounds_filters_fail_before_java(self):
        for query in ("ownerId=bob", "page=1&page=2", "keyword=a&keyword=a", "page=0", "page=10001",
                      "pageSize=101", "pageSize=0", "sort=price;DROP", "direction=sideways", "region=USA",
                      "currency=US", "freeShipping=1", "productStatus=unknown", "shopType=unknown"):
            with self.subTest(query=query):
                response = self.client.get("/v1/products/catalog?" + query, headers=HEADERS)
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json(), {"error": "invalid_product_catalog_input"})
        self.browser.browse.assert_not_awaited()

    def test_invalid_ranges_fail_before_java_and_valid_open_bounds_are_preserved(self):
        for value in (":", "2:1", "1:2:3", "-1:", "NaN:", "Infinity:", "1e2:", " 1:2",
                      "1000000000000000000:", "0.1234567:"):
            with self.subTest(value=value):
                response = self.client.get("/v1/products/catalog", params={"soldCount": value}, headers=HEADERS)
                self.assertEqual(response.status_code, 422)
        for field in ("priceAmount", "saleAmount", "day7SoldCount", "day7SaleAmount", "relateAuthorCount",
                      "commission", "authorOrderRate"):
            with self.subTest(field=field):
                response = self.client.get("/v1/products/catalog", params={field: "2:1", "currency": "USD"}, headers=HEADERS)
                self.assertEqual(response.status_code, 422)
        self.browser.browse.assert_not_awaited()
        for value in ("", "all", "0:", ":999999999999999999.123456", "1:1",
                      "999999999999999999.123456:999999999999999999.123456"):
            with self.subTest(value=value):
                response = self.client.get("/v1/products/catalog", params={"soldCount": value}, headers=HEADERS)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(self.browser.browse.await_args.args[0]["soldCount"], value)

    def test_money_comparisons_require_currency_but_all_filter_does_not(self):
        for params in ({"priceAmount": "0:100"}, {"saleAmount": ":10"},
                       {"day7SaleAmount": "1:"}, {"sort": "price"}):
            with self.subTest(params=params):
                self.assertEqual(self.client.get("/v1/products/catalog", params=params, headers=HEADERS).status_code, 422)
        self.browser.browse.assert_not_awaited()
        for params in ({"priceAmount": "all"}, {"priceAmount": "0:100", "currency": "USD"},
                       {"sort": "price", "currency": "USD"}):
            self.assertEqual(self.client.get("/v1/products/catalog", params=params, headers=HEADERS).status_code, 200)

    def test_response_schema_removes_unlisted_fields_and_preserves_unknown_values(self):
        self.browser.browse.return_value = {
            **PAGE, "debugAuthorization": "private-token",
            "products": [{**PRODUCT, "rawPayload": {"secret": "private-secret"}, "ownerId": "private-owner"}],
        }
        response = self.client.get("/v1/products/catalog", headers=HEADERS)
        self.assertEqual(response.status_code, 200)
        for value in ("private-token", "private-secret", "private-owner", "rawPayload"):
            self.assertNotIn(value, response.text)
        product = response.json()["products"][0]
        self.assertEqual(product["price"], "12.50")
        self.assertEqual(product["scores"]["opportunityScore"], "61.25")
        self.assertIsNone(product["freeShipping"])
        self.assertIsNone(product["scores"]["growthScore"])
        self.assertEqual(product["sourceRef"], "fixture:catalog")

    def test_invalid_upstream_schema_never_becomes_empty_success(self):
        for result in ({}, {**PAGE, "products": [PRODUCT] * 101},
                       {**PAGE, "products": [{**PRODUCT, "price": "NaN"}]},
                       {**PAGE, "products": [{"id": "partial"}]},
                       {**PAGE, "metricScope": "UNVERIFIED"}, {**PAGE, "total": -1}):
            with self.subTest(result_type=list(result)):
                self.browser.browse.return_value = result
                response = self.client.get("/v1/products/catalog", headers=HEADERS)
                self.assertEqual(response.status_code, 502)
                self.assertEqual(response.json(), {"error": "invalid_product_catalog_response"})

    def test_upstream_failure_is_preserved_without_retry_or_mock_fallback(self):
        self.browser.browse.side_effect = ProductBrowseError(429, "product_catalog_request_failed")
        response = self.client.get("/v1/products/catalog", headers=HEADERS)
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json(), {"error": "product_catalog_request_failed"})
        self.browser.browse.assert_awaited_once()


class RecordingStream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.read_count = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.read_count += 1
            yield chunk

    async def aclose(self):
        self.closed = True


class ProductCatalogTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_transport_sends_only_fixed_path_bearer_service_key_and_query_once(self):
        requests = []

        async def respond(request):
            requests.append(request)
            return httpx.Response(200, json=PAGE)

        async with httpx.AsyncClient(base_url="http://catalog.invalid", transport=httpx.MockTransport(respond)) as client:
            browser = JavaProductBrowser(Settings(gateway_api_key="java-service-key"), client)
            result = await browser.browse({"keyword": "灯 & 杯", "page": 2, "pageSize": 20}, FIXTURE_IDENTITY)
        self.assertEqual(result, PAGE)
        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertEqual((request.method, request.url.path), ("GET", "/api/v1/products/catalog"))
        self.assertEqual(dict(request.url.params), {"keyword": "灯 & 杯", "page": "2", "pageSize": "20"})
        self.assertEqual(request.headers["Authorization"], FIXTURE_IDENTITY.authorization)
        self.assertEqual(request.headers["X-Service-Key"], "java-service-key")
        self.assertEqual(request.headers["Accept-Encoding"], "identity")
        self.assertNotIn("X-User-Id", request.headers)
        self.assertEqual(request.content, b"")

    async def test_redirects_never_forward_bearer_or_service_key_to_another_origin(self):
        for code in (301, 302, 303, 307, 308):
            with self.subTest(code=code):
                requests = []
                async def respond(request):
                    requests.append(request)
                    return httpx.Response(code, headers={"Location": "https://external.invalid/private"})
                async with httpx.AsyncClient(base_url="http://catalog.invalid", follow_redirects=True,
                                            transport=httpx.MockTransport(respond)) as client:
                    with self.assertRaises(ProductBrowseError) as failure:
                        await JavaProductBrowser(Settings(gateway_api_key="secret"), client).browse({}, FIXTURE_IDENTITY)
                self.assertEqual(failure.exception.status, 503)
                self.assertEqual(len(requests), 1)
                self.assertEqual(requests[0].url.host, "catalog.invalid")

    async def test_error_bodies_are_not_read_or_leaked_and_status_mapping_is_explicit(self):
        for code in (400, 401, 403, 404, 429, 500):
            with self.subTest(code=code):
                stream = RecordingStream([b'{"secret":"upstream-credential"}'])
                transport = httpx.MockTransport(lambda request: httpx.Response(code, stream=stream))
                async with httpx.AsyncClient(base_url="http://catalog.invalid", transport=transport) as client:
                    with self.assertRaises(ProductBrowseError) as failure:
                        await JavaProductBrowser(Settings(), client).browse({}, FIXTURE_IDENTITY)
                self.assertEqual(failure.exception.status, code if code in (400, 401, 403, 429) else 503)
                self.assertEqual(str(failure.exception), "product_catalog_request_failed")
                self.assertEqual(stream.read_count, 0)
                self.assertTrue(stream.closed)

    async def test_size_limit_stops_reading_and_closes_oversized_response(self):
        stream = RecordingStream([b" " * 65536] * 70)
        transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=stream))
        async with httpx.AsyncClient(base_url="http://catalog.invalid", transport=transport) as client:
            with self.assertRaises(ProductBrowseError) as failure:
                await JavaProductBrowser(Settings(), client).browse({}, FIXTURE_IDENTITY)
        self.assertEqual(failure.exception.status, 502)
        self.assertEqual(stream.read_count, 65)
        self.assertTrue(stream.closed)

    async def test_exact_size_limit_is_accepted_and_invalid_json_or_non_objects_fail(self):
        prefix, suffix = b'{"value":"', b'"}'
        body = prefix + b"x" * (4 * 1024 * 1024 - len(prefix) - len(suffix)) + suffix
        async with httpx.AsyncClient(base_url="http://catalog.invalid",
                                    transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body))) as client:
            value = await JavaProductBrowser(Settings(), client).browse({}, FIXTURE_IDENTITY)
        self.assertEqual(len(value["value"]), len(body) - len(prefix) - len(suffix))
        for body in (b"[]", b"null", b"{", b"\xff"):
            with self.subTest(body=body):
                async with httpx.AsyncClient(base_url="http://catalog.invalid",
                                            transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body))) as client:
                    with self.assertRaises(ProductBrowseError) as failure:
                        await JavaProductBrowser(Settings(), client).browse({}, FIXTURE_IDENTITY)
                self.assertEqual(failure.exception.status, 502)

    async def test_network_failure_is_sanitized_and_not_retried(self):
        calls = 0
        async def respond(request):
            nonlocal calls
            calls += 1
            raise httpx.ReadTimeout("private-upstream-details", request=request)
        async with httpx.AsyncClient(base_url="http://catalog.invalid", transport=httpx.MockTransport(respond)) as client:
            with self.assertRaises(ProductBrowseError) as failure:
                await JavaProductBrowser(Settings(), client).browse({}, FIXTURE_IDENTITY)
        self.assertEqual(failure.exception.status, 503)
        self.assertEqual(str(failure.exception), "product_catalog_unavailable")
        self.assertEqual(calls, 1)


if __name__ == "__main__":
    unittest.main()

import json
import unittest

import httpx
from pydantic import ValidationError
from starlette.requests import Request
from starlette.testclient import TestClient

from flmh_agent.adapters.inbound.client_ip import client_address
from flmh_agent.adapters.outbound.identity import JavaIdentityVerifier
from flmh_agent.api import create_app
from flmh_agent.config import Settings
from flmh_agent.domain.identity import AuthenticationError, IdentityInputError, IdentityRateLimitedError, IdentityUnavailableError


REQUESTED = {"message": "如果该邮箱已注册，将收到密码重置邮件，请查收。"}
RESET = {"message": "密码重置成功，请使用新密码登录。"}


def incoming(peer="127.0.0.1", headers=()):
    return Request({"type": "http", "client": (peer, 50000), "headers": [(key.lower().encode(), value.encode()) for key, value in headers]})


class ClientAddressTests(unittest.TestCase):
    def test_untrusted_sources_cannot_choose_their_limit_bucket(self):
        headers = [("X-Flmh-Client-IP", "198.51.100.10"), ("X-Service-Key", "internal"),
                   ("X-Forwarded-For", "192.0.2.1"), ("Forwarded", "for=192.0.2.2")]
        self.assertEqual(client_address(incoming(headers=headers), Settings(gateway_api_key="internal")), "127.0.0.1")
        trusted = Settings(trusted_proxy_cidrs="127.0.0.0/8", gateway_api_key="internal")
        self.assertEqual(client_address(incoming("192.0.2.5", headers), trusted), "192.0.2.5")
        self.assertEqual(client_address(incoming(headers=headers[2:]), trusted), "127.0.0.1")
        self.assertEqual(client_address(incoming(headers=[("X-Service-Key", "wrong"), *headers]), trusted), "127.0.0.1")

    def test_trusted_ip_requires_both_key_and_cidr_and_normalizes_mapped_ipv6(self):
        settings = Settings(trusted_proxy_cidrs="127.0.0.1/32,::1/128", gateway_api_key="internal")
        for peer in ("127.0.0.1", "::ffff:127.0.0.1", "::1"):
            self.assertEqual(client_address(incoming(peer, [("X-Service-Key", "internal"), ("X-Flmh-Client-IP", "::ffff:192.0.2.7")]), settings), "192.0.2.7")
        self.assertEqual(client_address(incoming(headers=[("X-Service-Key", "internal"), ("X-Forwarded-For", "192.0.2.9")]), settings), "127.0.0.1")

    def test_trusted_malformed_or_duplicate_headers_are_rejected(self):
        settings = Settings(trusted_proxy_cidrs="127.0.0.1/32", gateway_api_key="internal")
        for value in ("", " 192.0.2.1", "192.0.2.1:80", "localhost", "192.0.2.1, 192.0.2.2", "fe80::1%lo"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                client_address(incoming(headers=[("X-Service-Key", "internal"), ("X-Flmh-Client-IP", value)]), settings)
        with self.assertRaises(ValueError):
            client_address(incoming(headers=[("X-Service-Key", "internal"), ("X-Flmh-Client-IP", "192.0.2.1"), ("X-Flmh-Client-IP", "192.0.2.2")]), settings)
        with self.assertRaises(ValueError):
            client_address(incoming("testclient"), Settings())

    def test_proxy_configuration_requires_explicit_secret_and_valid_cidrs(self):
        self.assertEqual(Settings(trusted_proxy_cidrs="").trusted_proxy_cidrs, ())
        self.assertEqual(Settings(trusted_proxy_cidrs="192.0.2.9/24", gateway_api_key="key").trusted_proxy_cidrs, ("192.0.2.0/24",))
        for values in ({"trusted_proxy_cidrs": "127.0.0.1/32"}, {"trusted_proxy_cidrs": "localhost", "gateway_api_key": "key"},
                       {"trusted_proxy_cidrs": "0.0.0.0/0", "gateway_api_key": "key"},
                       {"trusted_proxy_cidrs": ("127.0.0.1/32",) * 65, "gateway_api_key": "key"}):
            with self.assertRaises(ValidationError):
                Settings(**values)


class RecoveryAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_transport_sends_only_server_headers_and_allowlists_response(self):
        calls = []

        async def handler(request):
            calls.append(request)
            result = RESET if request.url.path.endswith("/reset-password") else REQUESTED
            return httpx.Response(200, json={**result, "code": "123456", "secret": "do-not-return"})

        async with httpx.AsyncClient(base_url="http://identity", transport=httpx.MockTransport(handler)) as client:
            adapter = JavaIdentityVerifier(Settings(gateway_api_key="internal"), client)
            self.assertEqual(await adapter.forgot_password("user@example.invalid", client_ip="192.0.2.8"), REQUESTED)
            self.assertEqual(await adapter.reset_password("user@example.invalid", "123456", "new-password", client_ip="192.0.2.8"), RESET)
        self.assertEqual(json.loads(calls[0].content), {"email": "user@example.invalid"})
        self.assertEqual(json.loads(calls[1].content), {"email": "user@example.invalid", "code": "123456", "newPassword": "new-password"})
        for request in calls:
            self.assertEqual(request.headers["x-service-key"], "internal")
            self.assertEqual(request.headers["x-flmh-client-ip"], "192.0.2.8")
            self.assertNotIn("authorization", request.headers)

    async def test_error_statuses_preserved_without_retry_or_redirect(self):
        for status, exception in ((429, IdentityRateLimitedError), (400, IdentityInputError), (422, IdentityInputError),
                                  (401, AuthenticationError), (503, IdentityUnavailableError), (307, IdentityUnavailableError)):
            calls = []

            async def handler(request):
                calls.append(request)
                return httpx.Response(status, headers={"Location": "http://untrusted.invalid/steal"}, json={"error": "secret"})

            async with httpx.AsyncClient(base_url="http://identity", transport=httpx.MockTransport(handler), follow_redirects=True) as client:
                with self.subTest(status=status), self.assertRaises(exception) as caught:
                    await JavaIdentityVerifier(Settings(), client).reset_password("user", "123456", "private", client_ip="127.0.0.1")
                self.assertNotIn("secret", str(caught.exception))
                self.assertEqual(len(calls), 1)

    async def test_oversized_invalid_and_noncanonical_responses_rejected(self):
        for response in (httpx.Response(200, content=b"x" * 65537), httpx.Response(200, content=b"not json"),
                         httpx.Response(200, json=[]), httpx.Response(200, json={"message": "email exists", "code": "123456"})):
            async with httpx.AsyncClient(base_url="http://identity", transport=httpx.MockTransport(lambda _: response)) as client:
                with self.assertRaises(IdentityUnavailableError):
                    await JavaIdentityVerifier(Settings(), client).forgot_password("user", client_ip="127.0.0.1")


class RecoveryFixture:
    def __init__(self, failure=None):
        self.failure = failure
        self.calls = []

    async def forgot_password(self, email, *, client_ip):
        self.calls.append(("forgot", email, client_ip))
        if self.failure:
            raise self.failure
        return REQUESTED

    async def reset_password(self, email, code, password, *, client_ip):
        self.calls.append(("reset", email, code, password, client_ip))
        if self.failure:
            raise self.failure
        return RESET


class RecoveryHttpTests(unittest.TestCase):
    def test_anonymous_recovery_still_requires_agent_access_key(self):
        fixture = RecoveryFixture()
        with TestClient(create_app(Settings(agent_api_key="access"), identity_recovery=fixture), client=("127.0.0.1", 50000)) as client:
            path = "/v1/identity/forgot-password"
            self.assertEqual(client.post(path, json={"email": "user@example.invalid"}).status_code, 401)
            for email in ("known@example.invalid", "unknown@example.invalid"):
                response = client.post(path, json={"email": email}, headers={"X-Agent-Key": "access", "X-Flmh-Client-IP": "192.0.2.1"})
                self.assertEqual(response.json(), REQUESTED)
                self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual([item[-1] for item in fixture.calls], ["127.0.0.1", "127.0.0.1"])
            self.assertEqual(client.get(path, headers={"X-Agent-Key": "access"}).status_code, 401)

    def test_reset_checks_utf8_password_code_extra_fields_and_never_leaks_input(self):
        fixture = RecoveryFixture()
        with TestClient(create_app(Settings(), identity_recovery=fixture), client=("127.0.0.1", 50000)) as client:
            body = {"email": "user@example.invalid", "code": "123456", "newPassword": "密" * 24}
            response = client.post("/v1/identity/reset-password", json=body)
            self.assertEqual(response.json(), RESET)
            self.assertEqual(response.headers["cache-control"], "no-store")
            for invalid in ({**body, "newPassword": "密" * 25}, {**body, "code": "12345"}, {**body, "admin": True}):
                response = client.post("/v1/identity/reset-password", json=invalid)
                self.assertEqual(response.status_code, 422)
                self.assertNotIn("12345", response.text)
                self.assertNotIn("密", response.text)
            self.assertEqual(len(fixture.calls), 1)

    def test_failure_mapping_is_safe_and_rate_limit_is_not_unavailable(self):
        for failure, status in ((IdentityRateLimitedError("secret"), 429), (IdentityInputError("secret"), 400),
                                (AuthenticationError("secret"), 503), (IdentityUnavailableError("secret"), 503)):
            with TestClient(create_app(Settings(), identity_recovery=RecoveryFixture(failure)), client=("127.0.0.1", 50000)) as client:
                response = client.post("/v1/identity/reset-password", json={"email": "user@example.invalid", "code": "123456", "newPassword": "private"})
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.headers["cache-control"], "no-store")
                for secret in ("secret", "private", "123456"):
                    self.assertNotIn(secret, response.text)

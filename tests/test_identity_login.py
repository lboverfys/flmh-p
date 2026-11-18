import json
import unittest

import httpx
from starlette.testclient import TestClient

from flmh_agent.adapters.outbound.identity import JavaIdentityVerifier
from flmh_agent.api import create_app
from flmh_agent.config import Settings
from flmh_agent.domain.identity import AuthenticationError, IdentityUnavailableError, IdentityRateLimitedError
from identity_support import FixtureIdentityVerifier


LOGIN_RESULT = {
    "token": "unit-user", "tokenType": "Bearer", "expiresAt": "2026-11-18T00:00:00Z",
    "user": {"id": "fixture-user", "role": "USER", "status": "ACTIVE", "source": "LOCAL_MOCK"},
    "source": "LOCAL_MOCK",
}


class FixtureLogin:
    def __init__(self, failure=None):
        self.calls = []
        self.failure = failure

    async def login(self, email, password, *, client_ip):
        self.calls.append((email, password, client_ip))
        if self.failure:
            raise self.failure
        return LOGIN_RESULT


class LoginAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_login_uses_only_server_service_key_and_returns_allowlisted_fields(self):
        calls = []

        async def handler(request):
            calls.append(request)
            return httpx.Response(200, json={**LOGIN_RESULT, "serviceKey": "do-not-expose", "password": "do-not-expose",
                                            "user": {**LOGIN_RESULT["user"], "passwordHash": "do-not-expose"}})

        async with httpx.AsyncClient(base_url="http://identity", transport=httpx.MockTransport(handler)) as client:
            result = await JavaIdentityVerifier(Settings(gateway_api_key="internal-service-secret"), client).login("alice@example.invalid", "input-password", client_ip="192.0.2.9")
        self.assertEqual(result, LOGIN_RESULT)
        self.assertEqual(calls[0].url.path, "/api/v1/identity/login")
        self.assertEqual(json.loads(calls[0].content), {"email": "alice@example.invalid", "password": "input-password"})
        self.assertEqual(calls[0].headers["x-service-key"], "internal-service-secret")
        self.assertEqual(calls[0].headers["x-flmh-client-ip"], "192.0.2.9")
        self.assertNotIn("authorization", calls[0].headers)
        self.assertNotIn("secret", json.dumps(result))
        self.assertNotIn("password", json.dumps(result))

    async def test_login_rejects_disabled_users_invalid_payloads_and_mock_in_production(self):
        for code in (401, 403):
            async with httpx.AsyncClient(base_url="http://identity", transport=httpx.MockTransport(lambda _: httpx.Response(code))) as client:
                with self.assertRaises(AuthenticationError):
                    await JavaIdentityVerifier(Settings(), client).login("user", "password", client_ip="192.0.2.9")
        for payload in ({}, {**LOGIN_RESULT, "token": "bad token"},
                        {**LOGIN_RESULT, "user": {**LOGIN_RESULT["user"], "status": "DISABLED"}}):
            async with httpx.AsyncClient(base_url="http://identity", transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))) as client:
                with self.assertRaises(IdentityUnavailableError):
                    await JavaIdentityVerifier(Settings(), client).login("user", "password", client_ip="192.0.2.9")
        settings = Settings(environment="production", agent_api_key="service", planner_backend="model_gateway",
                            checkpointer_backend="postgres", postgres_checkpointer_uri="postgresql://localhost/test")
        async with httpx.AsyncClient(base_url="http://identity", transport=httpx.MockTransport(lambda _: httpx.Response(200, json=LOGIN_RESULT))) as client:
            with self.assertRaises(AuthenticationError):
                await JavaIdentityVerifier(settings, client).login("user", "password", client_ip="192.0.2.9")

    async def test_login_service_failure_never_creates_a_mock_session(self):
        def unavailable(request):
            raise httpx.ConnectError("unavailable", request=request)

        async with httpx.AsyncClient(base_url="http://identity", transport=httpx.MockTransport(unavailable)) as client:
            with self.assertRaises(IdentityUnavailableError):
                await JavaIdentityVerifier(Settings(), client).login("user", "password", client_ip="192.0.2.9")


class LoginHttpTests(unittest.TestCase):
    def test_login_is_explicit_and_me_verifies_token_without_returning_credentials(self):
        login = FixtureLogin()
        with TestClient(create_app(Settings(), identity_verifier=FixtureIdentityVerifier(), identity_login=login), client=("127.0.0.1", 50000)) as client:
            self.assertEqual(client.get("/health/ready").status_code, 200)
            self.assertEqual(login.calls, [])
            self.assertEqual(client.get("/v1/identity/me").status_code, 401)
            response = client.post("/v1/identity/login", json={"email": " alice@example.invalid ", "password": "input-password"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(login.calls, [("alice@example.invalid", "input-password", "127.0.0.1")])
            me = client.get("/v1/identity/me", headers={"Authorization": "Bearer unit-user", "X-User-Id": "forged", "X-Role": "ADMIN"})
            self.assertEqual(me.status_code, 200)
            self.assertEqual(me.json(), LOGIN_RESULT["user"])
            self.assertEqual(me.headers["cache-control"], "no-store")
            self.assertNotIn("unit-user", me.text)

    def test_login_does_not_bypass_agent_access_key_or_open_other_routes(self):
        login = FixtureLogin()
        with TestClient(create_app(Settings(agent_api_key="access-only"), identity_verifier=FixtureIdentityVerifier(), identity_login=login), client=("127.0.0.1", 50000)) as client:
            body = {"email": "alice@example.invalid", "password": "input-password"}
            self.assertEqual(client.post("/v1/identity/login", json=body).status_code, 401)
            headers = {"X-Agent-Key": "access-only"}
            self.assertEqual(client.post("/v1/identity/login", json=body, headers=headers).status_code, 200)
            self.assertEqual(client.get("/v1/identity/me", headers=headers).status_code, 401)
            self.assertEqual(client.post("/v1/agent/runs", json={}, headers=headers).status_code, 401)
            self.assertEqual(len(login.calls), 1)

    def test_login_errors_are_safe_and_never_reflect_credentials(self):
        for failure, status in [(AuthenticationError("secret"), 401), (IdentityUnavailableError("secret"), 503), (IdentityRateLimitedError("secret"), 429)]:
            with TestClient(create_app(Settings(), identity_login=FixtureLogin(failure)), client=("127.0.0.1", 50000)) as client:
                response = client.post("/v1/identity/login", json={"email": "user@example.invalid", "password": "secret"})
                self.assertEqual(response.status_code, status)
                self.assertNotIn("secret", response.text)
        login = FixtureLogin()
        with TestClient(create_app(Settings(), identity_login=login), client=("127.0.0.1", 50000)) as client:
            response = client.post("/v1/identity/login", json={"email": "user", "password": "secret", "role": "ADMIN"})
            self.assertEqual(response.status_code, 422)
            self.assertNotIn("secret", response.text)
            self.assertEqual(login.calls, [])

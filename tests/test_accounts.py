import json
import unittest

import httpx
from starlette.testclient import TestClient

from flmh_agent.adapters.outbound.accounts import AccountRequestError, JavaUserAccounts
from flmh_agent.api import create_app
from flmh_agent.config import Settings
from flmh_agent.domain.identity import AuthenticationError, UserIdentity


ACCOUNT = {"id": "alice", "name": "Alice", "email": "alice@example.invalid", "role": "USER",
           "status": "ACTIVE", "membershipLevel": "FREE", "points": 5, "source": "LOCAL_MOCK"}


class Verifier:
    def __init__(self):
        self.role = "ADMIN"

    async def authenticate(self, authorization):
        if authorization != "Bearer session":
            raise AuthenticationError("invalid")
        return UserIdentity("admin", self.role, "LOCAL_MOCK", authorization)


class Accounts:
    def __init__(self):
        self.calls = []
        self.result = ACCOUNT

    async def request(self, method, path, identity, *, params, payload):
        self.calls.append((method, path, identity, params, payload))
        return self.result


class AccountHttpTests(unittest.TestCase):
    def setUp(self):
        self.accounts = Accounts()
        self.verifier = Verifier()
        self.client = self.enterContext(TestClient(create_app(Settings(), accounts=self.accounts,
                                                               identity_verifier=self.verifier)))
        self.headers = {"Authorization": "Bearer session"}

    def test_self_profile_strips_secret_fields_and_uses_verified_identity(self):
        self.accounts.result = {**ACCOUNT, "password": "secret", "passwordHash": "secret", "token": "secret"}
        response = self.client.get("/v1/identity/profile", headers={**self.headers, "X-User-Id": "forged"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), ACCOUNT)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(self.accounts.calls[0][2].user_id, "admin")
        self.assertNotIn("secret", response.text)

    def test_current_role_is_checked_and_non_admin_cannot_mutate_accounts(self):
        self.verifier.role = "USER"
        for method, path, body in [("GET", "/users", None), ("POST", "/users", {}),
                                   ("DELETE", "/users/alice", None), ("PUT", "/users/alice/points", {})]:
            response = self.client.request(method, "/v1/identity" + path,
                                           headers={**self.headers, "X-Role": "ADMIN"}, json=body)
            self.assertEqual(response.status_code, 403)
        self.assertEqual(self.accounts.calls, [])
        self.assertEqual(self.client.get("/v1/identity/profile", headers=self.headers).status_code, 200)
        self.assertEqual(self.client.get("/v1/identity/profile").status_code, 401)

    def test_mutation_input_cannot_self_assign_role_and_never_reflects_password(self):
        for path, payload in [("/profile", {"role": "ADMIN", "password": "secret"}),
                              ("/password", {"currentPassword": "secret", "newPassword": "short"}),
                              ("/users/alice/points", {"amount": True, "reason": "secret"})]:
            response = self.client.put("/v1/identity" + path, headers=self.headers, json=payload)
            self.assertEqual(response.status_code, 422)
            self.assertNotIn("secret", response.text)
        self.assertEqual(self.accounts.calls, [])

    def test_paging_filters_are_bounded_and_nested_outputs_are_allowlisted(self):
        self.accounts.result = {"users": [{**ACCOUNT, "password": "secret", "parentAgent": {"name": "parent", "password": "secret"},
                                           "usage": {"total": 1, "pending": 1, "succeeded": 0, "failed": 0, "token": "secret"}}],
                                "total": 1, "page": 1, "pageSize": 10, "totalPages": 1}
        response = self.client.get("/v1/identity/users?search=Alice&pageSize=10", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("secret", response.text)
        self.assertEqual(self.accounts.calls[0][3]["search"], "Alice")
        self.assertEqual(self.client.get("/v1/identity/users?pageSize=101", headers=self.headers).status_code, 422)
        self.assertEqual(self.client.get("/v1/identity/users?target=http://other", headers=self.headers).status_code, 422)
        self.assertEqual(len(self.accounts.calls), 1)

    def test_password_change_and_points_use_exact_contract_without_retries(self):
        self.accounts.result = {"success": True}
        response = self.client.put("/v1/identity/password", headers=self.headers,
                                   json={"currentPassword": "old-pass", "newPassword": "new-pass"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.accounts.calls[0][4], {"currentPassword": "old-pass", "newPassword": "new-pass"})
        self.accounts.result = {"points": 23}
        response = self.client.put("/v1/identity/users/alice/points", headers=self.headers, json={"amount": 18, "reason": "adjust"})
        self.assertEqual(response.json(), {"points": 23})
        self.assertEqual(len(self.accounts.calls), 2)

    def test_names_and_adjustment_reasons_match_the_java_limits_before_forwarding(self):
        for path, payload in [("/profile", {"name": "n" * 101}),
                              ("/users/alice", {"name": "n" * 101}),
                              ("/users/alice/points", {"amount": 1, "reason": "r" * 501})]:
            response = self.client.put("/v1/identity" + path, headers=self.headers, json=payload)
            self.assertEqual(response.status_code, 422)
        response = self.client.post("/v1/identity/users", headers=self.headers,
                                    json={"name": "n" * 101, "email": "new@example.invalid", "password": "new-password"})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.accounts.calls, [])

    def test_missing_upstream_fields_fail_instead_of_returning_fake_success(self):
        self.accounts.result = {"password": "secret"}
        response = self.client.get("/v1/identity/profile", headers=self.headers)
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("secret", response.text)


class AccountAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_server_key_and_verified_jwt_are_sent_and_no_http_error_retry_occurs(self):
        calls = []

        async def handler(request):
            calls.append(request)
            return httpx.Response(409, json={"detail": "must-not-reflect-password"})

        identity = UserIdentity("admin", "ADMIN", "LOCAL_MOCK", "Bearer session")
        async with httpx.AsyncClient(base_url="http://identity", transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(AccountRequestError) as error:
                await JavaUserAccounts(Settings(gateway_api_key="internal"), client).request(
                    "PUT", "/api/v1/identity/users/alice/points", identity, payload={"amount": 1, "reason": "test"})
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(str(error.exception), "account_conflict")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].headers["authorization"], "Bearer session")
        self.assertEqual(calls[0].headers["x-service-key"], "internal")
        self.assertNotIn("x-user-id", calls[0].headers)
        self.assertEqual(json.loads(calls[0].content), {"amount": 1, "reason": "test"})

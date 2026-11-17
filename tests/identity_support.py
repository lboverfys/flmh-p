import unittest

from flmh_agent.domain.identity import AuthenticationError, UserIdentity, bind_identity


FIXTURE_IDENTITY = UserIdentity("fixture-user", "USER", "LOCAL_MOCK", "Bearer unit-user")


class FixtureIdentityVerifier:
    async def authenticate(self, authorization):
        if authorization != FIXTURE_IDENTITY.authorization:
            raise AuthenticationError("test token is invalid")
        return FIXTURE_IDENTITY


class IdentityTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.enterContext(bind_identity(FIXTURE_IDENTITY))

"""Tests for letting DCR clients offer email-code sign-in."""

import unittest

from hamcrest import assert_that, equal_to

from ..clients import USER_AUTH, allow_email_codes
from .cognito import FakeCognito

CODE = "ALLOW_REFRESH_TOKEN_AUTH"


class TestAllowEmailCodes(unittest.TestCase):
    """Every DCR client gets ALLOW_USER_AUTH once; nothing else changes."""

    def setUp(self) -> None:
        """A pool with three DCR clients (one already allowed) and a regular one."""
        self.cognito = FakeCognito()
        self.cognito.add("a", [CODE])
        self.cognito.add("b", [CODE, USER_AUTH])
        self.cognito.add("c", [])
        self.cognito.add("r", [CODE], kind="REGULAR")

    def test_adds_flow_across_pages(self) -> None:
        """Clients lacking the flow get it, keeping their other flows."""
        changed = allow_email_codes(self.cognito.pool())
        assert_that(list(changed), equal_to(["a", "c"]))
        flows = self.cognito.flows()
        expected = {
            "a": [CODE, USER_AUTH],
            "b": [CODE, USER_AUTH],
            "c": [USER_AUTH],
            "r": [CODE],
        }
        assert_that(flows, equal_to(expected))

    def test_request_keeps_settings(self) -> None:
        """The update resends settings but not read-only members."""
        allow_email_codes(self.cognito.pool())
        request = dict(self.cognito.updates()[0])
        expected = {
            "UserPoolId": "pool",
            "ClientId": "a",
            "ClientName": "name-a",
            "ExplicitAuthFlows": [CODE, USER_AUTH],
            "CallbackURLs": ["https://claude.ai/callback"],
        }
        assert_that(request, equal_to(expected))

    def test_idempotent(self) -> None:
        """A second run changes nothing."""
        allow_email_codes(self.cognito.pool())
        assert_that(list(allow_email_codes(self.cognito.pool())), equal_to([]))
        assert_that(len(self.cognito.updates()), equal_to(2))

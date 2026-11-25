"""Tests for session tokens, alone and at the MCP endpoint."""

import unittest
from collections.abc import Callable, Mapping

import jwt
from hamcrest import assert_that, equal_to, has_key, instance_of

from .. import jsonvalue
from ..auth import AuthError, BearerValidator
from ..ingest import ingest
from ..sessions import EitherValidator, SessionValidator, issue, verify
from .checks import failure, includes
from .fakes import Clock, make_world, sample_raw
from .flaky import Validator
from .sessionkey import SESSION_KEY
from .mcpclient import HOST, Client, structured

ENDPOINT = f"https://{HOST}/mcp"
KEY = SESSION_KEY.value()
NOW = 1_767_225_600.0  # 2026-01-01T00:00:00Z, the fake world's start
COGNITO_TOKEN = jwt.encode({}, KEY, algorithm="HS512")


def token_at(now: float) -> str:
    """Issue a token.

    Args:
        now: When.

    Returns:
        The token.
    """
    return jsonvalue.get_str(issue(KEY, ENDPOINT, now), "token")


def refused(action: object) -> None:
    """Check that validating raised AuthError.

    Args:
        action: What the validation raised, from :func:`failure`.
    """
    assert_that(action, instance_of(AuthError))


def either(clock: Callable[[], float]) -> BearerValidator:
    """Build the production validator pair with a fake Cognito.

    Args:
        clock: The current time.

    Returns:
        The validator.
    """
    return EitherValidator.create(
        sessions=SessionValidator.create(SESSION_KEY, clock),
        cognito=Validator(outcomes={COGNITO_TOKEN: None}),
    )


class TestSessionTokens(unittest.TestCase):
    """Issuing and checking session tokens."""

    def test_issue(self) -> None:
        """A token names the endpoint and expires in five minutes."""
        issued = issue(KEY, ENDPOINT, NOW + 0.5)
        includes(
            issued,
            dict(endpoint=ENDPOINT, expires_at="2026-01-01T00:05:00+00:00"),
        )
        claims = verify(jsonvalue.get_str(issued, "token"), KEY, ENDPOINT, NOW)
        includes(claims, dict(iss="pidge-session", aud=ENDPOINT, exp=NOW + 300))

    def test_valid_until_expiry(self) -> None:
        """A token is accepted until it expires, then refused."""
        token = token_at(NOW)
        includes(verify(token, KEY, ENDPOINT, NOW + 299), dict(aud=ENDPOINT))
        refused(failure(lambda: verify(token, KEY, ENDPOINT, NOW + 300)))

    def test_tampered(self) -> None:
        """Changed claims, another key or another endpoint are refused."""
        head, _, signature = token_at(NOW).split(".")
        longer = token_at(NOW + 3600).split(".")[1]
        cases = {
            "claims": f"{head}.{longer}.{signature}",
            "key": jsonvalue.get_str(issue(b"x" * 40, ENDPOINT, NOW), "token"),
            "endpoint": jsonvalue.get_str(issue(KEY, "https://x/mcp", NOW), "token"),
            "unsigned": jwt.encode({"iss": "pidge-session"}, "", algorithm="none"),
        }
        for name, forged in cases.items():
            with self.subTest(name):
                refused(failure(lambda: verify(forged, KEY, ENDPOINT, NOW)))

    def test_non_integer_expiry(self) -> None:
        """A correctly signed token with a fractional expiry is refused."""
        claims = {"iss": "pidge-session", "aud": ENDPOINT, "exp": NOW + 60.5}
        token = jwt.encode(claims, KEY, algorithm="HS256")
        refused(failure(lambda: verify(token, KEY, ENDPOINT, NOW)))


class TestEitherValidator(unittest.TestCase):
    """Bearer tokens go to the validator for their kind."""

    def setUp(self) -> None:
        """Wire a session validator and a fake Cognito one."""
        self.clock = Clock.create(NOW)
        self.validator = either(self.clock.time)

    def check(self, token: str) -> Mapping[str, object]:
        """Validate a token.

        Args:
            token: The token.

        Returns:
            The claims.
        """
        return self.validator.validate(token, ENDPOINT, "full")

    def test_routing(self) -> None:
        """HS256 tokens are session tokens; others are Cognito's."""
        includes(self.check(token_at(NOW)), dict(iss="pidge-session"))
        assert_that(
            self.check(COGNITO_TOKEN), equal_to(dict(aud=ENDPOINT, scope="full"))
        )

    def test_garbage_and_expired(self) -> None:
        """A token that is not a JWT, or has expired by the clock, is refused."""
        refused(failure(lambda: self.check("nope")))
        token = token_at(NOW)
        self.clock.advance(301)
        refused(failure(lambda: self.check(token)))


class TestEndpoint(unittest.TestCase):
    """A session token from get_session_token works on the MCP endpoint."""

    def setUp(self) -> None:
        """Create a client that checks session tokens for real; get a token."""
        self.world = make_world(self)
        self.client = Client.create(self.world, either(self.world.clock.time))
        self.issued = self.tool("get_session_token", COGNITO_TOKEN)
        self.token = jsonvalue.get_str(self.issued, "token")

    def tool(self, name: str, token: str) -> Mapping[str, object]:
        """Call a tool with no arguments.

        Args:
            name: The tool.
            token: The bearer token.

        Returns:
            Its structured result.
        """
        return structured(self.client.call(name, {}, token))

    def status(self, token: str) -> object:
        """POST tools/list with a token.

        Args:
            token: The bearer token.

        Returns:
            The HTTP status.
        """
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        return self.client.post(body, token)["statusCode"]

    def test_issued(self) -> None:
        """The tool returns this endpoint and a five-minute expiry."""
        expected = dict(endpoint=ENDPOINT, expires_at="2026-01-01T00:05:00+00:00")
        includes(self.issued, expected)

    def test_list_tags(self) -> None:
        """A script holding the session token gets the same tools."""
        self.world.store.put("inbound/x", sample_raw("Hello", "Hi", {}))
        ingest(self.world.context, "inbound/x")
        includes(self.tool("list_tags", self.token), dict(tags={"inbox": 1}))
        renewed = self.tool("get_session_token", self.token)
        assert_that(renewed, has_key("token"))

    def test_expired_and_tampered(self) -> None:
        """Expired and tampered session tokens get a 401."""
        head, body, signature = self.token.split(".")
        flipped = "A" if signature[0] != "A" else "B"
        tampered = f"{head}.{body}.{flipped}{signature[1:]}"
        before = [self.status(self.token), self.status(tampered)]
        self.world.clock.advance(300)
        assert_that([*before, self.status(self.token)], equal_to([200, 401, 401]))

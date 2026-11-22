"""Tests for bearer-token validation."""

import json
import time
import unittest
from collections.abc import Mapping

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from hamcrest import assert_that, equal_to, instance_of

from ..auth import AuthError, ScopeError, TokenValidator
from .checks import failure, includes

ISSUER = "https://cognito-idp.us-east-2.amazonaws.com/us-east-2_TEST"
RESOURCE = "https://abc.lambda-url.us-east-2.on.aws/mcp"
SCOPE = f"{RESOURCE}/full"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def jwks() -> bytes:
    """Build the pool's JWKS document.

    Returns:
        The JSON bytes.
    """
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key()))
    return json.dumps(
        {"keys": [{**public, "kid": "k1", "alg": "RS256", "use": "sig"}]}
    ).encode()


def signed(overrides: Mapping[str, object], kid: str = "k1") -> str:
    """Sign an access token.

    Args:
        overrides: Claims to change.
        kid: The key ID header.

    Returns:
        The token.
    """
    claims = {
        "iss": ISSUER,
        "token_use": "access",
        "scope": f"openid {SCOPE}",
        "exp": int(time.time()) + 300,
        **overrides,
    }
    return jwt.encode(claims, KEY, algorithm="RS256", headers={"kid": kid})


class TestValidator(unittest.TestCase):
    """Cognito access tokens are checked properly."""

    def setUp(self) -> None:
        """Create a validator whose JWKS fetches are counted."""
        self.fetches: list[str] = []

        def fetch(url: str) -> bytes:
            self.fetches.append(url)
            return jwks()

        self.validator = TokenValidator.create(ISSUER, fetch)

    def check(self, value: str) -> Mapping[str, object]:
        """Validate a token.

        Args:
            value: The token.

        Returns:
            The claims.
        """
        return self.validator.validate(value, RESOURCE, SCOPE)

    def test_valid(self) -> None:
        """Good tokens pass, from either issuer form, and the JWKS is cached."""
        includes(self.check(signed({})), dict(token_use="access"))
        alternate = ISSUER.replace("cognito-idp.", "issuer-cognito-idp.")
        includes(
            self.check(signed({"iss": alternate, "aud": [RESOURCE]})),
            dict(iss=alternate),
        )
        assert_that(self.fetches, equal_to([f"{ISSUER}/.well-known/jwks.json"]))

    def test_invalid(self) -> None:
        """Bad tokens are rejected."""
        bad = [
            "garbage",
            signed({}, kid="other"),
            signed({"exp": 1}),
            signed({"iss": "https://evil.example.com"}),
            signed({"token_use": "id"}),
            signed({"aud": "https://other.example.com/mcp"}),
        ]
        for value in bad:
            assert_that(failure(lambda: self.check(value)), instance_of(AuthError))

    def test_scope(self) -> None:
        """A token without the scope is valid but forbidden."""
        assert_that(
            failure(lambda: self.check(signed({"scope": "openid"}))),
            instance_of(ScopeError),
        )

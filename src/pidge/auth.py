"""OAuth 2.1 resource-server checks for Cognito access tokens."""

import dataclasses
import json
from collections.abc import Callable, Mapping, Sequence
from typing import Protocol

import jwt

from . import jsonvalue

LEEWAY_SECONDS = 30


class AuthError(Exception):
    """The request's bearer token was missing or invalid."""


class ScopeError(Exception):
    """The token is valid but lacks the required scope."""


class BearerValidator(Protocol):
    """Checks bearer tokens."""

    def validate(self, token: str, resource: str, scope: str) -> Mapping[str, object]:
        """Check a bearer token.

        Args:
            token: The access token.
            resource: This server's resource URL.
            scope: The scope the token must carry.

        Returns:
            The claims; raises AuthError or ScopeError on failure.
        """


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class TokenValidator:
    """Validates Cognito access tokens against the pool's JWKS.

    Construct it with :meth:`create`.
    """

    _issuers: Sequence[str]
    _fetch: Callable[[str], bytes]
    _keys: dict[str, jwt.PyJWK]

    @classmethod
    def create(cls, issuer: str, fetch: Callable[[str], bytes]) -> "TokenValidator":
        """Create a validator for a user pool.

        Args:
            issuer: The pool's issuer URL (``https://cognito-idp...``).
            fetch: Retrieves a URL's body (used for the JWKS).

        Returns:
            The validator.
        """
        alternate = issuer.replace(
            "https://cognito-idp.", "https://issuer-cognito-idp."
        )
        return cls(_issuers=(issuer, alternate), _fetch=fetch, _keys={})

    def signing_key(self, kid: str) -> jwt.PyJWK:
        """Find the key that signed a token, refreshing the JWKS if needed.

        Args:
            kid: The key ID from the token header.

        Returns:
            The key.

        Raises:
            AuthError: If no key has that ID.
        """
        if kid not in self._keys:
            data = json.loads(self._fetch(f"{self._issuers[0]}/.well-known/jwks.json"))
            for key in jwt.PyJWKSet.from_dict(data).keys:
                self._keys[str(key.key_id)] = key
        if kid not in self._keys:
            raise AuthError("unknown signing key")
        return self._keys[kid]

    def validate(self, token: str, resource: str, scope: str) -> Mapping[str, object]:
        """Check a bearer token.

        Args:
            token: The access token.
            resource: This server's resource URL (the expected audience).
            scope: The scope the token must carry.

        Returns:
            The token's claims.

        Raises:
            AuthError: If the token is invalid.
            ScopeError: If the token lacks the scope.
        """
        try:
            kid = jsonvalue.get_str(jwt.get_unverified_header(token), "kid")
        except (jwt.PyJWTError, ValueError) as error:
            raise AuthError(str(error)) from error
        claims = decode_token(token, self.signing_key(kid))
        check_claims(claims, self._issuers, resource)
        if scope not in jsonvalue.get_str(claims, "scope").split():
            raise ScopeError(scope)
        return claims


def decode_token(token: str, key: jwt.PyJWK) -> Mapping[str, object]:
    """Verify a token's signature and expiry.

    Args:
        token: The access token.
        key: The signing key.

    Returns:
        The claims.

    Raises:
        AuthError: If verification fails.
    """
    try:
        claims: Mapping[str, object] = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            leeway=LEEWAY_SECONDS,
            options=dict(verify_aud=False, require=["exp", "iss"]),
        )
    except jwt.PyJWTError as error:
        raise AuthError(str(error)) from error
    return claims


def check_claims(
    claims: Mapping[str, object], issuers: Sequence[str], resource: str
) -> None:
    """Check the issuer, token use and audience of decoded claims.

    Args:
        claims: The decoded claims.
        issuers: The accepted issuers.
        resource: This server's resource URL.

    Raises:
        AuthError: If a check fails.
    """
    if claims.get("iss") not in issuers:
        raise AuthError("wrong issuer")
    if claims.get("token_use") != "access":
        raise AuthError("not an access token")
    claimed = claims.get("aud", resource)
    accepted = claimed if isinstance(claimed, list) else [claimed]
    if resource not in accepted:
        raise AuthError("wrong audience")

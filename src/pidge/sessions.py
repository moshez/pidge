"""Short-lived session tokens, so scripts can call the MCP endpoint directly.

An authenticated client calls ``get_session_token`` and hands the token and
endpoint to a script, which sends ``Authorization: Bearer <token>``. pidge
signs the tokens itself (HS256, key in SSM Parameter Store); they are valid
for five minutes and grant the same access as the Cognito token that asked.
"""

import contextlib
import dataclasses
import datetime
from collections.abc import Callable, Iterator, Mapping

import jwt
from jwt.types import Options

from .auth import AuthError, BearerValidator
from .toolkit import Secret, Services, Tool, schema

ISSUER = "pidge-session"
ALGORITHM = "HS256"
LIFETIME_SECONDS = 300
# Expiry is checked against the injected clock, not PyJWT's.
DECODE_OPTIONS: Options = {
    "verify_exp": False,
    "verify_iat": False,
    "require": ["exp", "iss", "aud"],
}


@contextlib.contextmanager
def as_auth_error() -> Iterator[None]:
    """Turn PyJWT errors into AuthError.

    Yields:
        Nothing.

    Raises:
        AuthError: If the block raised a PyJWT error.
    """
    try:
        yield
    except jwt.PyJWTError as error:
        raise AuthError(str(error)) from error


def issue(key: bytes, endpoint: str, now: float) -> Mapping[str, object]:
    """Sign a session token for an endpoint.

    Args:
        key: The signing key.
        endpoint: The MCP endpoint URL (the token's audience).
        now: The current time.

    Returns:
        ``token``, ``expires_at`` (ISO 8601) and ``endpoint``.
    """
    issued = int(now)
    expires = issued + LIFETIME_SECONDS
    claims = {"iss": ISSUER, "aud": endpoint, "iat": issued, "exp": expires}
    when = datetime.datetime.fromtimestamp(expires, datetime.UTC)
    return {
        "token": jwt.encode(claims, key, algorithm=ALGORITHM),
        "expires_at": when.isoformat(),
        "endpoint": endpoint,
    }


def verify(token: str, key: bytes, endpoint: str, now: float) -> Mapping[str, object]:
    """Check a session token's signature, issuer, audience and expiry.

    Args:
        token: The token.
        key: The signing key.
        endpoint: This server's MCP endpoint URL.
        now: The current time.

    Returns:
        The claims; raises AuthError if the token is invalid or expired.
    """
    with as_auth_error():
        claims: Mapping[str, object] = jwt.decode(
            token,
            key,
            algorithms=[ALGORITHM],
            audience=endpoint,
            issuer=ISSUER,
            options=DECODE_OPTIONS,
        )
    check_expiry(claims, now)
    return claims


def check_expiry(claims: Mapping[str, object], now: float) -> None:
    """Check that a token has not expired.

    Args:
        claims: The verified claims.
        now: The current time.

    Raises:
        AuthError: If it has expired (or its expiry is not a whole number).
    """
    expires = claims["exp"]
    if not isinstance(expires, int) or expires <= now:
        raise AuthError("session token expired")


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class SessionValidator:
    """Checks session tokens.

    Construct it with :meth:`create`.
    """

    _key: Secret
    _clock: Callable[[], float]

    @classmethod
    def create(cls, key: Secret, clock: Callable[[], float]) -> "SessionValidator":
        """Create a validator.

        Args:
            key: The signing key.
            clock: The current time.

        Returns:
            The validator.
        """
        return cls(_key=key, _clock=clock)

    def validate(self, token: str, resource: str, scope: str) -> Mapping[str, object]:
        """Check a session token; it carries every scope.

        Args:
            token: The token.
            resource: This server's MCP endpoint URL.
            scope: The required scope (always granted).

        Returns:
            The claims.
        """
        return verify(token, self._key.value(), resource, self._clock())


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class EitherValidator:
    """Sends HS256 tokens to the session validator and the rest to Cognito's.

    Construct it with :meth:`create`.
    """

    _sessions: BearerValidator
    _cognito: BearerValidator

    @classmethod
    def create(
        cls, sessions: BearerValidator, cognito: BearerValidator
    ) -> "EitherValidator":
        """Create a validator.

        Args:
            sessions: Checks session tokens.
            cognito: Checks Cognito access tokens.

        Returns:
            The validator.
        """
        return cls(_sessions=sessions, _cognito=cognito)

    def validate(self, token: str, resource: str, scope: str) -> Mapping[str, object]:
        """Check a bearer token with the validator for its kind.

        Args:
            token: The token.
            resource: This server's MCP endpoint URL.
            scope: The required scope.

        Returns:
            The claims.
        """
        with as_auth_error():
            algorithm = jwt.get_unverified_header(token).get("alg")
        chosen = self._sessions if algorithm == ALGORITHM else self._cognito
        return chosen.validate(token, resource, scope)


def run_get_session_token(services: Services, args: Mapping[str, object]) -> object:
    """Issue a session token for the endpoint this request came to.

    Args:
        services: The services.
        args: The tool arguments (none).

    Returns:
        The token, its expiry and the endpoint.
    """
    return issue(
        services.session_key.value(), services.endpoint, services.context.clock()
    )


GET_SESSION_TOKEN = Tool(
    name="get_session_token",
    description=(
        "Get a bearer token, valid for 5 minutes, that lets a script call this "
        "MCP server directly: POST JSON-RPC to `endpoint` with the header "
        "`Authorization: Bearer <token>`, starting with `initialize` and "
        "echoing its `Mcp-Session-Id` response header on every later request "
        "(a 404 means re-initialize). It has the same tools and access as "
        "this connection. Cheap to call; get a new one when it expires. It is "
        "for pidge_client (or any MCP client) calling the endpoint directly: "
        "clone https://github.com/moshez/pidge and set "
        "PYTHONPATH=<clone>/client/src."
    ),
    schema=schema({}, []),
    run=run_get_session_token,
)

SESSION_TOOLS = (GET_SESSION_TOKEN,)

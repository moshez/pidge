"""MCP session IDs that expire on every deploy, with no stored state.

``initialize`` answers with ``Mcp-Session-Id: <build>.<random>``, where the
build is the git SHA that was deployed. Every later request must echo an ID
minted by the running build; anything else (another build's ID, a malformed
one, or none at all) is answered 404, which tells MCP clients to
re-initialize and so re-read the tools and instructions.
"""

import dataclasses
import enum
import json
import re
import secrets
from collections.abc import Callable

HEADER = "mcp-session-id"
SESSION_EXPIRED = -32001
EXPIRED_MESSAGE = "session expired; re-initialize"
RANDOM_BYTES = 16
SESSION_ID = re.compile(r"(?P<build>[\x21-\x2d\x2f-\x7e]+)\.(?P<random>[\w-]{22})")


class Expiry(enum.Enum):
    """Why a request's session ID is not accepted.

    Attributes:
        ABSENT: The request has no session ID.
        MALFORMED: The ID is not ``<build>.<random>``.
        STALE: The ID was issued by another build.
    """

    ABSENT = enum.auto()
    MALFORMED = enum.auto()
    STALE = enum.auto()


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class Sessions:
    """Issues and checks session IDs for the running build.

    Attributes:
        build: The build identity (the deployed git SHA, or ``dev``).
        random: Returns a fresh URL-safe random string of 128 bits.
        log: Writes one log line.
    """

    build: str
    random: Callable[[], str]
    log: Callable[[str], None]


def random_part() -> str:
    """Draw the random part of a session ID from :mod:`secrets`.

    Returns:
        128 bits, URL-safe base64 (22 characters).
    """
    return secrets.token_urlsafe(RANDOM_BYTES)


def issue(sessions: Sessions, client: object) -> str:
    """Mint a session ID for a successful ``initialize`` and log it.

    Args:
        sessions: The sessions.
        client: The ``clientInfo`` the client sent.

    Returns:
        The session ID.
    """
    record = dict(mcp="initialize", build=sessions.build, client=client)
    sessions.log(json.dumps(record, sort_keys=True))
    return f"{sessions.build}.{sessions.random()}"


def check(sessions: Sessions, session_id: str | None) -> Expiry | None:
    """Check a request's session ID against the running build.

    Args:
        sessions: The sessions.
        session_id: The ``Mcp-Session-Id`` header, or None if absent.

    Returns:
        Why the session is expired, or None if it is current.
    """
    if session_id is None:
        return Expiry.ABSENT
    match = SESSION_ID.fullmatch(session_id)
    if match is None or not match["random"].isascii():
        return Expiry.MALFORMED
    if match["build"] != sessions.build:
        return Expiry.STALE
    return None


def log_expiry(
    sessions: Sessions, reason: Expiry, session_id: str | None, method: object
) -> None:
    """Log one refused request.

    Args:
        sessions: The sessions.
        reason: Why the session is expired.
        session_id: The header as sent.
        method: The JSON-RPC method of the request, if any.
    """
    record: dict[str, object] = dict(
        mcp="expired", reason=reason.name.lower(), build=sessions.build, method=method
    )
    if reason is Expiry.STALE and session_id is not None:
        record["old_build"] = session_id.rpartition(".")[0]
    sessions.log(json.dumps(record, sort_keys=True))

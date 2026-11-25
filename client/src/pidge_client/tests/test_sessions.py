"""MCP sessions: initialize first, and re-initialize once when pidge expires one."""

import dataclasses
import json
import unittest
from collections.abc import Mapping, MutableSequence

from hamcrest import assert_that, equal_to

from pidge import mcpsession

from .. import calls
from ..transport import Reply, Session, SessionExpired, Transport
from .mailbox import InProcess, empty_mailbox


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class Recording:
    """A transport that records each POST's method and session header.

    Attributes:
        inner: The transport that answers.
        seen: Receives ``(method, session ID or None)`` per POST.
        forged: If set, replaces the session ID on every ``tools/call``, so
            the server always refuses it.
    """

    inner: Transport
    seen: MutableSequence[tuple[str, str | None]]
    forged: str = ""

    def post(self, url: str, headers: Mapping[str, str], body: bytes) -> Reply:
        """Record and forward a POST.

        Args:
            url: The URL.
            headers: The request headers.
            body: The request body.

        Returns:
            The inner transport's reply.
        """
        method = json.loads(body)["method"]
        session_id = headers.get("Mcp-Session-Id")
        self.seen.append((method, session_id))
        if self.forged and method == "tools/call":
            headers = {**headers, "Mcp-Session-Id": self.forged}
        return self.inner.post(url, dict(headers), body)

    def get(self, url: str) -> Reply:  # pragma: no cover
        """Refuse a GET: these tests download nothing.

        Args:
            url: The URL.

        Returns:
            Never returns.

        Raises:
            AssertionError: Always.
        """
        raise AssertionError(f"unexpected GET {url}")


def redeployed(transport: InProcess, build: str) -> InProcess:  # noqa: SLD802
    """The same mailbox, served by another build.

    Args:
        transport: The in-process server.
        build: The new build.

    Returns:
        The redeployed server.
    """
    sessions = mcpsession.Sessions(build=build, random=lambda: "B" * 22, log=print)
    services = dataclasses.replace(transport.services, sessions=sessions)
    return dataclasses.replace(transport, services=services)


class TestSessions(unittest.TestCase):
    """The client's side of pidge's session expiry."""

    def setUp(self) -> None:
        """Connect a recording session to an empty mailbox."""
        mailbox = empty_mailbox(self)
        assert isinstance(mailbox.transport, InProcess)
        self.server = mailbox.transport
        self.posts: list[tuple[str, str | None]] = []
        self.session = Session(
            endpoint=mailbox.session.endpoint,
            token=mailbox.session.token,
            transport=Recording(inner=self.server, seen=self.posts),
        )

    def recording(self, server: Transport, forged: str) -> Session:
        """This session (sharing its handshake) over another recording transport.

        Args:
            server: The server.
            forged: Forge this session ID on tools/call (empty: don't).

        Returns:
            The session.
        """
        transport = Recording(inner=server, seen=self.posts, forged=forged)
        return dataclasses.replace(self.session, transport=transport)

    def test_initializes_first(self) -> None:
        """The first call initializes, then echoes the session ID."""
        calls.list_tags(self.session)
        calls.list_tags(self.session)
        first = "0123abc." + "0" * 22
        assert_that(
            self.posts,
            equal_to(
                [
                    ("initialize", None),
                    ("notifications/initialized", first),
                    ("tools/call", first),
                    ("tools/call", first),
                ]
            ),
        )

    def test_reinitializes_once(self) -> None:
        """A stale session ID triggers one re-initialize and one retry."""
        calls.list_tags(self.session)
        del self.posts[:]
        moved = self.recording(redeployed(self.server, "newbuild"), "")
        assert_that(calls.list_tags(moved), equal_to({}))
        old, new = "0123abc." + "0" * 22, "newbuild." + "B" * 22
        assert_that(
            self.posts,
            equal_to(
                [
                    ("tools/call", old),
                    ("initialize", None),
                    ("notifications/initialized", new),
                    ("tools/call", new),
                ]
            ),
        )

    def test_retries_only_once(self) -> None:
        """If the retry is refused too, SessionExpired is raised."""
        with self.assertRaises(SessionExpired):
            calls.list_tags(self.recording(self.server, "garbage"))
        methods = [method for method, _ in self.posts]
        handshake = ["initialize", "notifications/initialized", "tools/call"]
        assert_that(methods, equal_to(handshake * 2))

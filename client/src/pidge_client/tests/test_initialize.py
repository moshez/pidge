"""Sessions initialize before their first request and keep no session ID."""

import dataclasses
import json
import unittest
from collections.abc import Mapping, MutableSequence, Sequence

from hamcrest import assert_that, equal_to

from .. import calls
from ..calls import SearchArgs
from ..transport import AuthError, HTTPStatusError, Reply, Session, Transport
from .mailbox import empty_mailbox


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class Recording:
    """A transport that records each POST and can override some replies.

    Attributes:
        inner: The transport that answers.
        seen: Receives ``(method, headers)`` per POST.
        override: Canned replies by JSON-RPC method.
    """

    inner: Transport
    seen: MutableSequence[tuple[str, Mapping[str, str]]]
    override: Mapping[str, Reply]

    def post(self, url: str, headers: Mapping[str, str], body: bytes) -> Reply:
        """Record a POST, then answer it.

        Args:
            url: The URL.
            headers: The request headers.
            body: The request body.

        Returns:
            The canned reply for its method, or the inner transport's.
        """
        method = json.loads(body)["method"]
        self.seen.append((method, headers))
        if method in self.override:
            return self.override[method]
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


class TestInitialize(unittest.TestCase):
    """The client's handshake with an initialize-era server."""

    def setUp(self) -> None:
        """Connect to an empty mailbox."""
        self.mailbox = empty_mailbox(self)
        self.seen: list[tuple[str, Mapping[str, str]]] = []

    def session(self, override: Mapping[str, Reply]) -> Session:
        """A recording session.

        Args:
            override: Canned replies by method.

        Returns:
            The session.
        """
        transport = Recording(
            inner=self.mailbox.transport, seen=self.seen, override=override
        )
        return dataclasses.replace(self.mailbox.session, transport=transport)

    def methods(self) -> Sequence[str]:
        """The methods posted so far.

        Returns:
            Their names, in order.
        """
        return [method for method, _ in self.seen]

    def test_initializes_once(self) -> None:
        """initialize and its notification go first, once, with no session ID."""
        session = self.session({})
        calls.list_tags(session)
        calls.list_tags(session)
        assert_that(
            self.methods(),
            equal_to(
                [
                    "initialize",
                    "notifications/initialized",
                    "tools/call",
                    "tools/call",
                ]
            ),
        )
        sent = [{name.lower() for name in headers} for _, headers in self.seen]
        assert_that(any("mcp-session-id" in names for names in sent), equal_to(False))

    def test_404_is_an_error(self) -> None:
        """A 404 is an ordinary HTTP error: no re-initialize, no retry."""
        session = self.session({"tools/call": Reply(status=404, body=b"gone")})
        with self.assertRaises(HTTPStatusError) as caught:
            calls.search(session, SearchArgs())
        status, _ = caught.exception.args
        assert_that(status, equal_to(404))
        handshake = ["initialize", "notifications/initialized"]
        assert_that(self.methods(), equal_to([*handshake, "tools/call"]))

    def test_notification_refused(self) -> None:
        """The initialized notification must get a 202."""
        for status, error in [(401, AuthError), (500, HTTPStatusError)]:
            with self.subTest(status):
                refused = Reply(status=status, body=b"no")
                session = self.session({"notifications/initialized": refused})
                with self.assertRaises(error):
                    calls.list_tags(session)
                assert_that(session.handshake.done, equal_to(False))

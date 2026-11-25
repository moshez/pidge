"""refresh_tools, and reading its server-sent-events reply."""

import unittest

from hamcrest import assert_that, equal_to

from pidge.tests.mcpclient import TOOLS

from .. import calls
from ..jsonshape import ShapeError
from ..transport import Reply, Session, response_message, stream_messages
from .mailbox import empty_mailbox, http_server

STREAM = {"content-type": "text/event-stream"}


class TestRefreshTools(unittest.TestCase):
    """The typed call, in process and over HTTP."""

    def check(self, session: Session) -> None:
        """refresh_tools lists every server tool and the build.

        Args:
            session: The session.
        """
        refreshed = calls.refresh_tools(session)
        names = [tool.name for tool in refreshed.tools]
        assert_that(names, equal_to([tool.name for tool in TOOLS]))
        assert_that(refreshed.build, equal_to("dev"))
        assert_that(
            refreshed.tools[-1].summary,
            equal_to(
                "Re-announce this server's tools; "
                "call it if a tool you expected is missing."
            ),
        )

    def test_in_process(self) -> None:
        """The in-process server answers over SSE, and the client parses it."""
        self.check(empty_mailbox(self).session)

    def test_over_http(self) -> None:
        """The same over a real HTTP server and urllib."""
        mailbox = empty_mailbox(self)
        with http_server(mailbox.transport) as base:
            self.check(Session(endpoint=f"{base}/mcp", token=mailbox.session.token))


class TestEventStream(unittest.TestCase):
    """Parsing SSE bodies."""

    def test_notifications_ignored(self) -> None:
        """The response event is found after a notification."""
        body = (
            b'event: message\r\ndata: {"jsonrpc": "2.0", "method": "n"}\r\n\r\n'
            b": a comment\n\n"
            b'event: message\ndata: {"jsonrpc": "2.0",\ndata:  "id": 1}\n\n'
        )
        message = response_message(Reply(status=200, body=body, headers=STREAM))
        assert_that(message, equal_to({"jsonrpc": "2.0", "id": 1}))

    def test_no_response(self) -> None:
        """A stream with only notifications is malformed."""
        body = b'data: {"jsonrpc": "2.0", "method": "n"}\n\n'
        assert_that(len(stream_messages(body.decode())), equal_to(1))
        with self.assertRaises(ShapeError):
            response_message(Reply(status=200, body=body, headers=STREAM))

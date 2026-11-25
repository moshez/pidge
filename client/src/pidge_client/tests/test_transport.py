"""Tests for the JSON-RPC session and transports."""

import dataclasses
import unittest
from collections.abc import Mapping

from hamcrest import assert_that, contains_string, equal_to, has_length

from .. import calls
from ..jsonshape import ShapeError
from ..transport import (
    AuthError,
    HTTPStatusError,
    Reply,
    RpcError,
    Session,
    ToolError,
    UrllibTransport,
    call_tool,
    list_tools,
    rpc,
    rpc_result,
    tool_content,
)
from .mailbox import TOKEN, empty_mailbox, http_server, receive, dated, BASE


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class Canned:
    """A transport that always gives the same reply.

    Attributes:
        reply: The reply.
    """

    reply: Reply

    def post(self, url: str, headers: Mapping[str, str], body: bytes) -> Reply:
        """Return the reply.

        Args:
            url: Ignored.
            headers: Ignored.
            body: Ignored.

        Returns:
            The reply.
        """
        return self.reply

    def get(self, url: str) -> Reply:
        """Return the reply.

        Args:
            url: Ignored.

        Returns:
            The reply.
        """
        return self.reply


def answering(status: int, body: bytes) -> Session:
    """Build a session whose transport always replies the same way.

    Args:
        status: The HTTP status.
        body: The body.

    Returns:
        The session.
    """
    return Session(
        endpoint="https://x/mcp",
        token="t",
        transport=Canned(reply=Reply(status=status, body=body)),
    )


class TestSession(unittest.TestCase):
    """Errors and results through the in-process transport."""

    def test_tool_error(self) -> None:
        """An isError result raises ToolError with the tool's text."""
        session = empty_mailbox(self).session
        with self.assertRaises(ToolError) as caught:
            calls.get_message(session, "01ARZ3NDEKTSV4RRFFQ69G5FAV")
        assert_that(str(caught.exception), contains_string("no message"))

    def test_rpc_error(self) -> None:
        """A JSON-RPC error raises RpcError with the code and message."""
        session = empty_mailbox(self).session
        with self.assertRaises(RpcError) as caught:
            call_tool(session, "no_such_tool", {})
        assert_that(caught.exception.args[0], equal_to(-32602))

    def test_auth_error(self) -> None:
        """A 401 raises AuthError."""
        session = dataclasses.replace(empty_mailbox(self).session, token="stale")
        with self.assertRaises(AuthError):
            calls.list_tags(session)

    def test_other_status(self) -> None:
        """Any other non-200 status raises HTTPStatusError."""
        session = answering(502, b"bad gateway")
        assert_that(session.transport.get("https://x/y").status, equal_to(502))
        with self.assertRaises(HTTPStatusError) as caught:
            calls.list_tags(session)
        assert_that(caught.exception.args, equal_to((502, "bad gateway")))

    def test_request_ids_increase(self) -> None:
        """Each request gets the next ID."""
        session = empty_mailbox(self).session
        rpc(session, "ping", {})
        assert_that(next(session.ids), equal_to(2))

    def test_list_tools(self) -> None:
        """tools/list returns the tool descriptions."""
        names = [tool["name"] for tool in list_tools(empty_mailbox(self).session)]
        assert_that(names, has_length(7))


class TestUnwrap(unittest.TestCase):
    """Unwrapping responses."""

    def test_missing_result(self) -> None:
        """A response with no result and no error is malformed."""
        with self.assertRaises(ShapeError):
            rpc_result(Reply(status=200, body=b'{"jsonrpc": "2.0", "id": 1}'))

    def test_error_texts_joined(self) -> None:
        """Every text item of an error result is kept."""
        result = {
            "isError": True,
            "content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}],
        }
        with self.assertRaises(ToolError) as caught:
            tool_content(result)
        assert_that(str(caught.exception), equal_to("a\nb"))


class TestUrllib(unittest.TestCase):
    """The default transport, against a real local HTTP server."""

    def test_round_trip(self) -> None:
        """POSTs reach the server, 401s become AuthError, GETs download."""
        mailbox = empty_mailbox(self)
        ulid = receive(mailbox, "m", dated("Over HTTP", BASE))
        with http_server(mailbox.transport) as base:
            session = Session(endpoint=f"{base}/mcp", token=TOKEN)
            assert_that(calls.list_tags(session), equal_to({"inbox": 1}))
            with self.assertRaises(AuthError):
                calls.list_tags(dataclasses.replace(session, token="stale"))
            reply = UrllibTransport.create().get(f"{base}/memory:msgs/{ulid}/body.txt")
        assert_that(reply, equal_to(Reply(status=200, body=b"Body of Over HTTP\n")))

    def test_missing_object(self) -> None:
        """A presigned URL for a missing object is a 404."""
        reply = empty_mailbox(self).transport.get("memory:msgs/nothing?filename=x")
        assert_that(reply.status, equal_to(404))

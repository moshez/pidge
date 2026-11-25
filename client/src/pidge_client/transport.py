"""Send JSON-RPC ``tools/call`` requests to a pidge MCP endpoint.

A ``Session`` sends ``initialize`` (and ``notifications/initialized``)
before its first request. pidge keeps no MCP sessions, so there is no
session ID to keep.
"""

import dataclasses
import itertools
import json
import urllib.error
import urllib.request
from collections.abc import Iterator, Mapping, Sequence
from typing import Protocol

from . import jsonshape

PROTOCOL_VERSION = "2025-11-25"
TIMEOUT = 60.0
CLIENT_INFO = {"name": "pidge_client", "version": "1"}


class ToolError(Exception):
    """A tool ran and failed (an ``isError`` result); the argument is its text."""


class RpcError(Exception):
    """A JSON-RPC error response; the arguments are the code and the message."""


class AuthError(Exception):
    """The endpoint refused the bearer token (HTTP 401); get a fresh token."""


class HTTPStatusError(Exception):
    """An HTTP status other than 200 or 401; the arguments are status and body."""


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class Reply:
    """An HTTP response.

    Attributes:
        status: The HTTP status code.
        body: The response body.
        headers: The response headers, with lowercase names.
    """

    status: int
    body: bytes
    headers: Mapping[str, str] = dataclasses.field(default_factory=dict)


class Transport(Protocol):
    """Moves bytes over HTTP."""

    def post(self, url: str, headers: Mapping[str, str], body: bytes) -> Reply:
        """POST a request body.

        Args:
            url: The URL.
            headers: The request headers.
            body: The request body.

        Returns:
            The response, whatever its status.
        """

    def get(self, url: str) -> Reply:
        """GET a URL with no extra headers.

        Args:
            url: The URL.

        Returns:
            The response, whatever its status.
        """


def send(request: urllib.request.Request, timeout: float) -> Reply:
    """Send a request with urllib, turning HTTP error statuses into replies.

    Args:
        request: The request.
        timeout: Seconds to wait.

    Returns:
        The response.
    """
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            headers = {name.lower(): value for name, value in response.headers.items()}
            return Reply(status=response.status, body=response.read(), headers=headers)
    except urllib.error.HTTPError as error:
        with error:
            return Reply(status=error.code, body=error.read())


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class UrllibTransport:
    """The default transport: :mod:`urllib.request`.

    Construct it with :meth:`create`.
    """

    _timeout: float

    @classmethod
    def create(cls, timeout: float = TIMEOUT) -> "UrllibTransport":
        """Create a transport.

        Args:
            timeout: Seconds to wait for each request.

        Returns:
            The transport.
        """
        return cls(_timeout=timeout)

    def post(self, url: str, headers: Mapping[str, str], body: bytes) -> Reply:
        """POST a request body.

        Args:
            url: The URL.
            headers: The request headers.
            body: The request body.

        Returns:
            The response.
        """
        request = urllib.request.Request(
            url, data=body, headers=dict(headers), method="POST"
        )
        return send(request, self._timeout)

    def get(self, url: str) -> Reply:
        """GET a URL.

        Args:
            url: The URL.

        Returns:
            The response.
        """
        return send(urllib.request.Request(url, method="GET"), self._timeout)


def request_ids() -> Iterator[int]:
    """Count JSON-RPC request IDs from 1.

    Returns:
        The counter.
    """
    return itertools.count(1)


def check_status(reply: Reply, expected: int) -> None:
    """Check an HTTP reply's status.

    Args:
        reply: The HTTP reply.
        expected: The status that means success.

    Raises:
        AuthError: On HTTP 401.
        HTTPStatusError: On any other unexpected status (404 included).
    """
    if reply.status == 401:
        raise AuthError(reply.body.decode("utf-8", "replace"))
    if reply.status != expected:
        raise HTTPStatusError(reply.status, reply.body.decode("utf-8", "replace"))


def stream_messages(body: str) -> Sequence[Mapping[str, object]]:
    """Parse a server-sent-events body into its JSON-RPC messages.

    Args:
        body: The body.

    Returns:
        Each event's data, decoded, in order.
    """
    messages = []
    for block in body.replace("\r\n", "\n").split("\n\n"):
        data = [
            line.removeprefix("data:").removeprefix(" ")
            for line in block.split("\n")
            if line.startswith("data:")
        ]
        if data:
            messages.append(jsonshape.as_dict(jsonshape.loads("\n".join(data))))
    return messages


def response_message(reply: Reply) -> Mapping[str, object]:
    """Find the JSON-RPC response in a reply, plain JSON or SSE.

    In an event stream, notifications and requests from the server are
    ignored; the first message without a ``method`` is the response.

    Args:
        reply: The HTTP reply.

    Returns:
        The response message.
    """
    content_type = reply.headers.get("content-type", "")
    if not content_type.startswith("text/event-stream"):
        return jsonshape.as_dict(jsonshape.loads(reply.body))
    messages = stream_messages(reply.body.decode("utf-8"))
    responses = [message for message in messages if "method" not in message]
    if not responses:
        jsonshape.fail("a JSON-RPC response event", None)
    return responses[0]


def rpc_result(reply: Reply) -> Mapping[str, object]:
    """Check an HTTP reply to a JSON-RPC request and return its result.

    Args:
        reply: The HTTP reply.

    Returns:
        The JSON-RPC result.

    Raises:
        RpcError: On a JSON-RPC error response.
    """
    check_status(reply, 200)
    message = response_message(reply)
    if "error" in message:
        error = jsonshape.as_dict(message["error"])
        raise RpcError(
            jsonshape.get_int(error, "code"), jsonshape.get_str(error, "message")
        )
    return jsonshape.as_dict(message.get("result"))


def tool_content(result: Mapping[str, object]) -> Mapping[str, object]:
    """Unwrap a ``tools/call`` result.

    Args:
        result: The JSON-RPC result.

    Returns:
        The structured content.

    Raises:
        ToolError: If the tool reported an error.
    """
    if result.get("isError"):
        texts = [
            jsonshape.get_str(item, "text")
            for item in jsonshape.get_objects(result, "content")
        ]
        raise ToolError("\n".join(texts))
    return jsonshape.as_dict(result.get("structuredContent"))


# Mutable on purpose: it records, across requests, that initialize was sent.
@dataclasses.dataclass(slots=True, kw_only=True)
class Handshake:  # noqa: SLD501
    """Whether a Session has initialized: its only mutable part.

    Attributes:
        done: True once ``initialize`` succeeded.
    """

    done: bool = False


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class Session:
    """A pidge MCP endpoint and a bearer token for it.

    Attributes:
        endpoint: The MCP endpoint URL (``get_session_token``'s ``endpoint``).
        token: The bearer token.
        transport: How requests travel (default: urllib).
        ids: Supplies JSON-RPC request IDs.
        handshake: Whether ``initialize`` has been sent.
    """

    endpoint: str
    token: str
    transport: Transport = dataclasses.field(default_factory=UrllibTransport.create)
    ids: Iterator[int] = dataclasses.field(default_factory=request_ids)
    handshake: Handshake = dataclasses.field(default_factory=Handshake)


def post(session: Session, message: Mapping[str, object]) -> Reply:
    """POST one JSON-RPC message.

    Args:
        session: The session.
        message: The message.

    Returns:
        The HTTP reply.
    """
    headers = {
        "Authorization": f"Bearer {session.token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": PROTOCOL_VERSION,
    }
    body = json.dumps(message).encode("utf-8")
    return session.transport.post(session.endpoint, headers, body)


def request(
    session: Session, method: str, params: Mapping[str, object]
) -> Mapping[str, object]:
    """Send one JSON-RPC request.

    Args:
        session: The session.
        method: The method.
        params: The parameters.

    Returns:
        The result.
    """
    message = dict(jsonrpc="2.0", id=next(session.ids), method=method, params=params)
    return rpc_result(post(session, message))


def initialize(session: Session) -> Mapping[str, object]:
    """Send ``initialize`` and ``notifications/initialized``.

    Args:
        session: The session.

    Returns:
        The ``initialize`` result.
    """
    params = dict(
        protocolVersion=PROTOCOL_VERSION, capabilities={}, clientInfo=CLIENT_INFO
    )
    result = request(session, "initialize", params)
    reply = post(session, dict(jsonrpc="2.0", method="notifications/initialized"))
    check_status(reply, 202)
    session.handshake.done = True
    return result


def rpc(
    session: Session, method: str, params: Mapping[str, object]
) -> Mapping[str, object]:
    """Make a JSON-RPC request, initializing first if the session is new.

    Args:
        session: The session.
        method: The method.
        params: The parameters.

    Returns:
        The result.
    """
    if not session.handshake.done:
        initialize(session)
    return request(session, method, params)


def call_tool(
    session: Session, name: str, arguments: Mapping[str, object]
) -> Mapping[str, object]:
    """Call a tool.

    Args:
        session: The session.
        name: The tool name.
        arguments: The arguments.

    Returns:
        The tool's structured result.
    """
    return tool_content(
        rpc(session, "tools/call", dict(name=name, arguments=arguments))
    )


def list_tools(session: Session) -> Sequence[Mapping[str, object]]:
    """List the server's tools.

    Args:
        session: The session.

    Returns:
        Each tool's ``name``, ``description`` and ``inputSchema``.
    """
    return jsonshape.get_objects(rpc(session, "tools/list", {}), "tools")

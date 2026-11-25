"""A stateless MCP server over JSON-RPC (streamable HTTP, JSON responses)."""

import dataclasses
import json
from collections.abc import Callable, Mapping, Sequence

from . import jsonvalue
from .mcpsession import (
    EXPIRED_MESSAGE,
    HEADER,
    SESSION_EXPIRED,
    check,
    issue,
    log_expiry,
)
from .toolkit import Services, Tool, ToolError

SUPPORTED_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
REPOSITORY = "https://github.com/moshez/pidge"
CLIENT_EXAMPLE = (
    "import datetime\n"
    "from pidge_client import SearchArgs, Session, iter_search, views\n"
    'session = Session(endpoint="<endpoint>", token="<token>")\n'
    'hits = list(iter_search(session, SearchArgs(tags=["inbox"])))\n'
    "print(views.render_inbox(hits, datetime.datetime.now().astimezone()))\n"
)
INSTRUCTIONS = (
    "This is a personal mailbox. Search first, then read messages by id. "
    "Sending is asynchronous: send_email returns an outbox id; search "
    "tags=['outbox'] to see what is still queued.\n\n"
    "For anything beyond a few tool calls (bulk triage, rendering the inbox "
    "or a message, walking all search pages), call get_session_token, then "
    f"in a shell clone {REPOSITORY} and use pidge_client with "
    "PYTHONPATH=<clone>/client/src: no install needed, stdlib only. Keep "
    "results in the script and print only what's needed. For example:\n\n"
    + CLIENT_EXAMPLE
)

Method = Callable[[Services, Sequence[Tool], Mapping[str, object]], object]


class RPCError(Exception):
    """A JSON-RPC error; its arguments are the code and the message."""


def initialize(
    services: Services, tools: Sequence[Tool], params: Mapping[str, object]
) -> object:
    """Answer ``initialize``, negotiating the protocol version.

    Args:
        services: The services.
        tools: The tools.
        params: The request parameters.

    Returns:
        The server's capabilities.
    """
    requested = jsonvalue.get_str(params, "protocolVersion")
    version = requested if requested in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0]
    return {
        "protocolVersion": version,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": "pidge", "version": "1"},
        "instructions": INSTRUCTIONS,
    }


def ping(
    services: Services, tools: Sequence[Tool], params: Mapping[str, object]
) -> object:
    """Answer ``ping``.

    Args:
        services: The services.
        tools: The tools.
        params: The request parameters.

    Returns:
        An empty result.
    """
    return {}


def list_tools(
    services: Services, tools: Sequence[Tool], params: Mapping[str, object]
) -> object:
    """Answer ``tools/list``.

    Args:
        services: The services.
        tools: The tools.
        params: The request parameters.

    Returns:
        The tool descriptions.
    """
    return {
        "tools": [
            {
                "name": tool.name,
                "description": tool.description,
                "inputSchema": tool.schema,
            }
            for tool in tools
        ]
    }


def tool_result(tool: Tool, services: Services, args: Mapping[str, object]) -> object:
    """Run a tool and wrap its result for ``tools/call``.

    Args:
        tool: The tool.
        services: The services.
        args: The arguments.

    Returns:
        The result; tool failures are results with ``isError`` set.
    """
    try:
        result = tool.run(services, args)
    except ToolError as error:
        return {"content": [{"type": "text", "text": str(error)}], "isError": True}
    text = json.dumps(result, indent=1, sort_keys=True)
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": result,
        "isError": False,
    }


def call_tool(
    services: Services, tools: Sequence[Tool], params: Mapping[str, object]
) -> object:
    """Answer ``tools/call``.

    Args:
        services: The services.
        tools: The tools.
        params: The request parameters.

    Returns:
        The tool result.

    Raises:
        RPCError: If the tool does not exist or the arguments are not an object.
    """
    name = jsonvalue.get_str(params, "name")
    by_name = {tool.name: tool for tool in tools}
    if name not in by_name:
        raise RPCError(INVALID_PARAMS, f"unknown tool {name!r}")
    try:
        args = jsonvalue.get_dict(params, "arguments")
    except jsonvalue.JSONShapeError:
        raise RPCError(INVALID_PARAMS, "arguments must be an object") from None
    return tool_result(by_name[name], services, args)


METHODS: Mapping[str, Method] = {
    "initialize": initialize,
    "ping": ping,
    "tools/list": list_tools,
    "tools/call": call_tool,
}


def error_response(request_id: object, code: int, text: str) -> Mapping[str, object]:
    """Build a JSON-RPC error response.

    Args:
        request_id: The request ID.
        code: The JSON-RPC error code.
        text: The error message.

    Returns:
        The response.
    """
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": text},
    }


def run_method(
    services: Services, tools: Sequence[Tool], message: Mapping[str, object]
) -> Mapping[str, object]:
    """Run a request and build its response.

    Args:
        services: The services.
        tools: The tools.
        message: The request.

    Returns:
        The response.
    """
    request_id = message.get("id")
    method = METHODS.get(str(message["method"]))
    if method is None:
        return error_response(
            request_id, METHOD_NOT_FOUND, f"unknown method {message['method']!r}"
        )
    try:
        result = method(services, tools, jsonvalue.get_dict(message, "params"))
    except RPCError as error:
        return error_response(request_id, error.args[0], error.args[1])
    except jsonvalue.JSONShapeError as error:
        return error_response(request_id, INVALID_PARAMS, str(error))
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class Answer:
    """How to answer one MCP POST.

    Attributes:
        status: The HTTP status.
        headers: Extra response headers.
        payload: The JSON-RPC response, or None for an empty body.
    """

    status: int
    headers: Mapping[str, str]
    payload: Mapping[str, object] | None


def start_session(
    services: Services, tools: Sequence[Tool], message: Mapping[str, object]
) -> Answer:
    """Answer ``initialize``, issuing a session ID if it succeeds.

    Args:
        services: The services.
        tools: The tools.
        message: The request.

    Returns:
        The answer.
    """
    response = run_method(services, tools, message)
    if "result" not in response:
        return Answer(status=200, headers={}, payload=response)
    params = jsonvalue.as_dict(message["params"])
    session_id = issue(services.sessions, params.get("clientInfo"))
    return Answer(status=200, headers={HEADER: session_id}, payload=response)


def in_session(
    services: Services, message: Mapping[str, object], session_id: str | None
) -> Answer | None:
    """Refuse a message unless its session ID was issued by this build.

    Args:
        services: The services.
        message: The JSON-RPC message.
        session_id: The ``Mcp-Session-Id`` header, or None if absent.

    Returns:
        A 404 answer telling the client to re-initialize, or None to proceed.
    """
    expired = check(services.sessions, session_id)
    if expired is None:
        return None
    log_expiry(services.sessions, expired, session_id, message.get("method"))
    payload = error_response(message.get("id"), SESSION_EXPIRED, EXPIRED_MESSAGE)
    return Answer(status=404, headers={}, payload=payload)


def handle_rpc(
    services: Services, tools: Sequence[Tool], body: bytes, session_id: str | None
) -> Answer:
    """Handle one JSON-RPC message.

    ``initialize`` starts a session; anything else needs a session ID issued
    by this build, or it gets a 404 so that the client re-initializes.

    Args:
        services: The services.
        tools: The tools.
        body: The request body.
        session_id: The ``Mcp-Session-Id`` header, or None if absent.

    Returns:
        The answer: an empty 202 for a notification (or a client response).
    """
    try:
        message = jsonvalue.loads_dict(body)
    except (ValueError, jsonvalue.JSONShapeError):
        payload = error_response(None, PARSE_ERROR, "expected a JSON object")
        return Answer(status=200, headers={}, payload=payload)
    if "method" not in message or message.get("id") is None:
        return in_session(services, message, session_id) or Answer(
            status=202, headers={}, payload=None
        )
    if message["method"] == "initialize":
        return start_session(services, tools, message)
    return in_session(services, message, session_id) or Answer(
        status=200, headers={}, payload=run_method(services, tools, message)
    )

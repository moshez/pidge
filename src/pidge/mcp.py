"""A stateless MCP server over JSON-RPC (streamable HTTP, JSON responses)."""

import dataclasses
import json
from collections.abc import Callable, Mapping, Sequence

from . import jsonvalue
from .refresh import NAME as REFRESH_TOOLS
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
    "tags=['outbox'] to see what is still queued. To attach a new file, "
    "call put_attachment with its name and exact size, PUT the bytes to the "
    "returned URL, then pass {upload: upload_id} in send_email's "
    "attach_from.\n"
    f"If a tool you expected is missing, call {REFRESH_TOOLS}.\n\n"
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
        The server's capabilities, with the build as ``serverInfo.version``.
    """
    requested = jsonvalue.get_str(params, "protocolVersion")
    version = requested if requested in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0]
    build = services.build
    record = dict(mcp="initialize", build=build.name, client=params.get("clientInfo"))
    build.log(json.dumps(record, sort_keys=True))
    return {
        "protocolVersion": version,
        "capabilities": {"tools": {"listChanged": True}},
        "serverInfo": {"name": "pidge", "version": build.name},
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
    every = dataclasses.replace(services, tools=tools)
    return tool_result(by_name[name], every, args)


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
    """The reply to one JSON-RPC message.

    Attributes:
        response: The response, or None for a notification.
        announce: Whether to send ``notifications/tools/list_changed``
            ahead of the response (a successful ``refresh_tools`` call).
    """

    response: Mapping[str, object] | None
    announce: bool = False


def announces(message: Mapping[str, object], response: Mapping[str, object]) -> bool:
    """Tell whether a response answers a successful ``refresh_tools`` call.

    Args:
        message: The request.
        response: Its response.

    Returns:
        True if the tool list should be re-announced with it.
    """
    if message["method"] != "tools/call" or "result" not in response:
        return False
    return jsonvalue.get_dict(message, "params").get("name") == REFRESH_TOOLS


def handle_rpc(services: Services, tools: Sequence[Tool], body: bytes) -> Answer:
    """Handle one JSON-RPC message.

    Args:
        services: The services.
        tools: The tools.
        body: The request body.

    Returns:
        The answer; its response is None for a notification (or a client
        response).
    """
    try:
        message = jsonvalue.loads_dict(body)
    except (ValueError, jsonvalue.JSONShapeError):
        return Answer(
            response=error_response(None, PARSE_ERROR, "expected a JSON object")
        )
    if "method" not in message or message.get("id") is None:
        return Answer(response=None)
    response = run_method(services, tools, message)
    return Answer(response=response, announce=announces(message, response))

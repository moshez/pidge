"""An in-process MCP client for tests: builds Function URL events."""

import base64
import dataclasses
import json
from collections.abc import Mapping, Sequence

from hamcrest import assert_that, equal_to

from .. import jsonvalue
from ..auth import BearerValidator
from ..sessions import SESSION_TOOLS
from ..toolkit import Services
from ..tools_read import READ_TOOLS
from ..tools_write import WRITE_TOOLS
from ..mcpsession import HEADER
from ..web import route
from .sessionkey import SESSION_KEY, predictable_sessions
from .fakes import World, new_cache

HOST = "abc.lambda-url.us-east-2.on.aws"
TOOLS = (*READ_TOOLS, *WRITE_TOOLS, *SESSION_TOOLS)
OWN = "<this client's session>"


def request(method: str, path: str, body: object, token: str) -> Mapping[str, object]:
    """Build a Function URL event.

    Args:
        method: The HTTP method.
        path: The path.
        body: The JSON body (bytes are sent base64-encoded as they are).
        token: The bearer token, or empty for none.

    Returns:
        The event.
    """
    headers = {"authorization": f"Bearer {token}"} if token else {}
    event: dict[str, object] = {
        "rawPath": path,
        "headers": headers,
        "requestContext": {"domainName": HOST, "http": {"method": method}},
    }
    if isinstance(body, bytes):
        event["body"] = base64.b64encode(body).decode()
        event["isBase64Encoded"] = True
    else:
        event["body"] = json.dumps(body)
    return event


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class Client:
    """Calls the MCP endpoint of a fake deployment.

    Attributes:
        services: The services behind the endpoint.
        validator: The token validator.
        logged: The server's log lines.
        session_id: The session ID from ``initialize``, sent by default.
    """

    services: Services
    validator: BearerValidator
    logged: Sequence[str]
    session_id: str

    @classmethod
    def create(
        cls, world: World, validator: BearerValidator, token: str = "good"
    ) -> "Client":
        """Wire up a client.

        Args:
            world: The environment.
            validator: The token validator.
            token: The bearer token to initialize with.

        Returns:
            The client, with a session started.
        """
        logged: list[str] = []
        services = Services(
            context=world.context,
            cache=new_cache(world, "cache"),
            session_key=SESSION_KEY,
            sessions=predictable_sessions(logged.append),
        )
        body = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        event = request("POST", "/mcp", body, token)
        headers = jsonvalue.as_dict(route(event, services, validator, TOOLS)["headers"])
        session_id = jsonvalue.get_str(headers, HEADER)
        return cls(
            services=services, validator=validator, logged=logged, session_id=session_id
        )

    def post(
        self, body: object, token: str = "good", session_id: str | None = OWN
    ) -> Mapping[str, object]:
        """POST to /mcp.

        Args:
            body: The JSON-RPC body.
            token: The bearer token.
            session_id: The session header (by default this client's session),
                or None for no header.

        Returns:
            The HTTP response.
        """
        event = request("POST", "/mcp", body, token)
        if session_id is not None:
            headers = jsonvalue.as_dict(event["headers"])
            event = {
                **event,
                "headers": {
                    **headers,
                    HEADER: self.session_id if session_id == OWN else session_id,
                },
            }
        return route(event, self.services, self.validator, TOOLS)

    def rpc(
        self, method: str, params: object, token: str = "good"
    ) -> Mapping[str, object]:
        """Make a JSON-RPC call.

        Args:
            method: The method.
            params: The parameters.
            token: The bearer token.

        Returns:
            The JSON-RPC response.
        """
        response = self.post(
            {"jsonrpc": "2.0", "id": 7, "method": method, "params": params}, token
        )
        return jsonvalue.loads_dict(jsonvalue.as_str(response["body"]).encode())

    def call(
        self, name: str, args: object, token: str = "good"
    ) -> Mapping[str, object]:
        """Call a tool.

        Args:
            name: The tool.
            args: The arguments.
            token: The bearer token.

        Returns:
            The ``tools/call`` result.
        """
        return jsonvalue.get_dict(
            self.rpc("tools/call", {"name": name, "arguments": args}, token), "result"
        )


def structured(result: Mapping[str, object]) -> Mapping[str, object]:
    """Return a successful tool result's structured content.

    Args:
        result: The ``tools/call`` result.

    Returns:
        The structured content.
    """
    assert_that(result["isError"], equal_to(False))
    return jsonvalue.get_dict(result, "structuredContent")


def error_text(result: Mapping[str, object]) -> str:
    """Return a failed tool result's message.

    Args:
        result: The ``tools/call`` result.

    Returns:
        The text.
    """
    assert_that(result["isError"], equal_to(True))
    return jsonvalue.get_str(
        jsonvalue.as_dict(jsonvalue.get_list(result, "content")[0]), "text"
    )

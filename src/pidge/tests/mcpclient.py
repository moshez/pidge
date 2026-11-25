"""An in-process MCP client for tests: builds Function URL events."""

import base64
import dataclasses
import json
from collections.abc import Mapping

from hamcrest import assert_that, equal_to

from .. import jsonvalue
from ..auth import BearerValidator
from ..sessions import SESSION_TOOLS
from ..toolkit import Services
from ..tools_read import READ_TOOLS
from ..tools_write import WRITE_TOOLS
from ..web import route
from .sessionkey import SESSION_KEY
from .fakes import World, new_cache

HOST = "abc.lambda-url.us-east-2.on.aws"
TOOLS = (*READ_TOOLS, *WRITE_TOOLS, *SESSION_TOOLS)


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
    """

    services: Services
    validator: BearerValidator

    @classmethod
    def create(cls, world: World, validator: BearerValidator) -> "Client":
        """Wire up a client.

        Args:
            world: The environment.
            validator: The token validator.

        Returns:
            The client.
        """
        cache = new_cache(world, "cache")
        services = Services(context=world.context, cache=cache, session_key=SESSION_KEY)
        return cls(services=services, validator=validator)

    def post(self, body: object, token: str = "good") -> Mapping[str, object]:
        """POST to /mcp.

        Args:
            body: The JSON-RPC body.
            token: The bearer token.

        Returns:
            The HTTP response.
        """
        event = request("POST", "/mcp", body, token)
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

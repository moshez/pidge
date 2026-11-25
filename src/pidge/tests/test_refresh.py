"""``refresh_tools``: the tool list, re-announced over SSE."""

import unittest
from collections.abc import Mapping, Sequence

from hamcrest import assert_that, contains_string, equal_to

from .. import jsonvalue
from ..mcp import INSTRUCTIONS
from ..refresh import summary
from ..toolkit import Build
from ..web import route
from .fakes import make_world
from .flaky import GOOD
from .mcpclient import TOOLS, built, json_body, with_headers

EVENTS = "application/json, text/event-stream"


def call(name: str) -> Mapping[str, object]:
    """Build a ``tools/call`` request with no arguments.

    Args:
        name: The tool.

    Returns:
        The JSON-RPC message.
    """
    params = {"name": name, "arguments": {}}
    return {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": params}


def events(body: str) -> Sequence[Mapping[str, object]]:
    """Parse an SSE body into its JSON-RPC messages.

    Args:
        body: The body.

    Returns:
        Each event's data, decoded, in order.
    """
    parsed = []
    for block in body.split("\n\n"):
        lines = block.splitlines()
        if not lines:
            continue
        assert_that(lines[0], equal_to("event: message"))
        data = "\n".join(line.removeprefix("data: ") for line in lines[1:])
        parsed.append(jsonvalue.loads_dict(data))
    return parsed


class TestRefreshTools(unittest.TestCase):
    """The tool, its SSE reply and its JSON fallback."""

    def setUp(self) -> None:
        """Wire a deployment of build ``abc123``."""
        self.services = built(make_world(self), Build(name="abc123", log=print))

    def post(self, body: Mapping[str, object], accept: str) -> Mapping[str, object]:
        """POST to /mcp with an Accept header (empty: none).

        Args:
            body: The JSON-RPC message.
            accept: The Accept header.

        Returns:
            The HTTP response.
        """
        extra = {"accept": accept} if accept else {}
        return route(with_headers(body, extra), self.services, GOOD, TOOLS)

    def check_result(self, response: Mapping[str, object]) -> None:
        """Check a ``refresh_tools`` JSON-RPC response.

        Args:
            response: The JSON-RPC response.
        """
        result = jsonvalue.get_dict(response, "result")
        content = jsonvalue.get_dict(result, "structuredContent")
        assert_that(content["build"], equal_to("abc123"))
        listed = jsonvalue.pluck(content, "tools", "name")
        assert_that(listed, equal_to([tool.name for tool in TOOLS]))
        assert_that(listed[-1], equal_to("refresh_tools"))
        assert_that(
            jsonvalue.pluck(content, "tools", "summary")[-1],
            equal_to(
                "Re-announce this server's tools; "
                "call it if a tool you expected is missing."
            ),
        )

    def test_event_stream(self) -> None:
        """The notification comes first, then the response; both parse."""
        response = self.post(call("refresh_tools"), EVENTS)
        assert_that(response["statusCode"], equal_to(200))
        assert_that(
            response["headers"],
            equal_to({"content-type": "text/event-stream", "x-accel-buffering": "no"}),
        )
        notification, reply = events(jsonvalue.as_str(response["body"]))
        assert_that(
            notification,
            equal_to({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}),
        )
        assert_that(reply["id"], equal_to(5))
        self.check_result(reply)

    def test_json_fallback(self) -> None:
        """Without text/event-stream in Accept, the result comes as JSON."""
        for accept in ["", "application/json"]:
            with self.subTest(accept):
                response = self.post(call("refresh_tools"), accept)
                headers = jsonvalue.as_dict(response["headers"])
                assert_that(headers["content-type"], equal_to("application/json"))
                self.check_result(json_body(response))

    def test_only_refresh_streams(self) -> None:
        """Other tools, and failed calls, answer in plain JSON."""
        for message in [call("list_tags"), call("nope")]:
            with self.subTest(message):
                headers = jsonvalue.as_dict(self.post(message, EVENTS)["headers"])
                assert_that(headers["content-type"], equal_to("application/json"))

    def test_capability_and_instructions(self) -> None:
        """initialize declares listChanged and points at refresh_tools."""
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        result = jsonvalue.get_dict(json_body(self.post(init, "")), "result")
        capabilities = result["capabilities"]
        assert_that(capabilities, equal_to({"tools": {"listChanged": True}}))
        line = "If a tool you expected is missing, call refresh_tools."
        assert_that(INSTRUCTIONS, contains_string(line))

    def test_summary(self) -> None:
        """Summaries are first sentences."""
        cases = [
            ("One. Two.", "One."),
            ("Ends here", "Ends here"),
            ("Why? Because.", "Why?"),
            ("A 5.5 point. Next.", "A 5.5 point."),
        ]
        for description, expected in cases:
            assert_that(summary(description), equal_to(expected))

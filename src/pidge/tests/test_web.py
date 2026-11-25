"""Tests for the HTTP front end, the MCP server and the tools."""

import json
import unittest
from collections.abc import Mapping

from hamcrest import (
    assert_that,
    contains_exactly,
    contains_string,
    equal_to,
    has_key,
    has_length,
    starts_with,
)

from .. import jsonvalue
from ..ingest import ingest
from ..web import route
from .checks import includes
from .fakes import make_world, sample_raw
from .flaky import GOOD
from .mcpclient import HOST, Client, error_text, request, structured


class TestProtocol(unittest.TestCase):
    """Routing, metadata, authentication and JSON-RPC."""

    def setUp(self) -> None:
        """Create a client."""
        self.client = Client.create(make_world(self), GOOD)

    def test_metadata(self) -> None:
        """The protected-resource metadata names the pool and scope."""
        event = request("GET", "/.well-known/oauth-protected-resource/mcp", "", "")
        response = route(event, self.client.services, self.client.validator, ())
        body = json.loads(jsonvalue.as_str(response["body"]))
        includes(
            body,
            dict(
                resource=f"https://{HOST}/mcp",
                scopes_supported=[f"https://{HOST}/mcp/full"],
            ),
        )

    def test_routes(self) -> None:
        """Unknown paths are 404; GET on /mcp is 405."""
        services, validator = self.client.services, self.client.validator
        assert_that(
            route(request("GET", "/", "", ""), services, validator, ())["statusCode"],
            equal_to(404),
        )
        assert_that(
            route(request("GET", "/mcp/", "", ""), services, validator, ())[
                "statusCode"
            ],
            equal_to(405),
        )

    def test_challenges(self) -> None:
        """Missing, bad and under-scoped tokens get WWW-Authenticate."""
        missing = self.client.post({}, token="")
        assert_that(missing["statusCode"], equal_to(401))
        metadata = f"https://{HOST}/.well-known/oauth-protected-resource/mcp"
        expected = {"www-authenticate": f'Bearer resource_metadata="{metadata}"'}
        includes(missing["headers"], expected)
        assert_that(self.client.post({}, token="bad")["statusCode"], equal_to(401))
        forbidden = self.client.post({}, token="narrow")
        assert_that(forbidden["statusCode"], equal_to(403))
        assert_that(str(forbidden["headers"]), contains_string("insufficient_scope"))

    def test_initialize(self) -> None:
        """Known versions are echoed; unknown ones get the newest."""
        for asked, answered in [("2025-03-26", "2025-03-26"), ("1999", "2025-11-25")]:
            reply = self.client.rpc("initialize", {"protocolVersion": asked})
            includes(reply["result"], dict(protocolVersion=answered))
        includes(self.client.rpc("ping", {}), dict(result={}))

    def test_list(self) -> None:
        """Every tool is listed with its schema."""
        tools = jsonvalue.get_list(
            jsonvalue.get_dict(self.client.rpc("tools/list", {}), "result"), "tools"
        )
        names = [jsonvalue.get_str(jsonvalue.as_dict(tool), "name") for tool in tools]
        assert_that(
            names,
            contains_exactly(
                "search",
                "get_message",
                "get_attachment",
                "list_tags",
                "send_email",
                "tag_email",
                "get_session_token",
                "refresh_tools",
            ),
        )

    def test_errors(self) -> None:
        """Protocol errors get JSON-RPC error codes."""
        cases: list[tuple[str, object, int]] = [
            ("nope", {}, -32601),
            ("tools/call", {"name": "nope"}, -32602),
            ("tools/call", {"name": "search", "arguments": "x"}, -32602),
            ("ping", "x", -32602),
        ]
        for method, params, code in cases:
            reply = self.client.rpc(method, params)
            includes(reply["error"], dict(code=code))
        garbage = self.client.post(b"not json")
        assert_that(
            json.loads(jsonvalue.as_str(garbage["body"]))["error"]["code"],
            equal_to(-32700),
        )

    def test_notification(self) -> None:
        """Notifications are accepted without a body."""
        response = self.client.post(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}
        )
        assert_that(response["statusCode"], equal_to(202))


class TestTools(unittest.TestCase):
    """The tools, end to end."""

    def setUp(self) -> None:
        """Create a client and receive one message."""
        self.world = make_world(self)
        self.client = Client.create(self.world, GOOD)
        self.world.store.put(
            "inbound/a", sample_raw("Lunch", "Tacos?", {"menu.pdf": b"%"})
        )
        self.original = ingest(self.world.context, "inbound/a")

    def call(self, name: str, args: Mapping[str, object]) -> Mapping[str, object]:
        """Call a tool.

        Args:
            name: The tool.
            args: The arguments.

        Returns:
            The ``tools/call`` result.
        """
        return self.client.call(name, args)

    def fails(self, name: str, args: Mapping[str, object]) -> str:
        """Call a tool that should fail.

        Args:
            name: The tool.
            args: The arguments.

        Returns:
            The error message.
        """
        return error_text(self.call(name, args))

    def test_search(self) -> None:
        """Search with every filter."""
        args = {
            "query": "tacos",
            "after": "2025-12-01",
            "before": "2026-02-01T00:00:00+00:00",
            "has_attachments": True,
            "limit": 5,
        }
        found = structured(self.call("search", args))
        assert_that(jsonvalue.get_list(found, "results"), has_length(1))
        includes(structured(self.call("list_tags", {})), dict(tags={"inbox": 1}))

    def test_read(self) -> None:
        """Read a message and download its attachment."""
        message = structured(self.call("get_message", {"id": self.original}))
        includes(message, dict(body="Tacos?\n", direction="in"))
        args = {"id": self.original, "name": "1-menu.pdf"}
        link = structured(self.call("get_attachment", args))
        assert_that(jsonvalue.get_str(link, "url"), starts_with("memory:"))

    def test_missing(self) -> None:
        """Missing messages and attachments are tool errors."""
        missing = self.world.context.new_id()
        cases: list[tuple[str, Mapping[str, object], str]] = [
            ("get_message", {"id": missing}, "no message"),
            ("get_attachment", {"id": self.original, "name": "0"}, "no attachment"),
            ("get_attachment", {"id": missing, "name": "x"}, "no attachment"),
            ("tag_email", {"id": missing, "add": ["x"]}, "no message"),
            (
                "send_email",
                {
                    "to": ["a@example.com"],
                    "subject": "x",
                    "attach_from": [{"id": self.original, "name": "9-nope.pdf"}],
                },
                "no attachment '9-nope.pdf'",
            ),
        ]
        for name, args, prefix in cases:
            assert_that(self.fails(name, args), starts_with(prefix))

    def test_bad_arguments(self) -> None:
        """Malformed arguments are tool errors."""
        cases: list[tuple[str, Mapping[str, object], str]] = [
            ("get_message", {"id": "nope"}, "id must be a message id"),
            ("get_message", {}, "id is required"),
            ("send_email", {"to": [], "subject": "x"}, "to must list"),
            ("send_email", {"to": ["a"], "subject": "x", "attach_from": 1}, "attach"),
            ("tag_email", {"id": self.original, "add": [" "]}, "invalid tag"),
        ]
        for name, args, prefix in cases:
            assert_that(self.fails(name, args), starts_with(prefix))

    def test_bad_search(self) -> None:
        """Search arguments are validated."""
        cases = {
            "query": (3, "query must be a string"),
            "tags": ("inbox", "tags must be a list of strings"),
            "has_attachments": ("yes", "has_attachments must be a boolean"),
            "limit": (True, "limit must be an integer from 1 to 100"),
            "after": ("Tuesday", "after must be an ISO 8601 date"),
        }
        for name, (value, message) in cases.items():
            assert_that(self.fails("search", {name: value}), equal_to(message))

    def test_send(self) -> None:
        """Send a reply with an attachment; it is queued, not sent inline."""
        args = {
            "to": ["alice@example.com"],
            "bcc": ["b@example.com"],
            "subject": "Lunch",
            "body": "Yes",
            "in_reply_to_id": self.original,
            "attach_from": [{"id": self.original, "name": "1-menu.pdf"}],
        }
        assert_that(structured(self.call("send_email", args)), has_key("outbox_id"))
        assert_that(self.world.sender.deliveries(), has_length(0))

    def test_tag(self) -> None:
        """Archive by removing inbox."""
        args = {"id": self.original, "add": ["food"], "remove": ["inbox"]}
        includes(structured(self.call("tag_email", args)), dict(tags=["food"]))

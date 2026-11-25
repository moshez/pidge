"""Tests for MCP session IDs that expire when another build is deployed."""

import unittest
from collections.abc import Mapping

from hamcrest import assert_that, equal_to, has_key, has_length, is_not, not_none

from .. import jsonvalue
from ..mcpsession import HEADER, SESSION_ID, Sessions, issue, random_part
from ..web import route
from .checks import includes
from .fakes import make_world
from .flaky import GOOD
from .mcpclient import TOOLS, Client, request
from .sessionkey import BUILD

LIST = {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}}
NOTE = {"jsonrpc": "2.0", "method": "notifications/initialized"}
EXPIRED = {
    "jsonrpc": "2.0",
    "id": 3,
    "error": {"code": -32001, "message": "session expired; re-initialize"},
}


def body(response: Mapping[str, object]) -> Mapping[str, object]:
    """Decode a JSON response body.

    Args:
        response: The HTTP response.

    Returns:
        The body.
    """
    return jsonvalue.loads_dict(jsonvalue.as_str(response["body"]))


def code_or_name(reply: Mapping[str, object]) -> object:
    """Pick the error code, or the server name, out of a JSON-RPC reply.

    Args:
        reply: The reply.

    Returns:
        ``error.code`` for an error, else ``result.serverInfo.name``.
    """
    if "error" in reply:
        return jsonvalue.get_dict(reply, "error")["code"]
    return jsonvalue.get_dict(jsonvalue.get_dict(reply, "result"), "serverInfo")["name"]


class TestIssue(unittest.TestCase):
    """Session IDs carry the build and a 128-bit random part."""

    def test_real_random(self) -> None:
        """IDs from :mod:`secrets` are well formed, visible ASCII, and distinct."""
        sessions = Sessions(build="abc123", random=random_part, log=print)
        first, second = issue(sessions, None), issue(sessions, None)
        for session_id in (first, second):
            assert_that(SESSION_ID.fullmatch(session_id), not_none())
            assert_that(session_id.startswith("abc123."), equal_to(True))
            assert_that(all(0x21 <= ord(c) <= 0x7E for c in session_id), equal_to(True))
        assert_that(first, is_not(equal_to(second)))


class TestSessions(unittest.TestCase):
    """The endpoint issues session IDs and refuses other builds' sessions."""

    def setUp(self) -> None:
        """Create a client, which initializes a session."""
        world = make_world(self)
        self.client = Client.create(world, GOOD)

    def expire(
        self, message: Mapping[str, object], session_id: str | None
    ) -> Mapping[str, object]:
        """Post a message that must be refused as expired.

        Args:
            message: The JSON-RPC message.
            session_id: The session header, or None.

        Returns:
            The 404's body.
        """
        response = self.client.post(message, session_id=session_id)
        assert_that(response["statusCode"], equal_to(404))
        return body(response)

    def log(self) -> Mapping[str, object]:
        """Return the last log line, decoded.

        Returns:
            The record.
        """
        return jsonvalue.loads_dict(self.client.logged[-1])

    def test_initialize(self) -> None:
        """initialize returns a session ID for this build and logs it."""
        assert_that(self.client.session_id, equal_to(f"{BUILD}.{'0' * 22}"))
        params = {"clientInfo": {"name": "claude-ai", "version": "1"}}
        message = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params}
        reply = self.client.post(message, session_id=None)
        assert_that(
            reply["headers"],
            equal_to(
                {"content-type": "application/json", HEADER: f"{BUILD}.{'0' * 21}1"}
            ),
        )
        assert_that(code_or_name(body(reply)), equal_to("pidge"))
        record = {"mcp": "initialize", "build": BUILD, "client": params["clientInfo"]}
        assert_that(self.log(), equal_to(record))

    def test_failed_initialize(self) -> None:
        """An initialize that fails gets no session ID."""
        bad = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": "x"}
        failed = self.client.post(bad, session_id=None)
        assert_that(failed["headers"], equal_to({"content-type": "application/json"}))
        assert_that(code_or_name(body(failed)), equal_to(-32602))

    def test_current(self) -> None:
        """The session's own ID is accepted for requests and notifications."""
        response = self.client.post(LIST)
        assert_that(response["statusCode"], equal_to(200))
        assert_that(body(response), has_key("result"))
        assert_that(self.client.post(NOTE)["statusCode"], equal_to(202))

    def test_stale(self) -> None:
        """Another build's ID gets a 404, logged with both builds."""
        assert_that(self.expire(LIST, f"old.{'A' * 22}"), equal_to(EXPIRED))
        expected = dict(
            reason="stale", build=BUILD, old_build="old", method="tools/list"
        )
        includes(self.log(), {"mcp": "expired", **expected})

    def test_malformed(self) -> None:
        """IDs that are not <build>.<random> get a 404."""
        for session_id in [
            "",
            "nodot",
            f"{BUILD}.short",
            f".{'A' * 22}",
            f"{BUILD}.{'é' * 22}",
            f"a b.{'A' * 22}",
            f"{BUILD}.{'A' * 22}.x",
        ]:
            with self.subTest(session_id):
                assert_that(self.expire(LIST, session_id), equal_to(EXPIRED))
                includes(self.log(), dict(reason="malformed", build=BUILD))
                assert_that("old_build" in self.log(), equal_to(False))

    def test_absent(self) -> None:
        """A request without a session ID gets a 404 too."""
        assert_that(self.expire(LIST, None), equal_to(EXPIRED))
        includes(self.log(), dict(mcp="expired", reason="absent", method="tools/list"))

    def test_notifications(self) -> None:
        """Notifications follow the same rules, with a null ID."""
        for session_id in [None, "garbage", f"old.{'A' * 22}"]:
            with self.subTest(session_id):
                expired = self.expire(NOTE, session_id)
                assert_that(expired, equal_to({**EXPIRED, "id": None}))

    def test_unauthenticated(self) -> None:
        """Authentication comes first, whatever the session header."""
        for token in ["", "bad"]:
            for session_id in [None, "", "garbage", self.client.session_id]:
                with self.subTest(token=token, session_id=session_id):
                    response = self.client.post(LIST, token, session_id)
                    assert_that(response["statusCode"], equal_to(401))
        assert_that(self.client.logged, has_length(1))

    def test_parse_error(self) -> None:
        """A body that is not JSON is a parse error, session or not."""
        response = self.client.post(b"not json", session_id=None)
        assert_that(response["statusCode"], equal_to(200))
        assert_that(code_or_name(body(response)), equal_to(-32700))

    def test_delete(self) -> None:
        """DELETE of a session is 405: there is no session state to end."""
        event = {
            **request("DELETE", "/mcp", "", "good"),
            "headers": {HEADER: self.client.session_id},
        }
        client = self.client
        response = route(event, client.services, client.validator, TOOLS)
        assert_that(response["statusCode"], equal_to(405))
        assert_that(body(response), has_key("error"))

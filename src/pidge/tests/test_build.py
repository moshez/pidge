"""The build identity, and no MCP sessions: Mcp-Session-Id is ignored."""

import unittest
from collections.abc import Mapping

from hamcrest import assert_that, equal_to, has_key

from .. import jsonvalue
from ..toolkit import Build
from ..web import route
from .fakes import make_world
from .flaky import GOOD
from .mcpclient import TOOLS, built, with_headers

BUILD = "0123abc"
LIST = {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}}
NOTE = {"jsonrpc": "2.0", "method": "notifications/initialized"}
SESSION_IDS = [None, "", "random-0123456789", f"stale.{'A' * 22}"]


class TestBuild(unittest.TestCase):
    """initialize names the build; session headers change nothing."""

    def setUp(self) -> None:
        """Wire a deployment of build ``BUILD``, recording log lines."""
        self.logged: list[str] = []
        build = Build(name=BUILD, log=self.logged.append)
        self.services = built(make_world(self), build)

    def post(
        self, body: Mapping[str, object], session_id: str | None, method: str = "POST"
    ) -> Mapping[str, object]:
        """Send a request with an optional Mcp-Session-Id header.

        Args:
            body: The JSON-RPC message.
            session_id: The header, or None for none.
            method: The HTTP method.

        Returns:
            The HTTP response.
        """
        extra = {} if session_id is None else {"mcp-session-id": session_id}
        return route(with_headers(body, extra, method), self.services, GOOD, TOOLS)

    def test_initialize(self) -> None:
        """initialize sets no session header, reports and logs the build."""
        params = {"clientInfo": {"name": "claude-ai", "version": "1"}}
        message = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params}
        response = self.post(message, None)
        assert_that(response["headers"], equal_to({"content-type": "application/json"}))
        result = jsonvalue.get_dict(
            jsonvalue.loads_dict(jsonvalue.as_str(response["body"])), "result"
        )
        assert_that(
            jsonvalue.get_dict(result, "serverInfo"),
            equal_to({"name": "pidge", "version": BUILD}),
        )
        record = jsonvalue.loads_dict(self.logged[-1])
        expected = {"mcp": "initialize", "build": BUILD, "client": params["clientInfo"]}
        assert_that(record, equal_to(expected))

    def test_session_ids_ignored(self) -> None:
        """Absent, random and stale session IDs all proceed as usual."""
        for session_id in SESSION_IDS:
            with self.subTest(session_id):
                listed = self.post(LIST, session_id)
                assert_that(listed["statusCode"], equal_to(200))
                body = jsonvalue.loads_dict(jsonvalue.as_str(listed["body"]))
                assert_that(body, has_key("result"))
                assert_that(self.post(NOTE, session_id)["statusCode"], equal_to(202))
        assert_that(self.logged, equal_to([]))

    def test_delete(self) -> None:
        """DELETE stays 405, whatever its session header."""
        for session_id in SESSION_IDS:
            with self.subTest(session_id):
                deleted = self.post({}, session_id, "DELETE")
                assert_that(deleted["statusCode"], equal_to(405))

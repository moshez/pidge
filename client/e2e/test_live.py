"""pidge_client against a deployed endpoint.

Set PIDGE_ENDPOINT and PIDGE_TOKEN from a ``get_session_token`` result,
then run ``nox -s e2e`` in ``client/``. Without them, the tests skip. Set
PIDGE_EXPECTED_BUILD to the deployed git SHA to check the session IDs' build.
"""

import datetime
import json
import os
import unittest
from collections.abc import Sequence

from pidge_client import SearchArgs, Session, calls, iter_search, views
from pidge_client.models import Hit
from pidge_client.transport import initialize
from pidge_client.workflows import body_part, fetch_body_html

ENDPOINT = os.environ.get("PIDGE_ENDPOINT", "")
TOKEN = os.environ.get("PIDGE_TOKEN", "")
EXPECTED_BUILD = os.environ.get("PIDGE_EXPECTED_BUILD", "")
SCAN = 25


@unittest.skipUnless(ENDPOINT and TOKEN, "PIDGE_ENDPOINT and PIDGE_TOKEN not set")
class TestLive(unittest.TestCase):
    """Read-only calls against the deployed mailbox."""

    def setUp(self) -> None:
        """Connect."""
        self.session = Session(endpoint=ENDPOINT, token=TOKEN)

    def test_session_id(self) -> None:
        """initialize issues <build>.<random>; a request without it gets 404."""
        initialize(self.session)
        session_id = self.session.handshake.session_id or ""
        build, _, random = session_id.partition(".")
        self.assertEqual(len(random), 22)
        if EXPECTED_BUILD:
            self.assertEqual(build, EXPECTED_BUILD)
        message = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        headers = {
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        body = json.dumps(message).encode()
        reply = self.session.transport.post(ENDPOINT, headers, body)
        self.assertEqual(reply.status, 404)
        self.assertEqual(json.loads(reply.body)["error"]["code"], -32001)
        with_id = {**headers, "Mcp-Session-Id": session_id}
        self.assertEqual(
            self.session.transport.post(ENDPOINT, with_id, body).status, 200
        )

    def test_list_tags(self) -> None:
        """Tag counts are positive integers."""
        counts = calls.list_tags(self.session)
        self.assertTrue(all(count > 0 for count in counts.values()))

    def test_search_pages(self) -> None:
        """Paging one hit at a time matches one big page."""
        whole = calls.search(self.session, SearchArgs(limit=SCAN, order="date"))
        paged = [
            hit.id
            for _, hit in zip(
                range(SCAN), iter_search(self.session, SearchArgs(limit=1))
            )
        ]
        self.assertEqual(paged, [hit.id for hit in whole.hits])

    def test_fetch_body_html(self) -> None:
        """Recent messages download their HTML body exactly when they have one."""
        now = datetime.datetime.now(datetime.UTC)
        for hit in calls.search(self.session, SearchArgs(limit=SCAN)).hits:
            message = calls.get_message(self.session, hit.id)
            body = fetch_body_html(self.session, message)
            self.assertEqual(body is None, body_part(message) is None)
            views.render_message(message, body, datetime.UTC)
        self.assertIn("message", views.render_inbox(whole_page(self.session), now))


def whole_page(session: Session) -> Sequence[Hit]:
    """Read the newest page of mail.

    Args:
        session: The session.

    Returns:
        The hits.
    """
    return calls.search(session, SearchArgs(limit=SCAN)).hits

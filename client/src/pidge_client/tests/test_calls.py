"""Every typed call, through pidge's own ``handle_rpc``."""

import datetime
import unittest
from collections.abc import Sequence

from hamcrest import (
    assert_that,
    contains_exactly,
    contains_string,
    equal_to,
    has_length,
    none,
)

from pidge.indexer import run_tracked

from .. import calls
from ..calls import AttachFrom, Draft, SearchArgs
from ..models import Attachment
from .mailbox import ENDPOINT, fixture_mailbox

FIRST_DATE = 1_767_225_600


class TestCalls(unittest.TestCase):
    """The wrappers against a mailbox holding the .eml fixtures."""

    def setUp(self) -> None:
        """Fill a mailbox with the fixtures."""
        self.mailbox, self.ids = fixture_mailbox(self)
        self.session = self.mailbox.session

    def subjects(self, query: SearchArgs) -> Sequence[str]:
        """SearchArgs and list the subjects found.

        Args:
            query: The search.

        Returns:
            The subjects, in result order.
        """
        return [hit.subject for hit in calls.search(self.session, query).hits]

    def expect(self, args: SearchArgs, *subjects: str) -> None:
        """Search and check the subjects found, in order.

        Args:
            args: The search.
            *subjects: The expected subjects.
        """
        assert_that(self.subjects(args), equal_to(list(subjects)))

    def test_search_date_order(self) -> None:
        """With no words, hits are newest first and the last page has no cursor."""
        page = calls.search(self.session, SearchArgs())
        assert_that(page.hits[0].subject, equal_to("How did we do?"))
        assert_that(page.next_cursor, none())
        assert_that(page.hits[0].ranked, equal_to(False))

    def test_search_words_and_sender(self) -> None:
        """Query words and the sender filter reach the server."""
        for args, subject in [
            (SearchArgs(query="newsletter"), "Newsletter"),
            (SearchArgs(sender="widgets.example"), "How did we do?"),
        ]:
            self.expect(args, subject)

    def test_search_attachments_and_dates(self) -> None:
        """Attachment, recipient and date filters reach the server."""
        self.expect(SearchArgs(has_attachments=True), "Top-level related", "Newsletter")
        self.expect(
            SearchArgs(after="2026-06-01", to="me@example.org"), "How did we do?"
        )
        self.expect(SearchArgs(before="2025-01-01"))

    def test_search_relevance_ranked(self) -> None:
        """Indexed hits for a query are ranked by relevance."""
        run_tracked(self.mailbox.world.context)
        page = calls.search(
            self.session, SearchArgs(query="newsletter", order="relevance")
        )
        assert_that(page.hits[0].ranked, equal_to(True))

    def test_search_cursor(self) -> None:
        """A date-order page with more results carries a cursor that continues."""
        first = calls.search(self.session, SearchArgs(limit=2))
        assert first.next_cursor is not None
        second = calls.search(
            self.session, SearchArgs(limit=2, cursor=first.next_cursor)
        )
        ids = {hit.id for hit in [*first.hits, *second.hits]}
        assert_that(len(ids), equal_to(4))

    def test_get_message(self) -> None:
        """get_message parses headers, parts, body and tags."""
        message = calls.get_message(self.session, self.ids["related"])
        headers = message.headers
        assert_that(headers.subject, equal_to("Newsletter"))
        assert_that(headers.sender, equal_to("Alice <alice@example.com>"))
        assert_that(headers.to, contains_exactly("me@example.org"))
        assert_that(headers.date, equal_to(FIRST_DATE))
        assert_that(headers.message_id, equal_to("<related@example.com>"))
        assert_that(message.tags, contains_exactly("inbox"))
        assert_that(message.direction, equal_to("in"))
        assert_that(message.body, contains_string("Newsletter in plain text."))
        report = Attachment(
            name="1-report.pdf",
            content_type="application/pdf",
            size=5,
            kind="attachment",
        )
        assert_that(headers.attachments[1], equal_to(report))

    def test_get_attachment(self) -> None:
        """get_attachment returns the presigned URL."""
        url = calls.get_attachment(self.session, self.ids["html_only"], "0-body.html")
        assert_that(url, contains_string("0-body.html"))

    def test_tags(self) -> None:
        """tag_email returns the new tags, and list_tags counts them."""
        tags = calls.tag_email(
            self.session, self.ids["plain_only"], add=["work"], remove=["inbox"]
        )
        assert_that(tags, contains_exactly("work"))
        assert_that(calls.list_tags(self.session), equal_to({"inbox": 5, "work": 1}))

    def test_send_email(self) -> None:
        """send_email passes every argument and returns the outbox ID."""
        draft = Draft(
            to=["x@example.com"],
            subject="Re: Newsletter",
            body="Thanks",
            cc=["y@example.com"],
            blind=("z@example.com",),
            in_reply_to_id=self.ids["related"],
            sender="Me <me@example.org>",
            attach_from=[AttachFrom(id=self.ids["related"], name="1-report.pdf")],
        )
        queued = calls.send_email(self.session, draft)
        assert_that(queued.status, equal_to("pending"))
        self.expect(SearchArgs(tags=["outbox"]), "Re: Newsletter")
        assert_that(queued.outbox_id, has_length(26))

    def test_get_session_token(self) -> None:
        """get_session_token parses the token, its expiry and endpoint."""
        token = calls.get_session_token(self.session)
        assert_that(token.endpoint, equal_to(ENDPOINT))
        issued = datetime.datetime.fromtimestamp(FIRST_DATE + 6, datetime.UTC)
        assert_that(token.expires_at - issued, equal_to(datetime.timedelta(minutes=5)))

    def test_unknown_parameter(self) -> None:
        """A signature refuses names it does not declare."""
        with self.assertRaises(KeyError):
            calls.call(self.session, calls.SEARCH, {"q": "x"})

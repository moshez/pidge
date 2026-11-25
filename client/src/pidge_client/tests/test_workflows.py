"""Tests for paging, body download and archiving."""

import dataclasses
import unittest
from collections.abc import Mapping

from hamcrest import assert_that, contains_string, equal_to, none

from pidge.indexer import run_tracked
from pidge.tagging import tag_email
from pidge.tests.fakes import sample_raw

from .. import calls
from ..calls import SearchArgs
from ..transport import HTTPStatusError, Reply
from ..workflows import archive, fetch_body_html, iter_search
from ..transport import Transport
from .mailbox import BASE, dated, empty_mailbox, fixture_mailbox, receive

YEAR = 366 * 86400


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class ExpiredLinks:
    """Serves tool calls, but every download is refused.

    Attributes:
        inner: The transport for tool calls.
    """

    inner: Transport

    def post(self, url: str, headers: Mapping[str, str], body: bytes) -> Reply:
        """Pass a tool call through.

        Args:
            url: The URL.
            headers: The headers.
            body: The body.

        Returns:
            The inner transport's reply.
        """
        return self.inner.post(url, headers, body)

    def get(self, url: str) -> Reply:
        """Refuse a download.

        Args:
            url: Ignored.

        Returns:
            A 403.
        """
        return Reply(status=403, body=b"expired")


class TestIterSearch(unittest.TestCase):
    """Following next_cursor through a mailbox split across shards and tail."""

    def test_union_of_pages(self) -> None:
        """Pages, joined, equal one unpaged search: same order, no repeats."""
        mailbox = empty_mailbox(self)
        dates = [BASE + n for n in range(4)] + [BASE + YEAR + 9] * 4
        ids = [
            receive(mailbox, f"m{n}", dated(f"m{n}", when))
            for n, when in enumerate(dates)
        ]
        run_tracked(mailbox.world.context)
        receive(mailbox, "late", dated("late", BASE + YEAR + 9))
        tag_email(mailbox.world.store, ids[5], ["seen"], [])
        unpaged = calls.search(mailbox.session, SearchArgs(limit=100))
        paged = [hit.id for hit in iter_search(mailbox.session, SearchArgs(limit=3))]
        assert_that(paged, equal_to([hit.id for hit in unpaged.hits]))
        assert_that(len(set(paged)), equal_to(9))

    def test_filters_pass_through(self) -> None:
        """Filters apply to every page."""
        mailbox, ids = fixture_mailbox(self)
        query = SearchArgs(
            query="alice",
            sender="alice",
            to="me",
            after="2025-01-01",
            before="2026-06-01",
            tags=["inbox"],
            has_attachments=False,
            limit=1,
            order="relevance",
        )
        found = iter_search(mailbox.session, query)
        subjects = [hit.subject for hit in found]
        assert_that(subjects, equal_to(["Plain only", "HTML only", "Alternative"]))


class TestFetchBody(unittest.TestCase):
    """Downloading 0-body.html through its presigned URL."""

    def setUp(self) -> None:
        """Fill a mailbox with the fixtures."""
        self.mailbox, self.ids = fixture_mailbox(self)

    def body_of(self, name: str) -> str | None:
        """Download a fixture's HTML body.

        Args:
            name: The fixture name.

        Returns:
            The HTML, or None.
        """
        session = self.mailbox.session
        return fetch_body_html(session, calls.get_message(session, self.ids[name]))

    def test_html_body(self) -> None:
        """A message with an HTML body gets it back."""
        assert_that(str(self.body_of("alternative")), contains_string("<b>HTML</b>"))

    def test_plain_message(self) -> None:
        """A plain-text message has no HTML body."""
        assert_that(self.body_of("plain_only"), none())

    def test_attachments_but_no_html(self) -> None:
        """A plain message with attachments still has no HTML body."""
        raw = sample_raw("With a PDF", "Text", {"a.pdf": b"%PDF-"})
        ulid = receive(self.mailbox, "pdf", raw)
        session = self.mailbox.session
        assert_that(fetch_body_html(session, calls.get_message(session, ulid)), none())

    def test_download_failure(self) -> None:
        """A failed download raises HTTPStatusError."""
        expired = ExpiredLinks(inner=self.mailbox.transport)
        session = dataclasses.replace(self.mailbox.session, transport=expired)
        message = calls.get_message(session, self.ids["alternative"])
        with self.assertRaises(HTTPStatusError) as caught:
            fetch_body_html(session, message)
        assert_that(str(caught.exception), equal_to("(403, 'expired')"))


class TestArchive(unittest.TestCase):
    """Archiving removes the inbox tag."""

    def test_archive(self) -> None:
        """Each message loses inbox and keeps its other tags."""
        mailbox, ids = fixture_mailbox(self)
        calls.tag_email(mailbox.session, ids["related"], add=["work"])
        result = archive(mailbox.session, [ids["related"], ids["plain_only"]])
        assert_that(
            result, equal_to({ids["related"]: ("work",), ids["plain_only"]: ()})
        )
        assert_that(calls.list_tags(mailbox.session), equal_to({"inbox": 4, "work": 1}))

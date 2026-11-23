"""Tests for storing the HTML body and its inline parts."""

import pathlib
import unittest

from collections.abc import Mapping

from hamcrest import assert_that, contains_exactly, contains_string, equal_to, has_key

from .. import jsonvalue
from ..documents import IndexDoc, attachment_names
from ..mimeparts import content_id_key, inline_name, rewrite_cids
from ..ingest import ingest
from ..mimeparse import ParsedMessage, parse_message
from ..mimeparts import BODY_HTML, PartKind
from ..model import Meta, parse_meta
from ..records import read_json, read_meta
from .fakes import body, drop, keys, make_world

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
STORED = ("1-report.pdf", "2-logo.png", "3-inline-1.jpg")


def fixture(name: str) -> bytes:
    """Read a fixture message.

    Args:
        name: The file name, without ``.eml``.

    Returns:
        The raw message.
    """
    return (FIXTURES / f"{name}.eml").read_bytes()


def parsed(name: str) -> ParsedMessage:
    """Parse a fixture message.

    Args:
        name: The file name, without ``.eml``.

    Returns:
        The parsed message.
    """
    return parse_message(fixture(name))


def body_of(name: str, text: str) -> ParsedMessage:
    """Parse a fixture message and check its body text.

    Args:
        name: The file name, without ``.eml``.
        text: The expected ``body.txt``.

    Returns:
        The parsed message.
    """
    message = parsed(name)
    assert_that(message.body, equal_to(text))
    return message


def assert_parts(message: ParsedMessage, *expected: str) -> None:
    """Check the stored part names, in order.

    Args:
        message: The parsed message.
        *expected: The names.
    """
    stored = [part.name for part in message.attachments]
    assert_that(stored, equal_to(list(expected)))


def page(message: ParsedMessage) -> str:
    """Read the stored HTML body.

    Args:
        message: The parsed message.

    Returns:
        The decoded ``0-body.html``.
    """
    return message.attachments[0].data.decode("utf-8")


class TestHtmlBody(unittest.TestCase):
    """The HTML body is stored as 0-body.html; body.txt is unchanged."""

    def test_alternative(self) -> None:
        """text + html: body.txt from the text part, and 0-body.html."""
        message = body_of("alternative", "The plain version.")
        assert_parts(message, BODY_HTML)
        assert_that(message.attachments[0].content_type, equal_to("text/html"))
        assert_that(page(message), contains_string("<b>HTML</b> version"))

    def test_html_only(self) -> None:
        """HTML-only: body.txt is HTML→text; 0-body.html is UTF-8."""
        message = body_of("html_only", "Café menu")
        assert_parts(message, BODY_HTML)
        html = page(message)
        assert_that(html, contains_string("<p>Café menu</p>"))
        assert_that(html, contains_string("charset=utf-8"))
        assert_that("iso-8859-1" in html, equal_to(False))

    def test_plain_only(self) -> None:
        """Plain text only: no 0-body.html."""
        message = body_of("plain_only", "Just text.\n")
        assert_parts(message)
        assert_that(message.unresolved_cids, equal_to(()))


class TestInlineParts(unittest.TestCase):
    """Inline parts are stored and the HTML points at them."""

    def setUp(self) -> None:
        """Parse a newsletter with two inline images and a PDF."""
        self.message = parsed("related")

    def test_numbering(self) -> None:
        """The real attachment is 1, then inline parts; unnamed ones get names."""
        assert_parts(self.message, BODY_HTML, *STORED)
        kinds = [part.kind for part in self.message.attachments]
        expected = [PartKind.BODY, PartKind.ATTACHMENT] + [PartKind.INLINE] * 2
        assert_that(kinds, equal_to(expected))
        assert_that(self.message.attachments[2].data[:4], equal_to(b"\x89PNG"))
        assert_that(self.message.body, equal_to("Newsletter in plain text."))

    def test_references_rewritten(self) -> None:
        """src, href, background, and url() in style blocks and attributes."""
        html = page(self.message)
        for rewritten in [
            '<img src="2-logo.png"',
            '<a href="2-logo.png"',
            '<td background="3-inline-1.jpg"',
            ".banner { background: url(2-logo.png); }",
            "background-image: url('3-inline-1.jpg')",
        ]:
            assert_that(html, contains_string(rewritten))

    def test_unresolved(self) -> None:
        """A reference to no part is left alone and reported once."""
        html = page(self.message)
        assert_that(html, contains_string('<img src="cid:missing@x">'))
        assert_that(html, contains_string("<img src='cid:missing@x'>"))
        assert_that(self.message.unresolved_cids, contains_exactly("missing@x"))

    def test_top_level_related(self) -> None:
        """Inline parts that are already attachments keep their place and kind."""
        message = parsed("top_related")
        assert_parts(message, BODY_HTML, "1-inline-1.gif")
        assert_that(message.attachments[1].kind, equal_to(PartKind.ATTACHMENT))
        assert_that(page(message), contains_string('<img src="1-inline-1.gif">'))

    def test_matching(self) -> None:
        """Brackets, case and URL-encoding do not matter; blanks match nothing."""
        assert_that(content_id_key(" <Logo@X> "), equal_to("logo@x"))
        stored = {"logo@x": "2-logo.png"}
        markup, unresolved = rewrite_cids('<img SRC="cid:LOGO%40x">', stored)
        assert_that(markup, equal_to('<img SRC="2-logo.png">'))
        assert_that(unresolved, equal_to([]))
        markup, unresolved = rewrite_cids("<img src=cid:%20>", stored)
        assert_that(unresolved, contains_exactly(" "))

    def test_unknown_type(self) -> None:
        """A MIME type with no known extension is stored as .bin."""
        assert_that(inline_name("application/x-nothing", 2), equal_to("inline-2.bin"))


class TestStored(unittest.TestCase):
    """What ingest writes, and that it is the same every time."""

    def setUp(self) -> None:
        """Ingest the newsletter."""
        self.world = make_world(self)
        self.key = drop(self.world, "ses1", fixture("related"))
        self.ulid = self.ingest()

    def ingest(self) -> str:
        """Ingest the newsletter's S3 event.

        Returns:
            The message ID.
        """
        return ingest(self.world.context, self.key)

    def meta(self) -> Meta:
        """Read meta.json.

        Returns:
            The record.
        """
        return read_meta(self.world.store, self.ulid)[0]

    def snapshot(self) -> Mapping[str, bytes]:
        """Read the message's records.

        Returns:
            The bytes under ``msgs/{ulid}/``, by key.
        """
        prefix = f"msgs/{self.ulid}/"
        found = [key for key in keys(self.world) if key.startswith(prefix)]
        return {key: body(self.world, key) for key in found}

    def headers(self) -> Mapping[str, object]:
        """Read headers.json.

        Returns:
            The record.
        """
        return read_json(self.world.store, f"msgs/{self.ulid}/headers.json")

    def test_records(self) -> None:
        """Every part is a file and listed in headers.json, with its kind."""
        stored = self.snapshot()
        for name in [BODY_HTML, *STORED]:
            assert_that(stored, has_key(f"msgs/{self.ulid}/attachments/{name}"))
        listing = jsonvalue.objects(self.headers(), "attachments")
        found = [(entry["name"], entry["kind"]) for entry in listing]
        expected = [(BODY_HTML, "body"), (STORED[0], "attachment")]
        expected += [(name, "inline") for name in STORED[1:]]
        assert_that(found, equal_to(expected))

    def test_unresolved_in_meta(self) -> None:
        """meta.json lists the unresolved reference, and keeps it."""
        meta = self.meta()
        assert_that(meta.unresolved_cids, contains_exactly("missing@x"))
        stored = body(self.world, f"msgs/{self.ulid}/meta.json")
        assert_that(parse_meta(stored), equal_to(meta))

    def test_reingest(self) -> None:
        """A repeated S3 event writes the same keys with the same bytes."""
        before = self.snapshot()
        assert_that(self.ingest(), equal_to(self.ulid))
        assert_that(self.snapshot(), equal_to(before))

    def test_search_unchanged(self) -> None:
        """Only the real attachment counts for search."""
        meta = self.meta()
        doc = IndexDoc(ulid=self.ulid, headers=self.headers(), body="", meta=meta)
        assert_that(attachment_names(doc), contains_exactly(STORED[0]))

"""Tests for MIME parsing."""

import unittest
from email.message import EmailMessage

from hamcrest import assert_that, contains_exactly, contains_string, equal_to

from ..mimeparse import html_to_text, parse_date, parse_message, sanitize_name
from .fakes import sample_raw


class TestParse(unittest.TestCase):
    """Headers, body and attachments come out of raw mail."""

    def test_plain_with_attachment(self) -> None:
        """A plain message with an attachment parses fully."""
        parsed = parse_message(sample_raw("Hi", "Hello there", {"r e/p.pdf": b"%PDF"}))
        headers = parsed.headers
        assert_that(headers.sender, equal_to("Alice Example <alice@example.com>"))
        assert_that(headers.to, contains_exactly("Me <me@example.org>"))
        assert_that(headers.cc, contains_exactly("bob@example.com"))
        assert_that(headers.date, equal_to(1_767_225_600))
        assert_that(headers.message_id, equal_to("<original@example.com>"))
        assert_that(parsed.body, contains_string("Hello there"))
        assert_that(parsed.attachments[0].name, equal_to("0-r_e_p.pdf"))
        assert_that(parsed.attachments[0].data, equal_to(b"%PDF"))

    def test_html_only(self) -> None:
        """An HTML-only body is converted to text."""
        message = EmailMessage()
        message["Subject"] = "x"
        message.set_content(
            "<html><head><style>p{}</style></head><body><p>One &amp; two</p>"
            "<div>three</div><script>bad()</script></body></html>",
            subtype="html",
        )
        parsed = parse_message(message.as_bytes())
        assert_that(parsed.body, equal_to("One & two\nthree"))
        assert_that(parsed.headers.date, equal_to(0))

    def test_no_body(self) -> None:
        """A message with only an attachment has an empty body."""
        message = EmailMessage()
        message.add_attachment(b"x", maintype="image", subtype="png")
        parsed = parse_message(message.as_bytes())
        assert_that(parsed.body, equal_to(""))
        assert_that(parsed.attachments[0].name, equal_to("0-part0"))

    def test_unknown_charset(self) -> None:
        """A body in a charset Python lacks is decoded rather than rejected."""
        raw = b"Content-Type: text/plain; charset=x-unknown\r\n\r\nhello \xe9"
        assert_that(parse_message(raw).body, equal_to("hello \ufffd"))

    def test_edge_values(self) -> None:
        """Small helpers behave at the edges."""
        assert_that(sanitize_name("..."), equal_to("attachment"))
        assert_that(parse_date("garbage"), equal_to(0))
        assert_that(html_to_text("<p>a</p><p>b</p>"), equal_to("a\nb"))

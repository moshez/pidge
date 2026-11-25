"""Golden-file and unit tests for the HTML views."""

import datetime
import io
import json
import os
import pathlib
import tempfile
import unittest
import zoneinfo
from collections.abc import Sequence

from hamcrest import (
    assert_that,
    contains_string,
    equal_to,
    starts_with,
)

from .. import calls, views
from ..dates import when
from ..mail import as_mail
from ..text import initials, snippet
from ..calls import Draft
from ..models import Hit, Message, parse_hit
from ..reader import collapse_tiny_links, drop_pixels, readable_html
from ..workflows import fetch_body_html
from .mailbox import HERE, Mailbox, fixture_mailbox, fixture_paths

GOLDEN = HERE / "golden"
TZ = zoneinfo.ZoneInfo("America/Los_Angeles")
NOW = datetime.datetime(2026, 11, 24, 18, 0, tzinfo=TZ)
UPDATE = os.environ.get("PIDGE_UPDATE_GOLDEN") == "1"


def check_golden(case: unittest.TestCase, name: str, rendered: str) -> None:
    """Compare rendered HTML with its golden file.

    Set PIDGE_UPDATE_GOLDEN=1 to rewrite the golden files instead.

    Args:
        case: The test case.
        name: The golden file name.
        rendered: The HTML.
    """
    path = GOLDEN / name
    if UPDATE:  # pragma: no cover
        path.write_text(rendered + "\n", encoding="utf-8")
    case.assertEqual(rendered + "\n", path.read_text(encoding="utf-8"))


def sample_hit(**fields: object) -> Hit:
    """Build a search hit from JSON fields.

    Args:
        **fields: The fields that differ from a default hit.

    Returns:
        The hit.
    """
    base: dict[str, object] = {
        "id": "01J0000000000000000000000A",
        "date": int(NOW.timestamp()),
        "from": "Bob <bob@example.com>",
        "subject": "Hello",
        "snippet": "Hi",
        "tags": ["inbox"],
        "ranked": False,
    }
    return parse_hit({**base, **fields})


class TestGolden(unittest.TestCase):
    """Every .eml fixture rendered, compared with saved output."""

    def setUp(self) -> None:
        """Fill a mailbox with the fixtures, plus one sent message."""
        mailbox, ids = fixture_mailbox(self)
        self.mailbox: Mailbox = mailbox
        self.ids = dict(ids)
        draft = Draft(
            to=["Carol <carol@example.com>", "dan@example.com"],
            subject="Notes",
            body="See https://example.com/x for details.\r\n\r\nLine one\nLine two",
        )
        queued = calls.send_email(mailbox.session, draft)
        self.ids["sent"] = queued.outbox_id

    def messages(self) -> Sequence[Message]:
        """Read every message.

        Returns:
            The messages.
        """
        return [
            calls.get_message(self.mailbox.session, ulid) for ulid in self.ids.values()
        ]

    def test_inbox(self) -> None:
        """The inbox of every fixture message."""
        check_golden(self, "inbox.html", views.render_inbox(self.messages(), NOW, TZ))

    def test_inbox_of_hits(self) -> None:
        """The inbox built from search hits."""
        hits = calls.search(self.mailbox.session).hits
        check_golden(self, "inbox-hits.html", views.render_inbox(hits, NOW, TZ))

    def test_messages(self) -> None:
        """Each message as a fragment and as a page."""
        for name, ulid in self.ids.items():
            message = calls.get_message(self.mailbox.session, ulid)
            body = fetch_body_html(self.mailbox.session, message)
            with self.subTest(name=name):
                fragment = views.render_message(message, body, TZ)
                check_golden(self, f"{name}.html", fragment)
                page = views.render_message(message, body, TZ, page=True)
                check_golden(
                    self,
                    f"{name}.page.html",
                    views.as_page(message.headers.subject, page),
                )

    def test_fixture_names(self) -> None:
        """Every fixture has a golden file."""
        for path in fixture_paths():
            assert_that((GOLDEN / f"{path.stem}.html").exists(), equal_to(True))


class TestDates(unittest.TestCase):
    """Short dates in list rows."""

    def short(self, moment: datetime.datetime) -> str:
        """Format a time relative to NOW.

        Args:
            moment: The time.

        Returns:
            The short date.
        """
        return when(int(moment.timestamp()), NOW, TZ)

    def test_short_dates(self) -> None:
        """Time today, weekday this week, month and day this year, else the year."""
        cases = {
            "12:05 AM": NOW.replace(hour=0, minute=5),
            "1:00 PM": NOW.replace(hour=13),
            "Sun": NOW - datetime.timedelta(days=2),
            "Feb 3": NOW.replace(month=2, day=3),
            "Nov 24, 2024": NOW.replace(year=2024),
            "Nov 25": NOW + datetime.timedelta(days=1),
        }
        found = {self.short(moment): moment for moment in cases.values()}
        assert_that(found, equal_to(cases))

    def test_tz_defaults_to_nows(self) -> None:
        """Without tz, the inbox uses now's time zone, or UTC for naive now."""
        item = sample_hit()
        assert_that(views.render_inbox([item], NOW), contains_string(">6:00 PM<"))
        naive = NOW.astimezone(datetime.UTC).replace(tzinfo=None)
        assert_that(views.render_inbox([item], naive), contains_string(">2:00 AM<"))


class TestRendering(unittest.TestCase):
    """Smaller rendering rules."""

    def test_one_message_summary(self) -> None:
        """A single message is "1 message"."""
        assert_that(
            views.render_inbox([sample_hit()], NOW, TZ), contains_string(">1 message<")
        )

    def test_no_subject(self) -> None:
        """An empty subject shows as (no subject)."""
        assert_that(
            views.render_inbox([sample_hit(subject="")], NOW, TZ),
            contains_string("(no subject)"),
        )

    def test_long_snippet(self) -> None:
        """Long snippets are cut at 140 characters with an ellipsis."""
        text = snippet("word " * 100)
        assert_that(len(text), equal_to(140))
        assert_that(text[-1], equal_to("…"))

    def test_initials(self) -> None:
        """Avatars use up to two initials, or ? when there are none."""
        assert_that(initials("ada lovelace byron"), equal_to("AL"))
        assert_that(initials("!!"), equal_to("?"))

    def test_cannot_render(self) -> None:
        """Only messages and hits render."""
        with self.assertRaises(TypeError):
            as_mail("nope")

    def test_page_without_html(self) -> None:
        """A page of a plain-text message shows the text, not a frame."""
        rendered = views.render_message(
            sample_hit(snippet="plain"), None, TZ, page=True
        )
        assert_that("<iframe" in rendered, equal_to(False))


class TestReader(unittest.TestCase):
    """The readable reduction of email HTML."""

    def test_pixels_dropped(self) -> None:
        """Images 2px or less on a side are tracking pixels."""
        source = (
            '<img width="1" height="1" src="a"><img style="width: 2px" src="b">'
            '<IMG height=0 src=c><img width="100%" src="d"><img src="e">'
        )
        assert_that(
            drop_pixels(source), equal_to('<img width="100%" src="d"><img src="e">')
        )

    def test_pixel_alt_not_shown(self) -> None:
        """A tracking pixel's alt text is not shown either."""
        rendered = readable_html('<p>Hi <img width="1" height="1" alt="pixel"></p>')
        assert_that("pixel" in rendered, equal_to(False))

    def test_scale_collapsed(self) -> None:
        """A run of tiny links becomes one line; a lone one stays put."""
        digits = [(False, f'<a href="u">{n}</a>') for n in range(3)]
        rest = [(True, '<a href="u">A</a>'), (False, '<a href="u">ok</a>')]
        merged = collapse_tiny_links([(False, "How likely?"), *digits, *rest])
        line = " ".join(text for _, text in digits)
        assert_that(merged, equal_to([(False, "How likely?"), (False, line), *rest]))

    def test_nested_hidden_and_skipped(self) -> None:
        """Content inside hidden or skipped elements is dropped, and nothing after."""
        source = (
            "<div style='display:none'><span>gone</span><div>gone</div></div>"
            "<style><b>gone</b></style></style>"
            "<div><span>kept</span><a href='/relative'>link</a></div>"
        )
        assert_that(
            readable_html(source), equal_to('<p style="margin:0 0 10px;">keptlink</p>')
        )


class TestCommandLine(unittest.TestCase):
    """``python -m pidge_client.views``."""

    def setUp(self) -> None:
        """Make a scratch directory."""
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.directory = pathlib.Path(scratch.name)

    def save(self, name: str, data: object) -> str:
        """Save JSON to a scratch file.

        Args:
            name: The file name.
            data: The JSON value.

        Returns:
            The path.
        """
        path = self.directory / name
        path.write_text(json.dumps(data), encoding="utf-8")
        return str(path)

    def run_cli(self, *argv: str) -> str:
        """Run the command line.

        Args:
            *argv: The arguments.

        Returns:
            What it wrote.
        """
        out = io.StringIO()
        status = views.run(argv, out, lambda: NOW.timestamp())
        assert_that(status, equal_to(0))
        return out.getvalue()

    def test_inbox_shapes(self) -> None:
        """A search response, a list and a single object all load."""
        one = {"id": "A", "date": 0, "from": "x@y", "subject": "One", "tags": []}
        two = dict(one, id="B", subject="Two")
        three = dict(one, id="C", subject="Three")
        rendered = self.run_cli(
            "--tz",
            "UTC",
            "inbox",
            self.save("search.json", {"results": [one], "next_cursor": None}),
            self.save("list.json", [two]),
            self.save("single.json", three),
        )
        assert_that(rendered, contains_string(">3 messages<"))

    def test_message_page(self) -> None:
        """A message with --html and --page is a full document with a frame."""
        headers = {"from": "x@y", "to": ["me@z"], "subject": "Hi", "date": 0}
        message = {"id": "A", "headers": headers, "body": "text", "tags": ["inbox"]}
        body = self.directory / "body.html"
        body.write_text("<p>Hello</p>", encoding="utf-8")
        rendered = self.run_cli(
            "--page",
            "--now",
            "0",
            "message",
            self.save("m.json", message),
            "--html",
            str(body),
        )
        assert_that(rendered, starts_with("<!DOCTYPE html>"))
        assert_that(rendered, contains_string("<title>Hi</title>"))
        assert_that(rendered, contains_string("srcdoc="))

    def test_message_fragment(self) -> None:
        """Without --html, the plain text body is shown."""
        message = {"id": "A", "headers": {"subject": "Hi"}, "body": "text"}
        rendered = self.run_cli("message", self.save("m.json", message))
        assert_that(rendered, contains_string(">text</p>"))

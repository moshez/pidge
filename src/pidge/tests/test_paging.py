"""Tests for search ordering and cursor paging."""

import base64
import email.utils
import unittest
from collections.abc import Callable, Mapping, Sequence

from hamcrest import assert_that, equal_to, has_key, has_length, instance_of, none

from .. import jsonvalue
from ..docstore import shard_keys
from ..documents import Query
from ..indexer import run_tracked
from ..search import search
from ..tagging import tag_email
from ..toolkit import Services, ToolError
from ..tools_read import run_search
from .checks import failure
from .fakes import World, deliver, make_world, new_cache, sample_raw
from .sessionkey import SESSION_KEY

BASE = 1_767_225_600
YEAR = 366 * 86400
Between = Callable[[int], object]


def receive_dated(world: World, name: str, when: int) -> str:
    """Receive a message with a given Date header, a second after the last.

    Args:
        world: The environment.
        name: The SES object name (also the subject).
        when: The Date header in epoch seconds.

    Returns:
        The message's ULID.
    """
    raw = sample_raw(name, "Hello", {}).replace(
        b"Thu, 01 Jan 2026 00:00:00 +0000",
        email.utils.formatdate(when, usegmt=True).encode(),
    )
    world.clock.advance(1)
    return deliver(world, name, raw)


def receive_run(world: World, prefix: str, start: int, count: int) -> Sequence[str]:
    """Receive messages dated one second apart.

    Args:
        world: The environment.
        prefix: The SES object name prefix.
        start: The first message's date.
        count: How many to receive.

    Returns:
        Their IDs, oldest first.
    """
    return [receive_dated(world, f"{prefix}{n}", start + n) for n in range(count)]


def page_ids(found: Mapping[str, object]) -> Sequence[str]:
    """List the IDs on a page of results.

    Args:
        found: The ``search`` tool's result.

    Returns:
        The IDs, in order.
    """
    return jsonvalue.pluck(found, "results", "id")


def next_cursor(found: Mapping[str, object]) -> str:
    """Return a page's cursor.

    Args:
        found: The ``search`` tool's result.

    Returns:
        The cursor, or an empty string when there are no more pages.
    """
    value = found["next_cursor"]
    return "" if value is None else jsonvalue.as_str(value)


def call(world: World, args: Mapping[str, object]) -> Mapping[str, object]:
    """Call the ``search`` tool.

    Args:
        world: The environment.
        args: The arguments.

    Returns:
        The result.
    """
    services = Services(
        context=world.context,
        cache=new_cache(world, "cache"),
        session_key=SESSION_KEY,
    )
    return jsonvalue.as_dict(run_search(services, args))


def nothing(number: int) -> int:
    """Do nothing between pages.

    Args:
        number: The page number.

    Returns:
        The page number.
    """
    return number


def page_through(
    world: World, args: Mapping[str, object], between: Between = nothing
) -> Sequence[str]:
    """Follow cursors to the last page.

    Args:
        world: The environment.
        args: The arguments, without a cursor.
        between: Called with the page number after each page but the last.

    Returns:
        The IDs from every page, in order.
    """
    found = call(world, args)
    collected = list(page_ids(found))
    number = 0
    while cursor := next_cursor(found):
        between(number)
        number += 1
        found = call(world, {**args, "cursor": cursor})
        collected.extend(page_ids(found))
    return collected


def fill_mailbox(world: World) -> Sequence[str]:
    """Fill a mailbox: indexed, unindexed and dirty, with a same-second run.

    Args:
        world: The environment.

    Returns:
        The IDs, newest first.
    """
    dates = [BASE, BASE + 60, BASE + 120, *[BASE + 300] * 5, BASE + 600]
    indexed = [receive_dated(world, f"i{n}", when) for n, when in enumerate(dates)]
    run_tracked(world.context)
    tag_email(world.store, indexed[4], ["seen"], [])
    later = [BASE + 300, BASE + 300, BASE + 900]
    fresh = [receive_dated(world, f"f{n}", when) for n, when in enumerate(later)]
    by_position = {
        message: (when, message)
        for message, when in zip([*indexed, *fresh], [*dates, *later], strict=True)
    }
    return sorted(by_position, key=by_position.__getitem__, reverse=True)


class TestDateOrder(unittest.TestCase):
    """Without terms, index and tail hits share one newest-first order."""

    def test_newest_overall(self) -> None:
        """Newer unindexed and dirty mail beats older indexed mail."""
        world = make_world(self)
        indexed = [receive_dated(world, f"i{n}", BASE + n * 60) for n in range(5)]
        run_tracked(world.context)
        fresh = receive_run(world, "f", BASE + 900, 2)
        tag_email(world.store, indexed[-1], ["seen"], [])
        cache = new_cache(world, "cache")
        page = search(world.store, cache, Query(tags=["inbox"], limit=4))
        expected = [fresh[1], fresh[0], indexed[4], indexed[3]]
        assert_that([hit.ulid for hit in page.hits], equal_to(expected))


class TestPaging(unittest.TestCase):
    """Date-order cursors walk the whole result set exactly once."""

    def setUp(self) -> None:
        """Fill a mailbox."""
        self.world = make_world(self)
        self.newest = fill_mailbox(self.world)

    def assert_pages(self, args: Mapping[str, object], between: Between) -> None:
        """Check that paging returns every message once, newest first.

        Args:
            args: The search arguments.
            between: Called after each page but the last.
        """
        collected = page_through(self.world, args, between)
        assert_that(collected, equal_to(self.newest))

    def test_unpaged_order(self) -> None:
        """One big page lists everything newest first, with no cursor."""
        found = call(self.world, {"tags": ["inbox"]})
        assert_that(page_ids(found), equal_to(self.newest))
        assert_that(found["next_cursor"], none())

    def test_pages(self) -> None:
        """Small pages add up to the unpaged result, across a same-second run."""
        self.assert_pages({"tags": ["inbox"], "limit": 3}, nothing)

    def test_terms_as_filter(self) -> None:
        """In date order, query words only filter, and paging still works."""
        args = {"query": "hello", "order": "date", "limit": 4}
        found = call(self.world, args)
        assert_that(jsonvalue.pluck(found, "results", "snippet"), has_length(4))
        self.assert_pages(args, nothing)

    def test_changes_between_pages(self) -> None:
        """Tagging, new mail and an indexer run between pages skip nothing."""
        changes: dict[int, Callable[[], object]] = {
            0: lambda: tag_email(self.world.store, self.newest[-2], ["x"], []),
            1: lambda: receive_dated(self.world, "new", BASE + 5000),
            2: lambda: run_tracked(self.world.context),
        }
        self.assert_pages(
            {"tags": ["inbox"], "limit": 3}, lambda number: changes[number]()
        )

    def test_relevance_has_no_cursor(self) -> None:
        """Ranked results never carry a cursor, even when more matched."""
        found = call(self.world, {"query": "hello", "limit": 2})
        assert_that(page_ids(found), has_length(2))
        assert_that(found["next_cursor"], none())


class TestShards(unittest.TestCase):
    """Paging spans yearly shards and the tail."""

    def test_two_years(self) -> None:
        """Messages from two shards and the tail come back in order."""
        world = make_world(self)
        old = receive_run(world, "o", BASE, 3)
        world.clock.advance(YEAR)
        new = receive_run(world, "n", BASE + YEAR, 3)
        run_tracked(world.context)
        assert_that(shard_keys(world.store), has_length(2))
        tail = receive_run(world, "t", BASE + 2 * YEAR, 2)
        expected = [*reversed(tail), *reversed(new), *reversed(old)]
        assert_that(page_through(world, {"limit": 2}), equal_to(expected))

    def test_one_shard(self) -> None:
        """A shard with more matches than the limit has another page."""
        world = make_world(self)
        sent = receive_run(world, "m", BASE, 5)
        run_tracked(world.context)
        assert_that(page_through(world, {"limit": 2}), equal_to(sent[::-1]))


class TestBadCursors(unittest.TestCase):
    """Cursors are checked."""

    def setUp(self) -> None:
        """Make an empty mailbox."""
        self.world = make_world(self)

    def fails(self, args: Mapping[str, object]) -> str:
        """Call search and expect a tool error.

        Args:
            args: The arguments.

        Returns:
            The error message.
        """
        error = failure(lambda: call(self.world, args))
        assert_that(error, instance_of(ToolError))
        return str(error)

    def test_relevance(self) -> None:
        """A cursor needs date order."""
        good = call(self.world, {})
        assert_that(good["next_cursor"], none())
        cursor = base64.urlsafe_b64encode(b"5:01KDVDNGTR0000000000000007").decode()
        assert_that(call(self.world, {"cursor": cursor}), has_key("results"))
        for args in [{"query": "hi"}, {"order": "relevance"}]:
            message = self.fails({**args, "cursor": cursor})
            assert_that(message, equal_to("cursor only works with order 'date'"))

    def test_malformed(self) -> None:
        """Cursors that did not come from a search are rejected."""
        texts = [b"5", b"x:01KDVDNGTR0000000000000007", b"5:nope", b"\xff"]
        malformed = ["!!!", *[base64.urlsafe_b64encode(raw).decode() for raw in texts]]
        for value in malformed:
            message = self.fails({"cursor": value})
            assert_that(message, equal_to("cursor is malformed"))

    def test_bad_order(self) -> None:
        """Only two orders exist."""
        message = self.fails({"order": "random"})
        assert_that(message, equal_to("order must be one of date, relevance"))

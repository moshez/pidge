"""Tests for the indexer, search and tags."""

import dataclasses
import unittest
from collections.abc import Sequence

from hamcrest import (
    assert_that,
    contains_exactly,
    contains_inanyorder,
    empty,
    equal_to,
    has_item,
    has_length,
    instance_of,
)

from ..docstore import THRESHOLD, maybe_trigger_index
from ..documents import Hit, Query
from ..indexer import FAILURES_KEY, run_tracked, write_shard
from ..ports import ShardSource
from ..search import merge, query_shard, search
from ..store import ObjectStore, PreconditionFailed
from ..tagging import TagError, list_tags, tag_email
from .checks import failure, includes
from .fakes import (
    World,
    deliver,
    keys,
    make_world,
    new_cache,
    sample_raw,
)
from .flaky import failing, racing


def receive(world: World, name: str, subject: str, text: str) -> str:
    """Receive a message; one whose text says "attached" gets a PDF.

    Args:
        world: The environment.
        name: The SES object name.
        subject: The subject.
        text: The body.

    Returns:
        The message's ULID.
    """
    attachments = {"report.pdf": b"%PDF"} if "attached" in text else {}
    return deliver(world, name, sample_raw(subject, text, attachments))


def found(store: ObjectStore, cache: ShardSource, query: Query) -> Sequence[str]:
    """Search and return the IDs of the hits.

    Args:
        store: The mail bucket.
        cache: The shard cache.
        query: The query.

    Returns:
        The IDs, in order.
    """
    return [hit.ulid for hit in search(store, cache, query).hits]


class TestSearch(unittest.TestCase):
    """Search covers both the shards and the unindexed tail."""

    def setUp(self) -> None:
        """Ingest and index two messages."""
        self.world = make_world(self)
        self.store = self.world.store
        self.cache = new_cache(self.world, "cache")
        self.lunch = receive(self.world, "a", "Lunch plans", "Tacos on Friday?")
        self.world.clock.advance(60)
        self.report = receive(self.world, "b", "Quarterly report", "Report attached.")
        assert_that(run_tracked(self.world.context), equal_to(2))

    def find(self, query: Query) -> Sequence[str]:
        """Search and return the IDs.

        Args:
            query: The query.

        Returns:
            The matching IDs.
        """
        return found(self.store, self.cache, query)

    def hits(self, word: str) -> Sequence[Hit]:
        """Search for a word.

        Args:
            word: The word.

        Returns:
            The hits.
        """
        return search(self.store, self.cache, Query(terms=[word])).hits

    def test_indexed(self) -> None:
        """Indexed messages are found by words and by filters."""
        assert_that(self.store.list("unindexed/", 10), empty())
        expectations = [
            (Query(terms=["tacos"]), [self.lunch]),
            (Query(), [self.report, self.lunch]),
            (Query(has_attachments=True), [self.report]),
            (Query(sender="alice", recipient="bob"), [self.report, self.lunch]),
            (Query(before=1, tags=["inbox"]), []),
        ]
        for query, expected in expectations:
            assert_that(self.find(query), equal_to(expected))

    def test_tail_and_dirty(self) -> None:
        """New and retagged messages are served from the tail."""
        fresh = receive(self.world, "c", "Tacos again", "More tacos")
        tag_email(self.store, self.lunch, ["food"], ["inbox"])
        hits = self.hits("tacos")
        assert_that([hit.ulid for hit in hits], contains_inanyorder(fresh, self.lunch))
        assert_that([hit.score for hit in hits], equal_to([None, None]))
        food = Query(tags=["food"])
        assert_that(self.find(food), equal_to([self.lunch]))
        counts = list_tags(self.store, self.cache)
        assert_that(counts, equal_to({"food": 1, "inbox": 2}))

    def test_reindex_and_cache(self) -> None:
        """Reindexing replaces rows; the cache notices new and missing shards."""
        assert_that(self.find(Query(terms=["report"])), equal_to([self.report]))
        tag_email(self.store, self.report, ["work"], [])
        run_tracked(self.world.context)
        hits = self.hits("report")
        assert_that(hits, has_length(1))
        assert_that(hits[0].tags, contains_inanyorder("inbox", "work"))
        assert_that(hits[0].score, instance_of(float))
        self.store.delete("index/index-2026.db")
        assert_that(self.find(Query()), empty())
        assert_that(list_tags(self.store, self.cache), equal_to({}))

    def test_bad_tags(self) -> None:
        """Tags must be single words."""
        change = failure(lambda: tag_email(self.store, self.lunch, ["a b"], []))
        assert_that(change, instance_of(TagError))


class TestIndexer(unittest.TestCase):
    """The indexer is triggered, retried and reports repeated failures."""

    def test_trigger(self) -> None:
        """The indexer is invoked once enough messages are waiting."""
        world = make_world(self)
        assert_that(maybe_trigger_index(world.context), equal_to(False))
        for index in range(THRESHOLD):
            world.store.put(f"unindexed/{index:026d}", b"")
        assert_that(maybe_trigger_index(world.context), equal_to(True))
        assert_that(list(world.invoker.invocations()), equal_to([{"action": "index"}]))

    def test_orphans(self) -> None:
        """Markers without records are dropped once they are old."""
        world = make_world(self)
        marker = f"unindexed/{world.context.new_id()}"
        world.store.put(marker, b"")
        assert_that(run_tracked(world.context), equal_to(0))
        assert_that(keys(world), has_item(marker))
        world.clock.advance(3601)
        run_tracked(world.context)
        assert_that(marker in keys(world), equal_to(False))

    def test_races(self) -> None:
        """Lost races are retried, and give up after a few attempts."""
        world = make_world(self)
        for name, losses in [("a", 2), ("b", 1)]:
            receive(world, name, "Hi", "There")
            assert_that(run_tracked(racing(world, "index/", losses)), equal_to(1))
        losing = racing(world, "index/", 99)
        attempt = failure(lambda: write_shard(losing, 2026, []))
        assert_that(attempt, instance_of(PreconditionFailed))

    def test_failures_become_mail(self) -> None:
        """Two failed runs in a row write an ops message."""
        world = make_world(self)
        receive(world, "a", "Hi", "There")
        broken = failing(world, "index/", 2)
        for _ in range(2):
            assert_that(failure(lambda: run_tracked(broken)), instance_of(OSError))
        cache = new_cache(world, "c")
        ops = search(broken.store, cache, Query(tags=["ops"])).hits
        subject = "[pidge ops] The search indexer has failed 2 times in a row"
        assert_that([hit.subject for hit in ops], equal_to([subject]))
        assert_that(run_tracked(broken), equal_to(2))
        assert_that(FAILURES_KEY in keys(world), equal_to(False))
        for _ in range(2):
            again = found(broken.store, cache, Query(tags=["ops"]))
            assert_that(again, equal_to([ops[0].ulid]))
        includes(list_tags(world.store, cache), dict(ops=1))


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class GoneCache:
    """A shard source whose shards have all disappeared."""

    def fetch(self, store: object, key: str) -> str:
        """Report the shard as gone.

        Args:
            store: The mail bucket.
            key: The shard key.

        Returns:
            An empty path.
        """
        return ""


class TestEdges(unittest.TestCase):
    """Corner cases of the cache and the merge."""

    def test_missing_shard(self) -> None:
        """A shard that vanishes reads as empty."""
        world = make_world(self)
        receive(world, "a", "Hi", "There")
        run_tracked(world.context)
        cache = new_cache(world, "cache")
        assert_that(cache.fetch(world.store, "index/index-1999.db"), equal_to(""))
        assert_that(query_shard("", Query(), ()), empty())
        assert_that(list_tags(world.store, GoneCache()), equal_to({}))

    def test_merge_dedupes(self) -> None:
        """A hit found twice is listed once."""
        hit = Hit(
            ulid="A", date=1, sender="", subject="", snippet="", tags=(), score=None
        )
        assert_that(merge([hit], [hit], Query()).hits, contains_exactly(hit))

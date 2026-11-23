"""Tests for inbound mail: ingest, the sweep and quarantine."""

import unittest

from hamcrest import assert_that, contains_exactly, equal_to, has_item, instance_of

from ..ingest import ingest, ingest_or_record, sweep_inbound
from .checks import failure
from .fakes import PLAIN, assert_tags, body, drop, keys, make_world, sample_raw
from .flaky import failing


class TestIngest(unittest.TestCase):
    """Ingest is idempotent and resumable."""

    def setUp(self) -> None:
        """Create a world with one message waiting in inbound/."""
        self.world = make_world(self)
        self.context = self.world.context
        self.key = drop(self.world, "ses1", PLAIN)

    def test_ingest(self) -> None:
        """A message lands in msgs/ tagged inbox, and is marked done."""
        ulid = ingest(self.context, self.key)
        assert_tags(self.world, ulid, "inbox")
        done = body(self.world, "inbound-done/ses1")
        assert_that(done, equal_to(ulid.encode()))
        assert_that(keys(self.world), has_item("unindexed/" + ulid))

    def test_resume_uses_same_id(self) -> None:
        """A retry after a crash resumes with the ULID in the seen marker."""
        context = failing(self.world, "msgs/", 1)
        assert_that(failure(lambda: ingest(context, self.key)), instance_of(OSError))
        claimed = body(self.world, "inbound-seen/ses1").decode()
        assert_that(ingest(context, self.key), equal_to(claimed))

    def test_setup_notification(self) -> None:
        """SES's setup notification is skipped."""
        name = "AMAZON_SES_SETUP_NOTIFICATION"
        assert_that(ingest(self.context, f"inbound/{name}"), equal_to(""))
        assert_that(keys(self.world), has_item(f"inbound-done/{name}"))

    def test_threshold_triggers_indexer(self) -> None:
        """Enough unindexed messages start the batch indexer."""
        for number in range(100):
            self.world.store.put(f"unindexed/X{number}", b"")
        ingest(self.context, self.key)
        invoked = self.world.invoker.invocations()
        assert_that(list(invoked), equal_to([{"action": "index"}]))


class TestPoison(unittest.TestCase):
    """Messages that keep failing are quarantined, not lost."""

    def test_quarantine_after_attempts(self) -> None:
        """The fifth failure stores the raw message tagged ingest-failed."""
        world = make_world(self)
        key = drop(world, "bad", b"Subject: x\n\nbody")
        context = failing(world, "msgs/", 5)
        for _ in range(4):
            assert_that(
                failure(lambda: ingest_or_record(context, key)), instance_of(OSError)
            )
        ulid = ingest_or_record(context, key)
        assert_tags(world, ulid, "inbox", "ingest-failed")
        raw = body(world, f"msgs/{ulid}/raw.eml")
        assert_that(raw, equal_to(b"Subject: x\n\nbody"))
        ops = [name for name in keys(world) if name.startswith("ops-sent/")]
        assert_that(ops, contains_exactly("ops-sent/ingest-failed-bad/2026-01-01"))


class TestSweep(unittest.TestCase):
    """The sweep retries inbound objects that never finished."""

    def test_sweep(self) -> None:
        """Old unfinished messages are retried; failures don't stop the sweep."""
        world = make_world(self)
        for name in ["old", "done"]:
            drop(world, name, sample_raw(name, "x", {}))
        drop(world, "broken", b"x")
        world.store.put("inbound-done/done", b"")
        world.clock.advance(1000)
        drop(world, "young", sample_raw("Young", "x", {}))
        context = failing(world, "inbound-seen/broken", 1)
        assert_that(sweep_inbound(context), equal_to(2))
        stored = keys(world)
        for marker in ["inbound-done/old", "inbound-failures/broken"]:
            assert_that(stored, has_item(marker))

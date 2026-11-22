"""Tests for message records and meta compare-and-swap."""

import unittest

from hamcrest import assert_that, contains_exactly, equal_to, has_item, instance_of

from ..memstore import MemoryStore
from ..mimeparse import parse_message
from ..model import Direction, Meta
from ..records import (
    NewMessage,
    read_json,
    read_meta,
    retag,
    update_meta,
    write_message,
)
from ..store import PreconditionFailed
from .checks import failure
from .fakes import sample_raw


def new_message() -> NewMessage:
    """Build a message to store.

    Returns:
        The message.
    """
    raw = sample_raw("Hi", "Body", {"a.pdf": b"%PDF"})
    meta = Meta(tags=("inbox",), direction=Direction.IN, status="")
    return NewMessage(raw=raw, parsed=parse_message(raw), meta=meta)


class TestRecords(unittest.TestCase):
    """Writing and updating records."""

    def setUp(self) -> None:
        """Create an empty store."""
        self.store = MemoryStore.create(float)

    def test_write_is_idempotent(self) -> None:
        """A repeat write keeps tags changed in between."""
        assert_that(write_message(self.store, "M1", new_message()), equal_to(True))
        update_meta(self.store, "M1", retag(["work"], ["inbox"]))
        assert_that(write_message(self.store, "M1", new_message()), equal_to(False))
        assert_that(read_meta(self.store, "M1")[0].tags, contains_exactly("work"))
        stored = self.store.keys()
        for key in ["msgs/M1/attachments/0-a.pdf", "dirty/M1"]:
            assert_that(stored, has_item(key))
        headers = read_json(self.store, "msgs/M1/headers.json")
        assert_that(
            headers["attachments"],
            equal_to([dict(name="0-a.pdf", content_type="application/pdf", size=4)]),
        )

    def test_read_json_missing(self) -> None:
        """A missing JSON record reads as empty."""
        assert_that(read_json(self.store, "nope"), equal_to({}))

    def test_update_gives_up(self) -> None:
        """Losing every race raises PreconditionFailed."""
        write_message(self.store, "M1", new_message())

        def meddle(meta: Meta) -> Meta:
            self.store.put("msgs/M1/meta.json", b'{"tags": []}')
            return meta

        assert_that(
            failure(lambda: update_meta(self.store, "M1", meddle)),
            instance_of(PreconditionFailed),
        )

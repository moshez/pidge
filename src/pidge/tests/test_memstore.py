"""Tests for the in-memory object store."""

import unittest

from hamcrest import assert_that, contains_exactly, equal_to, instance_of

from ..memstore import MemoryStore
from ..store import NotFound, NotModified, PreconditionFailed
from .checks import failure


class TestMemoryStore(unittest.TestCase):
    """Conditional semantics match S3's."""

    def setUp(self) -> None:
        """Create an empty store."""
        self.store = MemoryStore.create(lambda: 5.0)

    def test_conditional_writes(self) -> None:
        """put_new and replace enforce their preconditions."""
        etag = self.store.put_new("k", b"1")
        assert_that(
            failure(lambda: self.store.put_new("k", b"2")),
            instance_of(PreconditionFailed),
        )
        newer = self.store.replace("k", b"2", etag)
        for key, error in [("k", PreconditionFailed), ("x", NotFound)]:
            stale = failure(lambda: self.store.replace(key, b"3", etag))
            assert_that(stale, instance_of(error))
        assert_that(self.store.get("k").body, equal_to(b"2"))
        assert_that(
            failure(lambda: self.store.get_if_changed("k", newer)),
            instance_of(NotModified),
        )
        assert_that(self.store.get_if_changed("k", etag).body, equal_to(b"2"))
        assert_that(
            failure(lambda: self.store.get_if_changed("x", etag)), instance_of(NotFound)
        )

    def test_deletes(self) -> None:
        """delete_if checks the ETag; delete ignores missing objects."""
        etag = self.store.put("k", b"1")
        assert_that(
            failure(lambda: self.store.delete_if("k", "wrong")),
            instance_of(PreconditionFailed),
        )
        self.store.delete_if("k", etag)
        self.store.delete("k")
        assert_that(failure(lambda: self.store.get("k")), instance_of(NotFound))

    def test_listing(self) -> None:
        """Listing is ordered, prefixed and limited."""
        for key in ["b/2", "b/1", "a/1"]:
            self.store.put(key, b"")
        listed = self.store.list("b/", 1)
        assert_that([entry.key for entry in listed], contains_exactly("b/1"))
        assert_that(listed[0].modified, equal_to(5.0))
        assert_that(self.store.keys(), contains_exactly("a/1", "b/1", "b/2"))

    def test_presign(self) -> None:
        """Presigned URLs describe the request."""
        assert_that(self.store.presign("k", "f.pdf"), equal_to(""))
        self.store.put("k", b"")
        assert_that(
            self.store.presign("k", "f.pdf"), equal_to("memory:k?filename=f.pdf")
        )

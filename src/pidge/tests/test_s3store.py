"""Tests for the S3 adapter, against a fake boto3 client."""

import dataclasses
import datetime
import io
import unittest
from collections.abc import Mapping, Sequence

from hamcrest import (
    assert_that,
    contains_exactly,
    equal_to,
    instance_of,
)

from ..s3store import S3Store, read_body, translate
from ..store import NotFound, NotModified, PreconditionFailed
from .checks import failure, includes
from .flaky import client_error


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class FakeS3:
    """Records calls and replays canned responses.

    Construct it with :meth:`create`.
    """

    _calls: list[tuple[str, Mapping[str, object]]]
    _responses: list[object]

    @classmethod
    def create(cls, *responses: object) -> "FakeS3":
        """Create a fake that answers with the given responses in order.

        Args:
            *responses: Mappings to return, or exceptions to raise.

        Returns:
            The fake.
        """
        return cls(_calls=[], _responses=list(responses))

    def answer(self, name: str, kwargs: Mapping[str, object]) -> Mapping[str, object]:
        """Record a call and return the next response.

        Args:
            name: The operation.
            kwargs: Its parameters.

        Returns:
            The canned response.

        Raises:
            response: If the canned response is an exception.
        """
        self._calls.append((name, kwargs))
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        assert isinstance(response, dict)
        return response

    def recorded(self) -> Sequence[tuple[str, Mapping[str, object]]]:
        """Return the recorded calls.

        Returns:
            Operation names and parameters.
        """
        return list(self._calls)

    def get_object(self, **kw: object) -> Mapping[str, object]:  # noqa: SLD801
        """Fake GetObject.

        Args:
            **kw: Parameters.

        Returns:
            The response.
        """
        return self.answer("get_object", kw)

    def put_object(self, **kw: object) -> Mapping[str, object]:  # noqa: SLD801
        """Fake PutObject.

        Args:
            **kw: Parameters.

        Returns:
            The response.
        """
        return self.answer("put_object", kw)

    def delete_object(self, **kw: object) -> Mapping[str, object]:  # noqa: SLD801
        """Fake DeleteObject.

        Args:
            **kw: Parameters.

        Returns:
            The response.
        """
        return self.answer("delete_object", kw)

    def list_objects_v2(self, **kw: object) -> Mapping[str, object]:  # noqa: SLD801
        """Fake ListObjectsV2.

        Args:
            **kw: Parameters.

        Returns:
            The response.
        """
        return self.answer("list_objects_v2", kw)

    def generate_presigned_url(self, *args: object, **kwargs: object) -> str:
        """Fake presigning.

        Args:
            *args: The operation name.
            **kwargs: Parameters.

        Returns:
            A fake URL.
        """
        self._calls.append(("presign", dict(kwargs, operation=args[0])))
        return "https://signed"


def body(data: bytes) -> Mapping[str, object]:
    """Build a GetObject response.

    Args:
        data: The object contents.

    Returns:
        The response.
    """
    return {"Body": io.BytesIO(data), "ETag": '"e1"'}


class TestS3Store(unittest.TestCase):
    """Requests carry the right conditions; errors are translated."""

    def test_reads(self) -> None:
        """Reads return bodies and ETags, with conditions applied."""
        fake = FakeS3.create(body(b"x"), body(b"y"), client_error("304", 304))
        store = S3Store.create(fake, "bucket", fake)
        assert_that(store.get("k").body, equal_to(b"x"))
        assert_that(store.get_if_changed("k", '"e0"').etag, equal_to('"e1"'))
        assert_that(
            failure(lambda: store.get_if_changed("k", '"e1"')), instance_of(NotModified)
        )
        includes(fake.recorded()[1][1], dict(IfNoneMatch='"e0"', Bucket="bucket"))

    def test_writes(self) -> None:
        """Writes use If-None-Match and If-Match."""
        etag = {"ETag": '"e2"'}
        conflict = client_error("PreconditionFailed", 412)
        fake = FakeS3.create(etag, etag, etag, conflict, {}, {})
        store = S3Store.create(fake, "bucket", fake)
        assert_that(store.put("k", b"1"), equal_to('"e2"'))
        store.put_new("k", b"1")
        store.replace("k", b"1", '"e1"')
        assert_that(
            failure(lambda: store.put_new("k", b"1")), instance_of(PreconditionFailed)
        )
        store.delete("k")
        store.delete_if("k", '"e2"')
        params = [kwargs for _, kwargs in fake.recorded()]
        includes(params[1], dict(IfNoneMatch="*"))
        includes(params[2], dict(IfMatch='"e1"'))
        includes(params[5], dict(IfMatch='"e2"'))

    def test_listing_pages(self) -> None:
        """Listing follows continuation tokens and stops at the limit."""
        stamp = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
        first = {
            "Contents": [{"Key": "a", "ETag": '"1"', "LastModified": stamp}],
            "NextContinuationToken": "t",
        }
        second = {"Contents": [{"Key": "b", "ETag": '"2"', "LastModified": "?"}]}
        fake = FakeS3.create(first, second)
        listed = S3Store.create(fake, "bucket", fake).list("", 5)
        assert_that([entry.key for entry in listed], contains_exactly("a", "b"))
        assert_that(listed[0].modified, equal_to(stamp.timestamp()))
        includes(fake.recorded()[1][1], dict(ContinuationToken="t"))

    def test_presign(self) -> None:
        """Downloads suggest the file name; uploads sign the body size."""
        fake = FakeS3.create()
        store = S3Store.create(FakeS3.create(), "b", fake)
        urls = [store.presign("k", "a b.pdf", 600), store.presign_upload("u", 7, 900)]
        assert_that(urls, equal_to(["https://signed"] * 2))
        (_, download), (_, upload) = fake.recorded()
        includes(download, dict(operation="get_object", ExpiresIn=600))
        includes(upload, dict(operation="put_object", ExpiresIn=900))
        includes(upload["Params"], dict(ContentLength=7, Key="u"))

    def test_translate(self) -> None:
        """Error codes and statuses map to store exceptions."""
        found = translate(client_error("NoSuchKey", 404), "k")
        assert_that(isinstance(found, NotFound), equal_to(True))
        other = client_error("AccessDenied", 403)
        assert_that(translate(other, "k"), equal_to(other))
        missing = translate(client_error("Unknown", 404), "k")
        assert_that(isinstance(missing, NotFound), equal_to(True))

    def test_read_body_checks(self) -> None:
        """Bodies must be readable and return bytes."""
        assert_that(failure(lambda: read_body({"Body": 1})), instance_of(TypeError))
        text = {"Body": io.StringIO("x")}
        assert_that(failure(lambda: read_body(text)), instance_of(TypeError))

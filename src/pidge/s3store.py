"""An object store backed by Amazon S3, through a boto3 client."""

import contextlib
import dataclasses
import itertools
import urllib.parse
from collections.abc import Iterator, Mapping, Sequence
from typing import Protocol

from . import jsonvalue
from .store import Listed, NotFound, NotModified, PreconditionFailed, StoredObject

PRESIGN_SECONDS = 900
_ERRORS: Mapping[str, type[Exception]] = {
    "304": NotModified,
    "NotModified": NotModified,
    "412": PreconditionFailed,
    "PreconditionFailed": PreconditionFailed,
    "409": PreconditionFailed,
    "ConditionalRequestConflict": PreconditionFailed,
    "404": NotFound,
    "NoSuchKey": NotFound,
}


class Readable(Protocol):
    """A streaming response body."""

    def read(self) -> bytes:
        """Read the whole body.

        Returns:
            The bytes.
        """


class S3Client(Protocol):
    """The subset of the boto3 S3 client that this module uses."""

    def get_object(self, **kwargs: object) -> Mapping[str, object]:
        """Get an object.

        Args:
            **kwargs: Request parameters.

        Returns:
            The response.
        """

    def put_object(self, **kwargs: object) -> Mapping[str, object]:
        """Put an object.

        Args:
            **kwargs: Request parameters.

        Returns:
            The response.
        """

    def delete_object(self, **kwargs: object) -> Mapping[str, object]:
        """Delete an object.

        Args:
            **kwargs: Request parameters.

        Returns:
            The response.
        """

    def list_objects_v2(self, **kwargs: object) -> Mapping[str, object]:
        """List objects.

        Args:
            **kwargs: Request parameters.

        Returns:
            The response.
        """

    def generate_presigned_url(self, *args: object, **kwargs: object) -> str:
        """Create a presigned URL.

        Args:
            *args: Positional parameters.
            **kwargs: Request parameters.

        Returns:
            The URL.
        """


def translate(error: Exception, key: str) -> Exception:
    """Map a botocore ClientError to this package's store exceptions.

    Args:
        error: The exception raised by the client.
        key: The object key involved, for the message.

    Returns:
        The store exception, or the original error if it is unrelated.
    """
    meta = jsonvalue.get_dict(
        jsonvalue.as_dict(getattr(error, "response", {})), "ResponseMetadata"
    )
    candidates = [
        jsonvalue.error_details(error).get("Code"),
        meta.get("HTTPStatusCode"),
    ]
    for code in candidates:
        mapped = _ERRORS.get(str(code))
        if mapped is not None:
            return mapped(key)
    return error


@contextlib.contextmanager
def client_errors(key: str) -> Iterator[None]:  # noqa: DOC503
    """Translate client errors raised inside the block into store errors.

    Args:
        key: The object key involved, for the message.

    Yields:
        Nothing.

    Raises:
        Exception: The translated store error, or the original error.
    """
    try:
        yield
    except Exception as error:
        raise translate(error, key) from error


def read_body(response: Mapping[str, object]) -> bytes:
    """Read the body of a GetObject response.

    Args:
        response: The response.

    Returns:
        The bytes.

    Raises:
        TypeError: If the body is not readable.
    """
    body = response["Body"]
    reader = getattr(body, "read", None)
    if reader is None:
        raise TypeError("response body is not readable")
    data: object = reader()
    if not isinstance(data, bytes):
        raise TypeError("response body did not return bytes")
    return data


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class S3Store:
    """An object store in one S3 bucket.

    Construct it with :meth:`create`.
    """

    _client: S3Client
    _bucket: str

    @classmethod
    def create(cls, client: S3Client, bucket: str) -> "S3Store":
        """Wrap a boto3 S3 client.

        Args:
            client: The boto3 client.
            bucket: The bucket name.

        Returns:
            The store.
        """
        return cls(_client=client, _bucket=bucket)

    def get(self, key: str) -> StoredObject:
        """Read an object.

        Args:
            key: The object key.

        Returns:
            The stored object.

        """
        with client_errors(key):
            response = self._client.get_object(Bucket=self._bucket, Key=key)
        etag = jsonvalue.as_str(response["ETag"])
        return StoredObject(body=read_body(response), etag=etag)

    def get_if_changed(self, key: str, etag: str) -> StoredObject:
        """Read an object unless it still has the given ETag.

        Args:
            key: The object key.
            etag: The ETag of the copy the caller already has.

        Returns:
            The stored object.

        """
        with client_errors(key):
            response = self._client.get_object(
                Bucket=self._bucket, Key=key, IfNoneMatch=etag
            )
        new_etag = jsonvalue.as_str(response["ETag"])
        return StoredObject(body=read_body(response), etag=new_etag)

    def put(self, key: str, body: bytes) -> str:
        """Write an object unconditionally.

        Args:
            key: The object key.
            body: The bytes to write.

        Returns:
            The new ETag.
        """
        response = self._client.put_object(Bucket=self._bucket, Key=key, Body=body)
        return jsonvalue.as_str(response["ETag"])

    def put_new(self, key: str, body: bytes) -> str:
        """Create an object.

        Args:
            key: The object key.
            body: The bytes to write.

        Returns:
            The new ETag.

        """
        with client_errors(key):
            response = self._client.put_object(
                Bucket=self._bucket, Key=key, Body=body, IfNoneMatch="*"
            )
        return jsonvalue.as_str(response["ETag"])

    def replace(self, key: str, body: bytes, etag: str) -> str:
        """Overwrite an object only if it still has the given ETag.

        Args:
            key: The object key.
            body: The bytes to write.
            etag: The ETag the object must currently have.

        Returns:
            The new ETag.

        """
        with client_errors(key):
            response = self._client.put_object(
                Bucket=self._bucket, Key=key, Body=body, IfMatch=etag
            )
        return jsonvalue.as_str(response["ETag"])

    def delete(self, key: str) -> None:
        """Delete an object.

        Args:
            key: The object key.
        """
        self._client.delete_object(Bucket=self._bucket, Key=key)

    def delete_if(self, key: str, etag: str) -> None:
        """Delete an object only if it still has the given ETag.

        Args:
            key: The object key.
            etag: The ETag the object must currently have.

        """
        with client_errors(key):
            self._client.delete_object(Bucket=self._bucket, Key=key, IfMatch=etag)

    def list(self, prefix: str, limit: int) -> Sequence[Listed]:
        """List objects under a prefix in key order.

        Args:
            prefix: The key prefix.
            limit: Stop after this many entries.

        Returns:
            The matching entries.
        """
        pages = iter_pages(self._client, self._bucket, prefix)
        entries = (entry for page in pages for entry in parse_listing(page))
        return list(itertools.islice(entries, limit))

    def presign(self, key: str, filename: str) -> str:
        """Create a short-lived download URL.

        Args:
            key: The object key.
            filename: The download file name to suggest.

        Returns:
            The URL.
        """
        quoted = urllib.parse.quote(filename)
        params = dict(
            Bucket=self._bucket,
            Key=key,
            ResponseContentDisposition=f"attachment; filename*=UTF-8''{quoted}",
        )
        return self._client.generate_presigned_url(
            "get_object", Params=params, ExpiresIn=PRESIGN_SECONDS
        )


def iter_pages(
    client: S3Client, bucket: str, prefix: str
) -> Iterator[Mapping[str, object]]:
    """Iterate over the ListObjectsV2 pages for a prefix.

    Args:
        client: The boto3 client.
        bucket: The bucket.
        prefix: The key prefix.

    Yields:
        Each response page.
    """
    extra: Mapping[str, object] = {}
    for _ in itertools.count():
        page = client.list_objects_v2(
            Bucket=bucket, Prefix=prefix, MaxKeys=1000, **extra
        )
        yield page
        token = jsonvalue.get_str(page, "NextContinuationToken")
        if not token:
            return
        extra = dict(ContinuationToken=token)


def parse_listing(page: Mapping[str, object]) -> Sequence[Listed]:
    """Convert one ListObjectsV2 page into listing entries.

    Args:
        page: The response.

    Returns:
        The entries on the page.
    """
    entries = []
    for raw in jsonvalue.get_list(page, "Contents"):
        item = jsonvalue.as_dict(raw)
        modified = item["LastModified"]
        stamp = getattr(modified, "timestamp", None)
        entries.append(
            Listed(
                key=jsonvalue.as_str(item["Key"]),
                etag=jsonvalue.as_str(item["ETag"]),
                modified=float(stamp()) if stamp is not None else 0.0,
            )
        )
    return entries

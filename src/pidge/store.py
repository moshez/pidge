"""The object store interface that every flow uses."""

import dataclasses
from collections.abc import Sequence
from typing import Protocol


class PreconditionFailed(Exception):
    """A conditional write or delete lost its race."""


class NotFound(Exception):
    """The requested object does not exist."""


class NotModified(Exception):
    """A conditional read found the object unchanged."""


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class StoredObject:
    """An object's bytes and entity tag.

    Attributes:
        body: The object's contents.
        etag: The entity tag, used for conditional operations.
    """

    body: bytes
    etag: str


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class Listed:
    """One entry of a prefix listing.

    Attributes:
        key: The object key.
        etag: The entity tag.
        modified: Last-modified time, in seconds since the epoch.
    """

    key: str
    etag: str
    modified: float


class ObjectStore(Protocol):
    """A key/value object store with conditional operations (S3)."""

    def get(self, key: str) -> StoredObject:
        """Read an object.

        Args:
            key: The object key.

        Returns:
            The stored object.
        """

    def get_if_changed(self, key: str, etag: str) -> StoredObject:
        """Read an object unless it still has the given ETag.

        Args:
            key: The object key.
            etag: The ETag of the copy the caller already has.

        Returns:
            The stored object; raises NotModified when unchanged.
        """

    def put(self, key: str, body: bytes) -> str:
        """Write an object unconditionally.

        Args:
            key: The object key.
            body: The bytes to write.

        Returns:
            The new ETag.
        """

    def put_new(self, key: str, body: bytes) -> str:
        """Create an object; raise PreconditionFailed if it exists.

        Args:
            key: The object key.
            body: The bytes to write.

        Returns:
            The new ETag.
        """

    def replace(self, key: str, body: bytes, etag: str) -> str:
        """Overwrite an object only if it still has the given ETag.

        Args:
            key: The object key.
            body: The bytes to write.
            etag: The ETag the object must currently have.

        Returns:
            The new ETag; raises PreconditionFailed on a lost race.
        """

    def delete(self, key: str) -> None:
        """Delete an object; deleting a missing object is not an error.

        Args:
            key: The object key.
        """

    def delete_if(self, key: str, etag: str) -> None:
        """Delete an object only if it still has the given ETag.

        Args:
            key: The object key.
            etag: The ETag the object must currently have.
        """

    def list(self, prefix: str, limit: int) -> Sequence[Listed]:
        """List objects under a prefix in key order.

        Args:
            prefix: The key prefix.
            limit: Stop after this many entries.

        Returns:
            The matching entries.
        """

    def presign(self, key: str, filename: str, seconds: int) -> str:
        """Create a download URL that works for a limited time.

        Args:
            key: The object key.
            filename: The download file name to suggest.
            seconds: How long the URL works.

        Returns:
            The URL.
        """

    def presign_upload(self, key: str, size: int, seconds: int) -> str:
        """Create a PUT URL that only accepts a body of exactly ``size`` bytes.

        Args:
            key: The object key.
            size: The body size, in bytes; it is part of the signature.
            seconds: How long the URL works.

        Returns:
            The URL.
        """

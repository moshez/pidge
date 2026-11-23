"""An in-memory object store with S3's conditional semantics."""

import dataclasses
import hashlib
import urllib.parse
from collections.abc import Callable, Sequence

from .store import Listed, NotFound, NotModified, PreconditionFailed, StoredObject


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class Entry:
    """A stored object and when it was written.

    Attributes:
        stored: The object.
        modified: When it was written, in seconds since the epoch.
    """

    stored: StoredObject
    modified: float


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class MemoryStore:
    """An object store that keeps everything in a dictionary.

    It is used by the tests, and is handy for local experiments.

    Construct it with :meth:`create`.
    """

    _entries: dict[str, Entry]
    _clock: Callable[[], float]
    _counter: list[int]

    @classmethod
    def create(cls, clock: Callable[[], float]) -> "MemoryStore":
        """Create an empty store.

        Args:
            clock: Returns the current time, used for modification times.

        Returns:
            The store.
        """
        return cls(_entries={}, _clock=clock, _counter=[0])

    def keys(self) -> Sequence[str]:
        """List every key, for test assertions.

        Returns:
            The keys in sorted order.
        """
        return sorted(self._entries)

    def get(self, key: str) -> StoredObject:
        """Read an object.

        Args:
            key: The object key.

        Returns:
            The stored object.

        Raises:
            NotFound: If there is no such object.
        """
        try:
            return self._entries[key].stored
        except KeyError:
            raise NotFound(key) from None

    def get_if_changed(self, key: str, etag: str) -> StoredObject:
        """Read an object unless it still has the given ETag.

        Args:
            key: The object key.
            etag: The ETag of the copy the caller already has.

        Returns:
            The stored object.

        Raises:
            NotFound: If there is no such object.
            NotModified: If the ETag matches.
        """
        entry = self._entries.get(key)
        if entry is None:
            raise NotFound(key)
        if entry.stored.etag == etag:
            raise NotModified(key)
        return entry.stored

    def put(self, key: str, body: bytes) -> str:
        """Write an object unconditionally.

        Args:
            key: The object key.
            body: The bytes to write.

        Returns:
            The new ETag.
        """
        self._counter[0] += 1
        digest = hashlib.md5(body, usedforsecurity=False).hexdigest()
        etag = f'"{digest}-{self._counter[0]}"'
        stored = StoredObject(body=body, etag=etag)
        self._entries[key] = Entry(stored=stored, modified=self._clock())
        return etag

    def put_new(self, key: str, body: bytes) -> str:
        """Create an object.

        Args:
            key: The object key.
            body: The bytes to write.

        Returns:
            The new ETag.

        Raises:
            PreconditionFailed: If the object exists.
        """
        if key in self._entries:
            raise PreconditionFailed(key)
        return self.put(key, body)

    def replace(self, key: str, body: bytes, etag: str) -> str:  # noqa: SLD303
        """Overwrite an object only if it still has the given ETag.

        Args:
            key: The object key.
            body: The bytes to write.
            etag: The ETag the object must currently have.

        Returns:
            The new ETag; raises like :meth:`check_etag`.
        """
        self.check_etag(key, etag)
        return self.put(key, body)

    def check_etag(self, key: str, etag: str) -> None:
        """Check that an object exists and still has the given ETag.

        Args:
            key: The object key.
            etag: The expected ETag.

        Raises:
            NotFound: If there is no such object.
            PreconditionFailed: If the ETag does not match.
        """
        if key not in self._entries:
            raise NotFound(key)
        if self._entries[key].stored.etag != etag:
            raise PreconditionFailed(key)

    def delete(self, key: str) -> None:
        """Delete an object; deleting a missing object is a no-op.

        Args:
            key: The object key.
        """
        self._entries.pop(key, None)

    def delete_if(self, key: str, etag: str) -> None:
        """Delete an object only if it still has the given ETag.

        Args:
            key: The object key.
            etag: The ETag the object must currently have.
        """
        self.check_etag(key, etag)
        del self._entries[key]

    def list(self, prefix: str, limit: int) -> Sequence[Listed]:
        """List objects under a prefix in key order.

        Args:
            prefix: The key prefix.
            limit: Stop after this many entries.

        Returns:
            The matching entries.
        """
        keys = sorted(key for key in self._entries if key.startswith(prefix))
        return [
            Listed(
                key=key,
                etag=self._entries[key].stored.etag,
                modified=self._entries[key].modified,
            )
            for key in keys[:limit]
        ]

    def presign(self, key: str, filename: str) -> str:
        """Create a fake download URL.

        Args:
            key: The object key.
            filename: The download file name to suggest.

        Returns:
            A ``memory:`` URL describing the request.
        """
        query = urllib.parse.urlencode(dict(filename=filename))
        return f"memory:{key}?{query}" if self._entries else ""

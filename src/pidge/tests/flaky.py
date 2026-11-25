"""Fakes for failure injection, AWS errors and bearer tokens."""

import dataclasses
from collections.abc import Callable, Mapping, Sequence

from ..auth import AuthError, ScopeError
from ..ports import Context
from ..store import Listed, ObjectStore, PreconditionFailed, StoredObject
from .fakes import World, with_store


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class FlakyStore:
    """Wraps a store; writes under a prefix fail a set number of times.

    Construct it with :meth:`create`.
    """

    _inner: ObjectStore
    _prefix: str
    _remaining: list[int]
    _error: Callable[[str], Exception]

    @classmethod
    def create(cls, inner: ObjectStore, prefix: str, failures: int) -> "FlakyStore":
        """Wrap a store; matching writes raise OSError.

        Args:
            inner: The real store.
            prefix: Writes to keys with this prefix fail.
            failures: How many times they fail before succeeding.

        Returns:
            The wrapper.
        """
        return cls(
            _inner=inner,
            _prefix=prefix,
            _remaining=[failures],
            _error=lambda key: OSError(f"injected failure writing {key}"),
        )

    @classmethod
    def racing(cls, inner: ObjectStore, prefix: str, failures: int) -> "FlakyStore":
        """Wrap a store; matching writes lose a race (PreconditionFailed).

        Args:
            inner: The real store.
            prefix: Writes to keys with this prefix fail.
            failures: How many times they fail before succeeding.

        Returns:
            The wrapper.
        """
        return cls(
            _inner=inner,
            _prefix=prefix,
            _remaining=[failures],
            _error=PreconditionFailed,
        )

    def check(self, key: str) -> None:
        """Fail if this write should fail.

        Args:
            key: The key being written.

        Raises:
            self._error: While failures remain for matching keys.
        """
        if key.startswith(self._prefix) and self._remaining[0] > 0:
            self._remaining[0] -= 1
            raise self._error(key)

    def get(self, key: str) -> StoredObject:
        """Delegate a read.

        Args:
            key: The key.

        Returns:
            The object.
        """
        return self._inner.get(key)

    def get_if_changed(self, key: str, etag: str) -> StoredObject:
        """Delegate a conditional read.

        Args:
            key: The key.
            etag: The known ETag.

        Returns:
            The object.
        """
        return self._inner.get_if_changed(key, etag)

    def put(self, key: str, body: bytes) -> str:
        """Delegate a write, perhaps failing.

        Args:
            key: The key.
            body: The bytes.

        Returns:
            The ETag.
        """
        self.check(key)
        return self._inner.put(key, body)

    def put_new(self, key: str, body: bytes) -> str:
        """Delegate a create, perhaps failing.

        Args:
            key: The key.
            body: The bytes.

        Returns:
            The ETag.
        """
        self.check(key)
        return self._inner.put_new(key, body)

    def replace(self, key: str, body: bytes, etag: str) -> str:
        """Delegate a conditional write, perhaps failing.

        Args:
            key: The key.
            body: The bytes.
            etag: The expected ETag.

        Returns:
            The ETag.
        """
        self.check(key)
        return self._inner.replace(key, body, etag)

    def delete(self, key: str) -> None:
        """Delegate a delete.

        Args:
            key: The key.
        """
        self._inner.delete(key)

    def delete_if(self, key: str, etag: str) -> None:
        """Delegate a conditional delete.

        Args:
            key: The key.
            etag: The expected ETag.
        """
        self._inner.delete_if(key, etag)

    def list(self, prefix: str, limit: int) -> Sequence[Listed]:
        """Delegate a listing.

        Args:
            prefix: The prefix.
            limit: The limit.

        Returns:
            The entries.
        """
        return self._inner.list(prefix, limit)

    def presign(self, key: str, filename: str, seconds: int) -> str:
        """Delegate presigning.

        Args:
            key: The key.
            filename: The file name.
            seconds: The lifetime.

        Returns:
            The URL.
        """
        return self._inner.presign(key, filename, seconds)

    def presign_upload(self, key: str, size: int, seconds: int) -> str:
        """Delegate upload presigning.

        Args:
            key: The key.
            size: The body size.
            seconds: The lifetime.

        Returns:
            The URL.
        """
        return self._inner.presign_upload(key, size, seconds)


def client_error(code: str, status: int) -> Exception:
    """Build an exception shaped like botocore's ClientError.

    Args:
        code: The AWS error code.
        status: The HTTP status.

    Returns:
        The exception.
    """
    error = Exception(code)
    setattr(
        error,
        "response",
        {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}},
    )
    return error


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class Validator:
    """A bearer validator that accepts only known tokens.

    Attributes:
        outcomes: For each known token, None to accept it or the error to raise.
    """

    outcomes: Mapping[str, Exception | None]

    def validate(self, token: str, resource: str, scope: str) -> Mapping[str, object]:
        """Check a token.

        Args:
            token: The token.
            resource: The resource URL.
            scope: The required scope.

        Returns:
            Claims naming the resource and scope.

        Raises:
            failure: The token's configured error.
        """
        failure = self.outcomes.get(token, AuthError("unknown token"))
        if failure is not None:
            raise failure
        return dict(aud=resource, scope=scope)


GOOD = Validator(outcomes={"good": None, "narrow": ScopeError("full")})


def failing(world: World, prefix: str, failures: int) -> Context:
    """The world's context, with writes under a prefix raising OSError.

    Args:
        world: The environment.
        prefix: Writes to keys with this prefix fail.
        failures: How many times.

    Returns:
        The context.
    """
    return with_store(world, FlakyStore.create(world.store, prefix, failures))


def racing(world: World, prefix: str, failures: int) -> Context:
    """The world's context, with writes under a prefix losing races.

    Args:
        world: The environment.
        prefix: Writes to keys with this prefix fail.
        failures: How many times.

    Returns:
        The context.
    """
    return with_store(world, FlakyStore.racing(world.store, prefix, failures))
